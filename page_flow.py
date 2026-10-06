#!/usr/bin/env python3
"""page_flow.py — what one Marketplace address MEANS, decided once for all
engines (CLAUDE.md §1: three copies of this triage would drift, and the
drift would be silent — one engine reporting exit 3 where its twin reports
0 on the same page).

facebook.com/marketplace answers a logged-out visitor these ways, all seen
live on 2026-10-06 (see listing_parser.py):

  1. a search or category page with its listings embedded, under HTTP 200
     → `content`: the 24 rows it embeds, then the page's own pagination
       for more (see _paginate);
  2. a Marketplace route with an empty feed → `empty`: a complete answer,
     zero rows, not a failure;
  3. a Marketplace route with no feed yet → `loading`: WAIT, bounded,
     never retry (CLAUDE.md §18);
  4. "This content isn't available right now" (`CometErrorRoute`) →
     `not_found` (a removed listing): listed, never retried, never a block;
  5. a login or checkpoint page → `blocked`;
  6. anything else → `blocked` under 401/403/429, a `fetch_error`
     otherwise.

With `--details` each listing found by a search is opened on its own
page (one more page load each) for its description, condition, category,
photos and currency code. A listing URL given as input is read from its
own page directly.

Triage is driver-independent; the shared async flow below consumes NAMED
engine operations — goto, content, current_url, wait, close — and no
JavaScript crosses this boundary.
"""
from __future__ import annotations

import logging as _logging
from dataclasses import dataclass, field
from pathlib import Path as _Path
from typing import List, Optional
from typing import Protocol as _Protocol

import listing_parser as lp
from captcha_solver import detect_from_html
from output_writer import Product
from output_writer import finish_run as _finish_run
from proxy_pool import is_proxy_dead_error as _is_proxy_dead_error, redact_credentials as _redact

_log = _logging.getLogger("page_flow")

BLOCKING_STATUSES = (401, 403, 429)
# With one exit and no pool to rotate through, a block is per IP: every later
# page gets the same answer. After this many in a row the run stops and
# reports the rest as not attempted, keeping what it has.
STOP_AFTER_CONSECUTIVE_BLOCKS = 3
PAINT_WAIT_S = 20
UNKNOWN_WAIT_S = 8
POLL_S = 1.0
CHALLENGE_WAIT_S = 15
# Past the first 24 (see listing_parser: the page's own pagination request).
# A wheel scroll makes the page send the first one; it is then sent again
# with each next cursor. 3s apart: measured 2026-10-06, 25 requests at that
# pace read 573 listings before "Rate limit exceeded"; at 1.5s the limit
# came after 15. The limit did not lift within 3.5 minutes, so it ends the
# search's pagination rather than being waited out.
WHEEL_PX = 4000
WHEEL_ROUNDS = 8
WHEEL_WAIT_S = 2.0
PAGINATION_DELAY_S = 3.0
PAGINATION_STALL_ROUNDS = 3
PAGINATION_RETRIES = 2


def is_challenge(html: str) -> bool:
    """A bot-challenge marker on a page that is not facebook.com's own."""
    return lp.page_route(html) is None and detect_from_html(html or "", lp.BOT_CHALLENGE_MARKERS)


@dataclass
class Outcome:
    url: str
    kind: str = "search"            # search | item | detail
    rows: List[Product] = field(default_factory=list)
    state: str = "unknown"          # content | empty | not_found | login | loading | unknown
    blocked: bool = False
    failure: Optional[str] = None   # fetch_error | not_painted | parse_error | proxy_pool_exhausted | remote_api_error | not_attempted | rate_limited | stalled
    html: str = ""
    pagination: Optional[str] = None  # end | limit | rate_limited | stalled | no_request | unsupported | error
    pages_read: int = 0               # pagination answers read (0 = the 24 the page embeds only)


