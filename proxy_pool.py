#!/usr/bin/env python3
"""proxy_pool.py — proxy pool, rotation policy, credential masking, argv
safety. No site knowledge lives here.

Accepted proxy formats (one per line in --proxy-file, or a single value on
--proxy):

    http://login:password@host:port
    host:port:login:password
    host:port                          (no auth)

Credentials NEVER reach argv or a command line — see PROHIBITED_FLAG_STYLE
below and the `*_auth()` helpers, which hand credentials to the driver's own
authenticated fields instead. Logs get host:port; credentials are masked.
"""
from __future__ import annotations

import itertools
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence
from urllib.parse import quote, urlparse

# Chromium reports a dead upstream proxy as one of these — NOT as a timeout.
# A caller must retry a *timeout* at the same exit, and a *dead proxy* error
# at a different one; conflating them wastes retries on a proxy that will
# never answer.
PROXY_DEAD_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_HTTP_RESPONSE_CODE_FAILURE",
    "net::ERR_NO_SUPPORTED_PROXIES",
)

# A literal command-line flag shaped like this leaks the password to `ps`,
# shell history and any crash report that includes argv. Never build one.
PROHIBITED_FLAG_STYLE = "--proxy-server=http://login:password@host:port"

_LINE_RE = re.compile(
    r"^(?:(?P<scheme>\w+)://)?"
    r"(?:(?P<login>[^:@/]+):(?P<password>[^:@/]*)@)?"
    r"(?P<host>[^:/@]+):(?P<port>\d+)"
    r"(?::(?P<login2>[^:@/]+):(?P<password2>[^:@/]*))?$"
)


class ProxyParseError(ValueError):
    pass


@dataclass(frozen=True)
class Proxy:
    host: str
    port: int
    login: Optional[str] = None
    password: Optional[str] = None
    scheme: str = "http"

    @property
    def has_auth(self) -> bool:
        return bool(self.login)

    def masked(self) -> str:
        auth = f"{self.login}=***@" if self.login else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def server_only(self) -> str:
        """host:port with NO credentials — safe to pass as a bare
        `--proxy-server` / `proxyServer` value on engines that cannot
        authenticate that way (Selenium's --proxy-server takes no auth)."""
        return f"{self.scheme}://{self.host}:{self.port}"

    def playwright_proxy_dict(self) -> dict:
        """Playwright's native `proxy=` field — credentials never touch argv."""
        d = {"server": self.server_only()}
        if self.login:
            d["username"] = self.login
            d["password"] = self.password or ""
        return d

    def pyppeteer_launch_arg(self) -> str:
        """pyppeteer/puppeteer: server goes on the launch args (no creds);
        call `page.authenticate({...})` separately for credentials."""
        return f"--proxy-server={self.server_only()}"

    def url(self, with_auth: bool = True) -> str:
        """The proxy as a URL for an HTTP client (the pre-check). Never log
        it: with `with_auth` it carries the password."""
        if with_auth and self.login:
            auth = "%s:%s@" % (quote(self.login, safe=""), quote(self.password or "", safe=""))
            return f"{self.scheme}://{auth}{self.host}:{self.port}"
        return self.server_only()

    def pyppeteer_auth_dict(self) -> Optional[dict]:
        if not self.login:
            return None
        return {"username": self.login, "password": self.password or ""}


def parse_proxy_line(line: str) -> Proxy:
    line = line.strip()
    if not line or line.startswith("#"):
        raise ProxyParseError("blank/comment proxy line")
    m = _LINE_RE.match(line)
    if not m:
        raise ProxyParseError("unrecognised proxy format (value omitted to protect credentials)")
    g = m.groupdict()
    login = g["login"] or g["login2"]
    password = g["password"] if g["login"] else g["password2"]
    return Proxy(
        host=g["host"],
        port=int(g["port"]),
        login=login,
        password=password,
        scheme=g["scheme"] or "http",
    )


