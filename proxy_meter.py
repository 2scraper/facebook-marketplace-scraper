"""proxy_meter.py — count what a run sends through a local proxy, and stop
at a budget.

Only a LOCAL proxy is metered (`--proxy` / `--proxy-file` / the `*_PROXY`
variable): a residential proxy bills per byte, and a run that loops or a page
that grew can spend a lot before anyone looks. Nothing is counted or limited
over `--cdp-endpoint` (the remote browser is billed on its own side), over the
Scraper API, or with no proxy.

What is counted, per engine — an ESTIMATE of the tunnelled payload:

* Playwright: `request.sizes()` of every finished or failed request —
  request headers + body, response headers + (encoded) body.
* pyppeteer: CDP `Network.loadingFinished.encodedDataLength` (response
  headers + encoded body as Chromium received them) of every response that
  came over the network, plus each request's line, headers and post body
  from `Network.requestWillBeSent`. Chrome's own error page and cache hits
  are not counted.
* Selenium: the same CDP events, read from Chrome's performance log.
* HTTP clients (requests / curl): the request line, headers and body sent,
  plus the response status line, headers and body as received on the wire.

TLS records, the CONNECT exchange and TCP/IP framing are not visible to a
browser or an HTTP client, so the proxy's own bill is a few per cent higher.
WebSocket frames, and the browser's own background requests that are not
part of any page, are not counted either. 1 MB here is 1,000,000 bytes.

The budget (`--proxy-budget-mb`, default 10, 0 = unlimited): once the
running total goes past it, the engine stops fetching new pages, any request
the browser still tries is aborted, the rows already gathered are kept, and
the run ends with stop_reason `proxy_budget_exceeded` — exit 6 when it has
rows, 5 when it has none. It never silently goes on.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import sys
import threading
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

MB = 1_000_000
DEFAULT_BUDGET_MB = 10.0
STOP_REASON = "proxy_budget_exceeded"
SIDECAR_KEYS = ("proxy_bytes", "proxy_budget_bytes", "proxy_budget_exceeded")


class ProxyBudgetExceeded(RuntimeError):
    """Raised by `ProxyMeter.check()` once the budget is spent."""


def budget_mb(value: str) -> float:
    """argparse type for --proxy-budget-mb: a number >= 0 (0 = unlimited)."""
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--proxy-budget-mb must be a number of megabytes, got %r"
                                         % value) from None
    if number != number or number < 0 or number == float("inf"):
        raise argparse.ArgumentTypeError("--proxy-budget-mb must be >= 0 (0 = unlimited), got %r" % value)
    return number


def add_budget_argument(parser, *, env_var: Optional[str] = None) -> None:
    """--proxy-budget-mb. `env_var` only names the variable in the help."""
    parser.add_argument(
        "--proxy-budget-mb", type=budget_mb, default=DEFAULT_BUDGET_MB, metavar="MB",
        help="Stop once this many megabytes have gone through --proxy / --proxy-file (default %g; 0 = "
             "unlimited%s). The rows gathered so far are kept and the run exits 6 (5 with no rows), "
             "stop_reason proxy_budget_exceeded. The count is an estimate of the tunnelled payload "
             "(TLS/CONNECT overhead not included). Not applied with --cdp-endpoint or with no proxy"
             % (DEFAULT_BUDGET_MB, "; or set %s" % env_var if env_var else ""))


def _header_bytes(headers: Any) -> int:
    """`Name: value\\r\\n` per header (a dict, a list of pairs, or CDP's
    `[{name, value}]`)."""
    if not headers:
        return 0
    pairs: Iterable[Any]
    if isinstance(headers, dict):
        pairs = headers.items()
    else:
        pairs = ((h.get("name", ""), h.get("value", "")) if isinstance(h, dict) else h for h in headers)
    return sum(len(str(k)) + len(str(v)) + 4 for k, v in pairs)


def _request_line_bytes(method: Optional[str], url: str) -> int:
    parts = urlsplit(url or "")
    target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    return len("%s %s HTTP/1.1\r\n" % (method or "GET", target)) + 2  # + the blank line


class ProxyMeter:
    """A thread-safe running byte count with a budget."""

    def __init__(self, budget_mb: float = DEFAULT_BUDGET_MB, active: bool = False):
        self.active = bool(active)
        self.budget_bytes = int(round(budget_mb * MB)) if budget_mb else 0
        self.bytes = 0
        self.exceeded = False
        self._lock = threading.Lock()
        self._reported = False
        self._on_exceeded: list = []
        self._partial: dict = {}

    # ------------------------------------------------------------- counting
    def add(self, n: Any) -> bool:
        """Add `n` bytes. Returns True when the budget is (now) exceeded."""
        if not self.active:
            return False
        try:
            n = int(n or 0)
        except (TypeError, ValueError):
            return self.exceeded
        if n <= 0:
            return self.exceeded
        fire = False
        with self._lock:
            self.bytes += n
            if self.budget_bytes and self.bytes > self.budget_bytes and not self.exceeded:
                self.exceeded = fire = True
        if fire:
            print("[!] Proxy budget of %s exceeded (%.1f MB so far): no new requests; the rows gathered so far "
                  "are kept (stop_reason %s). Raise it with --proxy-budget-mb (0 = unlimited)."
                  % (self.budget_text(), self.bytes / MB, STOP_REASON), file=sys.stderr, flush=True)
            for callback in list(self._on_exceeded):
                try:
                    callback()
                except Exception:  # noqa: BLE001 - blocking is best effort; the loop check is the rule
                    pass
        return self.exceeded

    def on_exceeded(self, callback) -> None:
        """Run `callback()` once when the budget is first exceeded (e.g. to
        block every further request in a live browser)."""
        if self.exceeded:
            try:
                callback()
            except Exception:  # noqa: BLE001
                pass
        else:
            self._on_exceeded.append(callback)

    def check(self) -> None:
        """Raise ProxyBudgetExceeded once the budget is spent."""
        if self.exceeded:
            raise ProxyBudgetExceeded(STOP_REASON)

    # ------------------------------------------------------------- reporting
    def budget_text(self) -> str:
        return "%g MB" % (self.budget_bytes / MB) if self.budget_bytes else "no (unlimited)"

    def summary_line(self) -> str:
        return "Proxy traffic: %.1f MB of %s budget" % (self.bytes / MB, self.budget_text())

    def sidecar(self) -> dict:
        """The three sidecar keys. null when no local proxy was metered."""
        return {
            "proxy_bytes": self.bytes if self.active else None,
            "proxy_budget_bytes": (self.budget_bytes or None) if self.active else None,
            "proxy_budget_exceeded": bool(self.exceeded),
        }

    def report(self) -> None:
        """Print the summary line once (only when a local proxy was metered)."""
        if self.active and not self._reported:
            self._reported = True
            print(self.summary_line(), file=sys.stderr, flush=True)

    # ------------------------------------------------------------- Playwright
    @staticmethod
    def _sizes_total(sizes: dict) -> int:
        return sum(int(sizes.get(k) or 0) for k in (
            "requestHeadersSize", "requestBodySize", "responseHeadersSize", "responseBodySize"))

    def _playwright_fallback(self, request) -> int:
        try:
            body = request.post_data_buffer or b""
        except Exception:  # noqa: BLE001
            body = b""
        try:
            headers = request.headers
        except Exception:  # noqa: BLE001
            headers = {}
        return _request_line_bytes(request.method, request.url) + _header_bytes(headers) + len(body)

    def playwright_request_done(self, request) -> None:
        """`requestfinished` / `requestfailed` handler (sync API)."""
        try:
            total = self._sizes_total(request.sizes())
        except Exception:  # noqa: BLE001 - a failed request may have no sizes: count what was sent
            total = self._playwright_fallback(request)
        self.add(total)

    async def playwright_request_done_async(self, request) -> None:
        """`requestfinished` / `requestfailed` handler (async API)."""
        try:
            total = self._sizes_total(await request.sizes())
        except Exception:  # noqa: BLE001
            total = self._playwright_fallback(request)
        self.add(total)

    def attach_playwright(self, target) -> None:
        """Count every request of a sync-API BrowserContext (or Page)."""
        if not self.active:
            return
        target.on("requestfinished", self.playwright_request_done)
        target.on("requestfailed", self.playwright_request_done)
        if self.budget_bytes:
            target.route("**/*", self.playwright_budget_route)

    async def attach_playwright_async(self, target) -> None:
        """Count every request of an async-API BrowserContext (or Page)."""
        if not self.active:
            return
        target.on("requestfinished", self.playwright_request_done_async)
        target.on("requestfailed", self.playwright_request_done_async)
        if self.budget_bytes:
            await target.route("**/*", self.playwright_budget_route_async)

    def playwright_budget_route(self, route) -> None:
        """Abort everything once the budget is spent; otherwise hand the
        request to the next route (media blocking) or the network."""
        if self.exceeded:
            route.abort("blockedbyclient")
        else:
            route.fallback()

    async def playwright_budget_route_async(self, route) -> None:
        if self.exceeded:
            await route.abort("blockedbyclient")
        else:
            await route.fallback()

    # ------------------------------------------------------------- CDP events
    def feed_cdp(self, method: str, params: dict) -> None:
        """One CDP Network event (pyppeteer session or Selenium's log)."""
        if not self.active or not isinstance(params, dict):
            return
        if method == "Network.requestWillBeSent":
            redirect = params.get("redirectResponse") or {}
            if redirect:
                # The 3xx that led here was received and has no loadingFinished.
                self.add(redirect.get("encodedDataLength") or 0)
            request = params.get("request") or {}
            url = request.get("url") or ""
            if not url.startswith(("http://", "https://")):
                return  # data:, blob:, chrome:// (the new-tab page), ... never cross the proxy
            body = request.get("postData") or ""
            self.add(_request_line_bytes(request.get("method"), url) + _header_bytes(request.get("headers"))
                     + len(body.encode("utf-8", "replace") if isinstance(body, str) else body))
        elif method == "Network.responseReceived":
            # Only a response that came over the network is counted at
            # loadingFinished: Chrome's own error page ("This site can't be
            # reached", ~250 KB) is reported as a finished load of the failed
            # URL with no responseReceived, the new-tab page's chrome://
            # scripts (~2.8 MB, seen with Selenium) never leave the browser,
            # and a cache hit crossed nothing.
            response = params.get("response") or {}
            local = str(response.get("url") or "").split(":", 1)[0] not in ("", "http", "https")
            if not local and not (response.get("fromDiskCache") or response.get("fromServiceWorker")
                                  or response.get("fromPrefetchCache")):
                with self._lock:
                    self._partial.setdefault(params.get("requestId"), 0)
        elif method == "Network.dataReceived":
            # Only kept for a load that then fails: loadingFinished reports
            # the whole length itself.
            request_id = params.get("requestId")
            with self._lock:
                if request_id in self._partial:
                    self._partial[request_id] += int(params.get("encodedDataLength") or 0)
        elif method == "Network.loadingFinished":
            with self._lock:
                responded = self._partial.pop(params.get("requestId"), None) is not None
            if responded:
                self.add(params.get("encodedDataLength") or 0)
        elif method == "Network.loadingFailed":
            with self._lock:
                partial = self._partial.pop(params.get("requestId"), 0)
            self.add(partial)

    def feed_selenium_log(self, entries: Iterable[dict]) -> None:
        """Entries of `driver.get_log("performance")`."""
        for entry in entries or ():
            try:
                message = json.loads(entry["message"])["message"]
            except Exception:  # noqa: BLE001
                continue
            self.feed_cdp(message.get("method", ""), message.get("params") or {})

    async def attach_pyppeteer(self, page) -> None:
        """Count a pyppeteer page's traffic over its own CDP session; once
        the budget is spent, block every further URL in that page."""
        if not self.active:
            return
        client = await page.target.createCDPSession()
        client.on("Network.requestWillBeSent", lambda params: self.feed_cdp("Network.requestWillBeSent", params))
        for event in ("Network.responseReceived", "Network.dataReceived", "Network.loadingFinished",
                      "Network.loadingFailed"):
            client.on(event, lambda params, _event=event: self.feed_cdp(_event, params))
        await client.send("Network.enable", {})
        if self.budget_bytes:
            # The budget can trip on another thread (a concurrent worker) that
            # has no event loop: hand the block to this page's own loop.
            loop = asyncio.get_event_loop()

            async def _send_block():
                try:
                    await client.send("Network.setBlockedURLs", {"urls": ["*"]})
                except Exception:  # noqa: BLE001 - the loop check still stops the run
                    pass

            def _block():
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_send_block()))
            self.on_exceeded(_block)

    def attach_selenium(self, driver, *, keep_entries: bool = True) -> None:
        """Tee a Chrome WebDriver's performance log into the meter.

        The driver must have been created with `selenium_logging_prefs`
        applied. `driver.get_log("performance")` is wrapped so the engine's
        own readers still get every entry; `poll_selenium` reads the log for
        the meter alone and, with `keep_entries`, hands what it read to the
        engine's next `get_log` call."""
        if not self.active or getattr(driver, "_proxy_meter_attached", False):
            return
        original = driver.get_log
        held: list = []

        def get_log(kind):
            entries = original(kind)
            if kind != "performance":
                return entries
            self.feed_selenium_log(entries)
            out = held + list(entries)
            held.clear()
            self._selenium_block_if_needed(driver)
            return out

        def poll():
            try:
                entries = original("performance")
            except Exception:  # noqa: BLE001 - e.g. a driver without the log enabled
                return
            self.feed_selenium_log(entries)
            if keep_entries:
                held.extend(entries)
            self._selenium_block_if_needed(driver)

        driver.get_log = get_log
        driver._proxy_meter_poll = poll
        driver._proxy_meter_attached = True

    def poll_selenium(self, driver) -> None:
        """Bring the count up to date (call after each navigation/scroll)."""
        poll = getattr(driver, "_proxy_meter_poll", None)
        if poll is not None:
            poll()

    def _selenium_block_if_needed(self, driver) -> None:
        if self.exceeded and self.budget_bytes and not getattr(driver, "_proxy_meter_blocked", False):
            driver._proxy_meter_blocked = True
            try:
                driver.execute_cdp_cmd("Network.enable", {})
                driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": ["*"]})
            except Exception:  # noqa: BLE001 - the loop check still stops the run
                pass

    # ------------------------------------------------------------- HTTP clients
    def count_http(self, *, method: str, url: str, request_headers: Any = None, request_body: Any = None,
                   status_line: str = "", response_headers: Any = None, response_body_bytes: int = 0) -> None:
        body = request_body or b""
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        sent = _request_line_bytes(method, url) + _header_bytes(request_headers) + len(body)
        received = (len(status_line) + 2 if status_line else len("HTTP/1.1 200 OK\r\n")) \
            + _header_bytes(response_headers) + 2 + int(response_body_bytes or 0)
        self.add(sent + received)

    def count_requests_response(self, response) -> None:
        """A `requests.Response` (its redirect history too). Reads the body
        if it was not read yet; the size is what came off the wire (before
        decompression) when urllib3 can tell, else the decoded length."""
        if not self.active:
            return
        for r in list(getattr(response, "history", None) or []) + [response]:
            try:
                content = r.content
            except Exception:  # noqa: BLE001
                content = b""
            wire = 0
            try:
                wire = int(r.raw.tell()) if r.raw is not None else 0
            except Exception:  # noqa: BLE001
                wire = 0
            prepared = r.request
            self.count_http(
                method=getattr(prepared, "method", "GET"), url=getattr(prepared, "url", "") or r.url,
                request_headers=dict(getattr(prepared, "headers", {}) or {}),
                request_body=getattr(prepared, "body", None),
                status_line="HTTP/1.1 %s %s" % (r.status_code, r.reason or ""),
                response_headers=dict(r.headers or {}), response_body_bytes=wire or len(content or b""))

    def requests_hook(self, response, *args, **kwargs):
        """`hooks={"response": meter.requests_hook}` for requests."""
        self.count_requests_response(response)
        return response


# --------------------------------------------------------------------------- #
# The run's meter
# --------------------------------------------------------------------------- #
_current = ProxyMeter(active=False)


def start(budget_mb: Optional[float], *, active: bool) -> ProxyMeter:
    """Install the run's meter. `active` = traffic goes through a LOCAL proxy."""
    global _current
    _current = ProxyMeter(DEFAULT_BUDGET_MB if budget_mb is None else budget_mb, active=active)
    return _current


def current() -> ProxyMeter:
    return _current


def sidecar() -> dict:
    return _current.sidecar()


def report() -> None:
    _current.report()


def exceeded() -> bool:
    return _current.exceeded


def selenium_logging_prefs(options) -> None:
    """Turn Chrome's performance log on in a Selenium `Options`, keeping any
    other logging preference already set."""
    try:
        prefs = dict(options.to_capabilities().get("goog:loggingPrefs") or {})
    except Exception:  # noqa: BLE001
        prefs = {}
    prefs["performance"] = "ALL"
    options.set_capability("goog:loggingPrefs", prefs)


@atexit.register
def _report_at_exit() -> None:  # a crash path still prints the line once
    _current.report()