# --------------------------------------------------------------------------- #
# The engine boundary (CLAUDE.md §26)
# --------------------------------------------------------------------------- #
class PageSession(_Protocol):
    async def goto(self, url: str) -> Optional[int]: ...  # HTTP status or None; raises on failure
    async def content(self) -> str: ...
    async def current_url(self) -> str: ...
    async def wait(self, seconds: float) -> None: ...
    async def close(self) -> None: ...
    # Pagination (an engine with can_paginate = False need not provide these):
    async def dismiss_dialog(self) -> bool: ...  # close the login dialog drawn over the page
    async def wheel(self, pixels: int) -> None: ...  # a real mouse-wheel scroll
    async def take_requests(self) -> List[tuple]: ...  # (form body, answer) of the page's pagination requests since the last call
    async def post_form(self, data: str) -> tuple: ...  # (HTTP status, body) of a POST to /api/graphql/ from the page


class Engine(_Protocol):
    name: str
    readiness_s: float

    async def open(self, proxy) -> PageSession: ...
    async def sleep(self, seconds: float) -> None: ...
    async def solve_captcha(self, session: PageSession, *, html: str, url: str) -> Optional[dict]: ...


class SolveBudget:
    """One cap on PAID captcha solves for the whole run (CLAUDE.md §23: a
    per-page limit nothing sums is a bill). `limit=0` means never pay."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.spent = 0

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def spend(self) -> None:
        self.spent += 1


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def resolve_urls(args) -> tuple:
    """(addresses, skipped). `--query` / `--category` with `--location`
    build one search; `--url` wins over `--urls-file`, each a pasted
    Marketplace search, category or listing URL. Anything else is logged
    and skipped, never fetched; duplicates are fetched once."""
    if getattr(args, "query", None) or getattr(args, "category", None):
        if args.url or args.urls_file:
            raise ValueError("give --query/--category OR --url/--urls-file, not both: a pasted URL carries its own filters")
        if not getattr(args, "location", None):
            raise ValueError("--query/--category needs --location (a Marketplace location slug such as nyc, la, london)")
        url = lp.search_url(location=args.location, query=args.query, category=args.category,
                            min_price=args.min_price, max_price=args.max_price, sort=args.sort,
                            days_since_listed=args.days_since_listed)
        _warn_known_empty(url)
        return [url], 0
    if args.url:
        candidates = [args.url]
    elif args.urls_file:
        try:
            lines = _Path(args.urls_file).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError(f"could not read --urls-file {args.urls_file!r}: {exc}")
        candidates = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    else:
        return [], 0
    urls, skipped = [], 0
    for text in candidates:
        url = lp.normalize_input(text)
        if url is None:
            _log.warning("Skipping %s — %s.", text, lp.refusal_reason(text))
            skipped += 1
            continue
        if url not in urls:
            _warn_known_empty(url)
            urls.append(url)
    return urls, skipped


def _warn_known_empty(url: str) -> None:
    reason = lp.known_empty_combination(url)
    if reason:
        _log.warning("%s: expect no listings — %s.", url, reason)


def _dump_path(out_path: str, index: int) -> str:
    return f"{_Path(out_path).with_suffix('')}_debug_{index}.html"


async def _solve_within_budget(engine: Engine, session: PageSession, args, *, html: str, url: str) -> Optional[dict]:
    budget = getattr(args, "_solve_budget", None)
    if budget is not None and budget.remaining() == 0:
        _log.warning("Captcha solving skipped: the run's solve budget is spent (--max-solves %d).", budget.limit)
        return None
    result = await engine.solve_captcha(session, html=html, url=url)
    if budget is not None and result and result.get("action") in ("solved", "warning_solver_error"):
        budget.spend()  # a task was created and billed, whatever came back
    return result


# --------------------------------------------------------------------------- #
# One page
# --------------------------------------------------------------------------- #
async def _wait_for_paint(session: PageSession) -> tuple:
    html = await session.content()
    state = lp.page_state(html, final_url=await session.current_url())
    waited = 0.0
    while (state == "loading" and waited < PAINT_WAIT_S) or (state == "unknown" and waited < UNKNOWN_WAIT_S):
        await session.wait(POLL_S)
        waited += POLL_S
        try:
            html = await session.content()
        except Exception:  # noqa: BLE001 — mid-navigation
            continue
        state = lp.page_state(html, final_url=await session.current_url())
    if waited and state in ("content", "empty"):
        _log.info("Listings painted after %.0fs.", waited)
    return state, html


async def fetch(engine: Engine, args, url: str, index: int, proxy_pool, *, kind: str, on_content=None) -> Outcome:
    """Open one address and classify what came back; the html is kept for
    the caller to read. Every failure is a typed outcome, never a raise."""
    outcome = Outcome(url=url, kind=kind)
    proxy = proxy_pool.next() if proxy_pool is not None else None
    if proxy_pool is not None and proxy is None:
        outcome.failure = "proxy_pool_exhausted"
        return outcome
    _log.info("Opening %s (proxy: %s)", url, proxy.masked() if proxy else "(none — direct, or the --cdp-endpoint session's own exit)")
    try:
        session = await engine.open(proxy)
    except Exception as exc:
        _log.error("Browser connection failed — treating this page as failed, not a crash: %s", _redact(str(exc)))
        outcome.failure = "fetch_error"
        return outcome
    try:
        last_error, status = None, None
        for attempt in range(args.retries + 1):
            try:
                status = await session.goto(lp.fetch_url(url))
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 — every remote call must be bounded and reported
                last_error = _redact(str(exc)).splitlines()[0] if str(exc) else type(exc).__name__
                if proxy_pool is not None and proxy is not None and _is_proxy_dead_error(last_error):
                    proxy_pool.report_failure(proxy, dead=True)
                    _log.warning("Proxy reported dead: %s", last_error)
                else:
                    _log.warning("Navigation attempt %d/%d for %s failed: %s", attempt + 1, args.retries + 1, url, last_error)
                if getattr(engine, "fatal", None):
                    break  # a refused key or an empty balance: every retry would fail the same way
                if attempt < args.retries:
                    await engine.sleep(args.retry_delay)
        if last_error is not None:
            _log.error("%s permanently failed to load: %s", url, last_error)
            outcome.failure = "remote_api_error" if getattr(engine, "last_remote_error", False) else "fetch_error"
            return outcome

        state, html = await _wait_for_paint(session)
        if state == "unknown":
            waited = 0.0
            while is_challenge(html) and waited < CHALLENGE_WAIT_S:
                await session.wait(1)
                waited += 1
                html = await session.content()
            if is_challenge(html) and args.solve_captcha != "off":
                result = await _solve_within_budget(engine, session, args, html=html, url=url)
                if result and result.get("action") == "solved":
                    await session.wait(engine.readiness_s)
            state, html = await _wait_for_paint(session)
        outcome.state, outcome.html = state, html
        if args.dump_html:
            _Path(_dump_path(args.out, index)).write_text(html, encoding="utf-8")

        if state == "content" and on_content is not None:
            await on_content(session, outcome)  # while the page is still open
        elif state in ("content", "empty"):
            pass
        elif state == "not_found":
            _log.warning("%s: \"This content isn't available right now\" — removed or never there. Not a block.", url)
        elif state == "login":
            outcome.blocked = True
            _log.warning("%s: facebook.com sent this visitor to a login page — blocked, not empty. "
                         "Slow down (--delay-between-pages) or use another exit (--proxy-file).", url)
        elif state == "loading":
            outcome.failure = "not_painted"
            _log.warning("%s: the page loaded but its listings never arrived in %ds — nothing read. "
                         "Re-run with --dump-html to inspect it.", url, PAINT_WAIT_S)
        elif status == 407:
            outcome.failure = "fetch_error"
            _log.warning("%s: the proxy refused this request — HTTP 407, proxy authentication required. "
                         "Selenium cannot send a proxy password: allow this machine's IP in the proxy's settings "
                         "(IP whitelist), or use playwright_scraper.py / puppeteer_scraper.py.", url)
        elif status in BLOCKING_STATUSES:
            outcome.blocked = True
            _log.warning("%s: HTTP %s and not a facebook.com page (%d bytes, title %r) — blocked, not empty.",
                         url, status, len(html or ""), lp.page_title(html))
        else:
            outcome.failure = "fetch_error"
            _log.warning("%s: HTTP %s, not a facebook.com page (%d bytes, title %r) — nothing read. "
                         "Re-run with --dump-html to inspect it.", url, status or "-", len(html or ""), lp.page_title(html))
        if proxy_pool is not None and proxy is not None:
            if outcome.blocked:
                proxy_pool.report_failure(proxy, dead=True)  # a block is a property of the EXIT (CLAUDE.md §8)
            else:
                proxy_pool.report_success(proxy)
        if getattr(engine, "last_remote_error", False) and state not in ("content", "empty", "not_found"):
            outcome.failure = "remote_api_error"
        return outcome
    except Exception as exc:  # noqa: BLE001
        _log.warning("Page failed for %s: %s", url, _redact(str(exc)))
        outcome.failure = outcome.failure or "fetch_error"
        return outcome
    finally:
        try:
            await session.close()
        except Exception as exc:  # noqa: BLE001
            _log.warning("Session cleanup failed: %s", _redact(str(exc)))


# --------------------------------------------------------------------------- #
# The ONE run loop
# --------------------------------------------------------------------------- #
class _Run:
    """What the run has gathered, in the site's order."""

    def __init__(self, limit: int):
        self.limit = limit
        self.rows: List[Product] = []
        self.seen: set = set()
        self.outcomes: List[Outcome] = []
        self.rejected = 0
        self.capped = False
        self.consecutive_blocks = 0

    @property
    def full(self) -> bool:
        return len(self.rows) >= self.limit