def load_proxies(proxy: Optional[str], proxy_file: Optional[str]) -> List[Proxy]:
    out: List[Proxy] = []
    if proxy:
        out.append(parse_proxy_line(proxy))
    if proxy_file:
        for line in Path(proxy_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(parse_proxy_line(line))
    return out


class ProxyPool:
    """Round-robin pool with per-proxy failure tracking.

    Concurrency contract (see engines' --concurrency): call `.worker_view(i,
    n)` once per worker to get an independent `ProxyPool` rotated to a
    different starting offset over the SAME underlying proxy list. Each
    worker then only ever calls `.next()` on its own view, so no lock is
    needed anywhere — the safety comes from construction, not discipline.
    """

    def __init__(self, proxies: Sequence[Proxy], shuffle: bool = False, block_retries: int = 3):
        proxies = list(proxies)
        if shuffle:
            proxies = proxies[:]
            random.shuffle(proxies)
        self._proxies: List[Proxy] = proxies
        self._blocked_count = {id(p): 0 for p in proxies}
        self._dead = {id(p): False for p in proxies}
        self.block_retries = max(1, block_retries)
        self._cursor = 0

    def __len__(self) -> int:
        return len(self._proxies)

    def worker_view(self, worker_index: int, total_workers: int) -> "ProxyPool":
        if not self._proxies:
            return ProxyPool([], block_retries=self.block_retries)
        offset = worker_index % len(self._proxies)
        rotated = self._proxies[offset:] + self._proxies[:offset]
        view = ProxyPool(rotated, block_retries=self.block_retries)
        return view

    def _live_proxies(self) -> List[Proxy]:
        return [p for p in self._proxies if not self._dead[id(p)]]

    def next(self) -> Optional[Proxy]:
        live = self._live_proxies()
        if not live:
            return None
        p = live[self._cursor % len(live)]
        self._cursor += 1
        return p

    def peek(self) -> Optional[Proxy]:
        """The proxy the next `.next()` returns, without advancing."""
        live = self._live_proxies()
        return live[self._cursor % len(live)] if live else None

    def report_failure(self, proxy: Proxy, dead: bool) -> None:
        """`dead=True` for a PROXY_DEAD_MARKERS error (kills the exit after
        `block_retries` in a row); `dead=False` (a timeout / non-proxy
        error) does not count against the exit at all."""
        key = id(proxy)
        if key not in self._blocked_count:
            return
        if not dead:
            return
        self._blocked_count[key] += 1
        if self._blocked_count[key] >= self.block_retries:
            self._dead[key] = True

    def report_success(self, proxy: Proxy) -> None:
        self._blocked_count[id(proxy)] = 0


def is_proxy_dead_error(message: str) -> bool:
    return any(marker in message for marker in PROXY_DEAD_MARKERS)


# --------------------------------------------------------------------------- #
# Redacting a credential OUT OF an arbitrary string — not the Proxy dataclass
# above, but a driver/HTTP library's own exception message.
# An EXCEPTION MESSAGE is a log — Playwright's connect_over_cdp repeats a
# failed ws:// endpoint (login:password embedded) up to five times in one
# error (message + a four-line call log), and `requests` puts the full URL,
# query string included, into every HTTPError/connection-error message. Any
# endpoint whose credential rides in the URL — a --cdp-endpoint connection
# string, or 2Captcha's fingerprint/random which takes `?key=...` — leaks it
# the moment the call fails, independent of the `Proxy.masked()` path above,
# which only ever sees a *successful* parse, never a driver's own error text.
# Live-reproduced (2026-09-14): a bad connect_over_cdp call put the fake
# login AND password into the exception's "Call log" four times over.
_URL_CREDENTIAL_RE = re.compile(r"(?P<scheme>\w+://)[^\s/@]+@")
_KEY_PARAM_RE = re.compile(r"(?i)\b((?:client)?key|token|api[_-]?key)=[^&\s'\"]+")


def redact_credentials(text: str) -> str:
    """Scrub every credential shape this family's own endpoints can leak
    into a driver/HTTP exception's message, GLOBALLY (re.sub, not just the
    first occurrence — a masker that stops after one hit prints the
    password the other four times and looks like it is working). Call this
    on `str(exc)` BEFORE re-raising or logging — never pass the original
    exception through afterwards (chain with `from None`), or its own
    unredacted text still reaches the traceback."""
    if not text:
        return text
    text = _URL_CREDENTIAL_RE.sub(lambda m: f"{m.group('scheme')}***:***@", text)
    text = _KEY_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)
    return text




# ---------------------------------------------------------------------------
# Traffic savers for runs through a paid proxy.
#
# 1. Pre-check. ONE small request (a HEAD for the target site's robots.txt)
#    goes through the proxy BEFORE a browser is started. A gateway that
#    answers 407 (credential refused, or the traffic balance is exhausted) or
#    402 (traffic/balance exhausted) ends the run there, with exit 5, instead
#    of launching a browser whose every request would be refused the same way.
#    Any other outcome — a 200, a 403 from the site, a timeout, a DNS error —
#    is not a verdict on the proxy and the run continues exactly as before.
# 2. Media blocking. Images, video/audio and web fonts are aborted in the
#    browser while its traffic goes through a LOCAL proxy (`--proxy` /
#    `--proxy-file`): no parser here reads them, and they are most of a page's
#    bytes. Not applied with `--cdp-endpoint` (the remote browser is billed
#    and configured on its own side) or with no proxy (nothing is metered).
#    `--load-media` keeps them.
# ---------------------------------------------------------------------------

PROXY_PRECHECK_STATUSES = {
    407: "the gateway refused the credential (or the traffic balance is exhausted)",
    402: "proxy traffic/balance exhausted",
}
PROXY_PRECHECK_STOP_REASON = "proxy_auth_failed"