async def run(engine: Engine, args, *, urls: List[str], proxy_pool, client, started_at: float) -> int:
    """Every address in input order, one browser session each; with
    --details, every listing found then gets its own page, in order."""
    state = _Run(args.max_results)
    stopped = False
    for i, url in enumerate(urls, 1):
        if state.full:
            state.capped = True
            break
        kind = "item" if lp.classify(url) and lp.classify(url)[0] == "item" else "search"
        async def on_content(session, outcome, url=url):
            _collect(state, outcome, url)
            if outcome.kind == "search":
                await _paginate(engine, args, session, state, outcome, url)
        outcome = await fetch(engine, args, url, i, proxy_pool, kind=kind, on_content=on_content)
        state.outcomes.append(outcome)
        if _should_stop(engine, state, outcome, proxy_pool, urls[i:]):
            stopped = True
            break
        if i < len(urls):
            await engine.sleep(args.delay_between_pages)
    if args.details and not stopped:
        await _details(engine, args, state, proxy_pool, start_index=len(urls) + 1)
    return finish_run(args, state, engine_name=engine.name, urls=urls, started_at=started_at)


def _collect(state: _Run, outcome: Outcome, url: str) -> None:  # the listings the page embeds
    if outcome.state != "content":
        return
    if outcome.kind == "item":
        listing_id = lp.classify(url)[1]
        row = lp.item_row(outcome.html, listing_id)
        if row is None:
            outcome.failure = "parse_error"
            _log.warning("%s: the listing's own data is not on its page.", url)
            return
        _add(state, row, outcome)
        return
    nodes = lp.listings(lp.json_blocks(outcome.html))
    for position, node in enumerate(nodes, 1):
        if state.full:
            state.capped = True
            return
        try:
            row = lp.listing_row(node, search=url, position=position)
        except Exception as exc:  # noqa: BLE001 — one malformed listing must not cost the search
            _log.warning("A listing on %s could not be read (%s) — skipped and counted as rejected.", url, exc)
            row = None
        if row is None:
            state.rejected += 1
            continue
        _add(state, row, outcome)


def _collect_payloads(state: _Run, outcome: Outcome, url: str, payloads) -> int:
    """Add a pagination answer's listings; returns how many were NEW."""
    added = 0
    for node in lp.listings(payloads):
        if state.full:
            break
        try:
            row = lp.listing_row(node, search=url, position=len(outcome.rows) + 1)
        except Exception as exc:  # noqa: BLE001
            _log.warning("A listing on %s could not be read (%s) — skipped and counted as rejected.", url, exc)
            row = None
        if row is None:
            state.rejected += 1
            continue
        before = len(state.rows)
        _add(state, row, outcome)
        added += len(state.rows) - before
    return added


async def _paginate(engine, args, session, state: _Run, outcome: Outcome, url: str) -> None:
    """Read past the 24 a search embeds, until --max-results, the end of the
    results, a rate limit, or three answers in a row with nothing new."""
    has_next, _ = lp.page_info(lp.json_blocks(outcome.html))
    if state.full or has_next is False:
        outcome.pagination = "limit" if state.full else "end"
        state.capped = state.capped or (state.full and has_next is not False)
        return
    if not getattr(engine, "can_paginate", False):
        outcome.pagination = "unsupported"
        state.capped = True  # more were announced; this mode cannot ask for them
        _log.info("%s: %d listings — this mode reads only the ones the page embeds (no live page to paginate).",
                  url, len(outcome.rows))
        return
    requests: List[tuple] = []
    for _ in range(WHEEL_ROUNDS):
        # The login dialog can be drawn after the listings; while it is up
        # it swallows the scroll, so it is closed before every round.
        await session.dismiss_dialog()
        await session.wheel(WHEEL_PX)
        await session.wait(WHEEL_WAIT_S)
        requests = await session.take_requests()
        if requests:
            break
    if not requests:
        outcome.pagination, outcome.failure = "no_request", "stalled"
        _log.warning("%s: the page never asked for more than its first %d listings — partial, not the end of the "
                     "results.", url, len(outcome.rows))
        return
    form, body = requests[-1][0], ""
    for _form, answer in requests:  # the page's own requests: their listings count too
        body = answer
        _collect_payloads(state, outcome, url, lp.graphql_payloads(answer))
        outcome.pages_read += 1
    stalled = 0
    while True:
        if state.full:
            outcome.pagination, state.capped = "limit", True
            return
        if lp.is_rate_limited(body):
            outcome.pagination, outcome.failure = "rate_limited", "rate_limited"
            _log.warning("%s: Facebook answered \"Rate limit exceeded\" after %d listings — stopping this search "
                         "(it did not lift within minutes when measured). Partial; another exit (--proxy-file) "
                         "can read on.", url, len(outcome.rows))
            return
        payloads = lp.graphql_payloads(body)
        has_next, cursor = lp.page_info(payloads)
        errors = any(node.get("errors") for payload in payloads for node in lp._find(payload, "errors"))
        if not errors and has_next is False:
            outcome.pagination = "end"
            return
        if errors or has_next is not True or not cursor:
            outcome.pagination, outcome.failure = "error", "stalled"
            _log.warning("%s: pagination returned errors or no valid cursor — keeping the rows as partial.", url)
            return
        data = lp.next_request(form, cursor)
        if data is None:
            outcome.pagination, outcome.failure = "error", "stalled"
            return
        # A single failed request is retried: seen once through a proxy, the
        # same request answered normally when sent again.
        status, body, problem = None, "", None
        for attempt in range(PAGINATION_RETRIES + 1):
            await session.wait(PAGINATION_DELAY_S * (attempt + 1))
            try:
                status, body = await session.post_form(data)
                problem = None if status == 200 else f"HTTP {status}"
            except Exception as exc:  # noqa: BLE001 — a page that died mid-pagination keeps what it gave
                problem = _redact(str(exc)).splitlines()[0] if str(exc) else type(exc).__name__
            if problem is None:
                break
        if problem is not None:
            _log.warning("%s: a pagination request failed %d times (%s) — keeping the %d listings read.", url,
                         PAGINATION_RETRIES + 1, problem, len(outcome.rows))
            outcome.pagination, outcome.failure = "error", "stalled"
            return
        outcome.pages_read += 1
        if _collect_payloads(state, outcome, url, lp.graphql_payloads(body)) == 0 and not lp.is_rate_limited(body):
            stalled += 1
            if stalled >= PAGINATION_STALL_ROUNDS:
                outcome.pagination, outcome.failure = "stalled", "stalled"
                _log.warning("%s: %d pagination answers in a row brought nothing new — partial.", url, stalled)
                return
        else:
            stalled = 0