# Playwright `request.resource_type` values that are aborted.
BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})
# The same three as CDP `Network.ResourceType` names (pyppeteer, via Fetch).
BLOCKED_CDP_RESOURCE_TYPES = ("Image", "Media", "Font")
# Selenium has no request events, only `Network.setBlockedURLs`, which
# matches URLs, not resource types: an image served without an extension
# still loads there.
BLOCKED_URL_PATTERNS = tuple(
    "*.%s%s" % (ext, tail)
    for ext in ("jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "ico",
                "mp4", "webm", "m4v", "mov", "mp3", "m4a", "ogg", "wav",
                "woff", "woff2", "ttf", "otf", "eot")
    for tail in ("", "?*"))


class ProxyPrecheckError(RuntimeError):
    """The proxy gateway itself refused the pre-check (407 or 402)."""

    def __init__(self, status: int):
        self.status = status
        super().__init__("HTTP %d: %s" % (status, PROXY_PRECHECK_STATUSES[status]))


_STATUS_IN_ERROR_RE = re.compile(r"Tunnel connection failed:\s*(40[27])\b")


def robots_url(url: Optional[str]) -> Optional[str]:
    """`scheme://host/robots.txt` for a page URL — the pre-check's target."""
    try:
        parts = urlparse(url or "")
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return "%s://%s/robots.txt" % (parts.scheme, parts.netloc.rsplit("@", 1)[-1])


def precheck_proxy(proxy_url: Optional[str], probe_url: Optional[str],
                   timeout: float = 20.0) -> Optional[int]:
    """One HEAD for `probe_url` through `proxy_url`.

    Raises ProxyPrecheckError on a gateway 407/402. Returns the status the
    probe got otherwise, or None when there was nothing to check (no proxy, a
    non-HTTP proxy scheme, no probe URL) or the request failed in transport —
    a transport failure keeps the run's existing behaviour. The proxy URL is
    never printed or put in the exception.
    """
    if not proxy_url or not probe_url:
        return None
    try:
        scheme = (urlparse(proxy_url).scheme or "http").lower()
    except ValueError:
        return None
    if scheme not in ("http", "https"):
        return None
    try:
        import requests
    except ImportError:
        return None
    try:
        response = requests.head(probe_url, proxies={"http": proxy_url, "https": proxy_url},
                                 timeout=timeout, allow_redirects=False)
    except requests.exceptions.ProxyError as exc:
        # An https target: the gateway answers the CONNECT, and urllib3
        # reports its status line as "Tunnel connection failed: 407 ...".
        match = _STATUS_IN_ERROR_RE.search(str(exc))
        if match:
            raise ProxyPrecheckError(int(match.group(1))) from None
        return None
    except Exception:  # noqa: BLE001 - transport errors are not a verdict
        return None
    if response.status_code in PROXY_PRECHECK_STATUSES:
        raise ProxyPrecheckError(response.status_code)
    return response.status_code


def precheck_message(exc: ProxyPrecheckError, env_var: str, site: str,
                     browser: bool = True) -> str:
    """What to print. Names the variable, never its value."""
    return ("[!] proxy pre-check failed — %s.\n"
            "    %s%s was never reached. Check %s (or the --proxy / --proxy-file "
            "value)." % (exc, "No browser was started and " if browser else "",
                         site, env_var))


def should_block_media(args, proxy_in_use: bool, over_cdp: bool = False) -> bool:
    """Abort images/media/fonts? Only for a LOCAL browser behind a proxy.

    `--load-media` opts out. The hidden `--force-block-media` turns it on
    without a proxy, so the effect on parsing can be measured for free.
    """
    if getattr(args, "force_block_media", False):
        return True
    if getattr(args, "load_media", False) or over_cdp:
        return False
    return bool(proxy_in_use)


def playwright_media_route(route) -> None:
    """`context.route("**/*", ...)` handler: abort the heavy resource types."""
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort("blockedbyclient")
    else:
        route.continue_()


async def pyppeteer_block_media(page) -> None:
    """Abort images/media/fonts in a pyppeteer page through the CDP Fetch domain.

    `page.setRequestInterception` sends Network.setRequestInterception, which
    current Chromium no longer implements; Fetch.enable with resource-type
    patterns pauses only the requests to be aborted.
    """
    import asyncio
    client = await page.target.createCDPSession()

    def on_paused(event):
        asyncio.ensure_future(client.send("Fetch.failRequest", {
            "requestId": event["requestId"], "errorReason": "BlockedByClient"}))

    client.on("Fetch.requestPaused", on_paused)
    await client.send("Fetch.enable", {"patterns": [
        {"resourceType": kind, "requestStage": "Request"}
        for kind in BLOCKED_CDP_RESOURCE_TYPES]})


def selenium_block_media(driver) -> None:
    """Block media URLs in a Chrome WebDriver through `Network.setBlockedURLs`."""
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": list(BLOCKED_URL_PATTERNS)})