def _add(state: _Run, row: Product, outcome: Outcome) -> None:
    if row.sku in state.seen:  # a listing two searches both found is one row, the first one's
        return
    state.seen.add(row.sku)
    state.rows.append(row)
    outcome.rows.append(row)


def _should_stop(engine, state: _Run, outcome: Outcome, proxy_pool, rest: List[str]) -> bool:
    state.consecutive_blocks = state.consecutive_blocks + 1 if outcome.blocked else 0
    stop = (outcome.failure == "proxy_pool_exhausted" or getattr(engine, "fatal", None)
            or (proxy_pool is None and state.consecutive_blocks >= STOP_AFTER_CONSECUTIVE_BLOCKS))
    if stop and rest:
        if proxy_pool is None and state.consecutive_blocks >= STOP_AFTER_CONSECUTIVE_BLOCKS:
            _log.error("%d blocked answers in a row from the same exit — stopping instead of asking again. "
                       "Rerun later, or with --proxy-file to rotate exits.", state.consecutive_blocks)
        state.outcomes.extend(Outcome(url=u, failure="not_attempted") for u in rest)
    return bool(stop)


async def _details(engine, args, state: _Run, proxy_pool, *, start_index: int) -> None:
    todo = [r for r in state.rows if r.price_source != "item_page" and r.description is None and r.search_url]
    for n, row in enumerate(todo):
        await engine.sleep(args.delay_between_pages)
        outcome = await fetch(engine, args, row.product_url, start_index + n, proxy_pool, kind="detail")
        state.outcomes.append(outcome)
        if outcome.state == "content":
            node = lp.detail(outcome.html, row.listing_id)
            if node is not None:
                lp.apply_detail(row, node, lp.listing_photos(outcome.html, row.listing_id))
            else:
                outcome.failure = "parse_error"
        elif outcome.state == "not_found":
            row.status = "REMOVED"  # gone between the search and its own page
        if _should_stop(engine, state, outcome, proxy_pool, [r.product_url for r in todo[n + 1:]]):
            for o in state.outcomes:
                if o.failure == "not_attempted":
                    o.kind = "detail"
            return


_INCOMPLETE_REASONS = ("rate_limited", "stalled", "not_painted", "parse_error", "proxy_pool_exhausted")


def finish_run(args, state: _Run, *, engine_name: str, urls: List[str], started_at: float) -> int:
    failed, failures = [], []
    for index, o in enumerate(state.outcomes, 1):
        if o.failure or o.blocked:
            failed.append(index)
            failures.append({"url": o.url, "kind": o.kind, "reason": o.failure or "blocked"})
    inputs = [o for o in state.outcomes if o.kind != "detail"]
    pages = [{"url": o.url, "kind": o.kind, "state": o.state, "rows": len(o.rows), "pagination": o.pagination,
              "pagination_answers": o.pages_read,
              "result": "not_found" if o.state == "not_found" else o.failure or ("blocked" if o.blocked else "ok")}
             for o in inputs]
    details = [o for o in state.outcomes if o.kind == "detail"]
    return finish(args, products=state.rows, blocked=any(o.blocked for o in state.outcomes),
                  remote_api_error=any(o.failure == "remote_api_error" for o in state.outcomes),
                  engine_name=engine_name, urls=urls, started_at=started_at,
                  pages_requested=len(state.outcomes),
                  pages_completed=sum(1 for o in state.outcomes if not (o.failure or o.blocked)),
                  failed_pages=failed, failures=failures, capped=state.capped, pages=pages,
                  not_found=[o.url for o in state.outcomes if o.state == "not_found"],
                  details_read=sum(1 for o in details if o.state == "content" and not o.failure),
                  rejected_rows=state.rejected)


def finish(args, *, products: List[Product], blocked: bool, remote_api_error: bool, engine_name: str,
           urls: List[str], started_at: float, pages_completed: int, failed_pages: List[int],
           pages_requested: Optional[int] = None, failures=None, capped: bool = False, pages=None,
           not_found=None, details_read: int = 0, rejected_rows: int = 0) -> int:
    budget = getattr(args, "_solve_budget", None)
    selection = {"mode": "marketplace", "urls": urls, "details": bool(args.details), "max_results": args.max_results}
    extra = {"solves_spent": budget.spent if budget is not None else 0, "selection": selection,
             "failed_urls": failures or [], "pages": pages or [], "not_found_urls": not_found or [],
             "details_read": details_read}
    return _finish_run(
        products=products, out_path=args.out, fmt=args.format, engine=engine_name, url=urls[0] if urls else "",
        pages_requested=len(urls) if pages_requested is None else pages_requested, pages_completed=pages_completed,
        failed_pages=failed_pages or None, blocked=blocked, remote_api_error=remote_api_error,
        allow_empty=args.allow_empty, started_at=started_at, price_confirmed_pct=None,
        max_results=args.max_results, rejected_rows=rejected_rows,
        incomplete_reason=next((f["reason"] for f in (failures or []) if f["reason"] in _INCOMPLETE_REASONS), None),
        capped=capped, extra_meta=extra,
    )


def validate_common(args, *, urls, skipped, print_err) -> Optional[int]:
    """The usage checks every engine makes before launching anything;
    returns an exit code to stop with, or None to go on."""
    from output_writer import EXIT_BAD_USAGE
    if not urls:
        if skipped:
            print_err(f"Error: none of the inputs is a Marketplace search, category or listing ({skipped} skipped) — nothing left to fetch")
        else:
            print_err("Error: provide --query/--category with --location, or --url / --urls-file")
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print_err(f"Error: unsupported --format {args.format!r}")
        return EXIT_BAD_USAGE
    # Checked before any browser starts: the file is written only at the end,
    # so a missing directory would otherwise crash after the whole run.
    out_dir = _Path(args.out).parent if getattr(args, "out", None) else _Path(".")
    if not out_dir.is_dir():
        print_err(f"Error: the --out directory {str(out_dir)!r} does not exist")
        return EXIT_BAD_USAGE
    return None


def add_marketplace_arguments(p) -> None:
    """The input flags, identical on every engine."""
    p.add_argument("--query", default=None, help="Keyword search, as typed into Marketplace's search box (needs --location)")
    p.add_argument("--category", default=None, help="Browse a category instead of searching, e.g. vehicles, electronics (needs --location)")
    p.add_argument("--location", default=None, help="Marketplace location slug or numeric id, as in facebook.com/marketplace/<location>/ — nyc, la, london, berlin, …")
    p.add_argument("--min-price", type=int, default=None, help="Lowest price, in the location's currency")
    p.add_argument("--max-price", type=int, default=None, help="Highest price, in the location's currency")
    p.add_argument("--sort", choices=lp.SORTS, default=None, help="The site's own sort order (default: best match)")
    p.add_argument("--days-since-listed", type=int, choices=lp.DAYS, default=None, help="Only listings from the last 1, 7 or 30 days")
    p.add_argument("--details", action="store_true", help="Also open each listing's own page: description, condition, category, photos, currency (one more page load per listing)")
    p.add_argument("--url", default=None, help="One Marketplace search, category or listing URL, as copied from a browser (or set FACEBOOK_URL) — overrides --urls-file")
    p.add_argument("--urls-file", default=None, help="A file with one Marketplace search, category or listing URL per line (# comments allowed)")
