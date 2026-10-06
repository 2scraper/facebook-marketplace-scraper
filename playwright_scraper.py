#!/usr/bin/env python3
"""playwright_scraper.py — Playwright engine for facebook-marketplace-scraper.
Playwright is the primary engine (see selenium_scraper.py /
puppeteer_scraper.py for parity copies — all three must agree on exit
codes, run status and whether a run crashes or spends money — CLAUDE.md
§4).

**Input**: a search — `--query` (keywords) or `--category` (a category
slug) in a `--location`, narrowed by `--min-price`, `--max-price`,
`--sort` and `--days-since-listed` — or `--url` / `--urls-file`,
Marketplace search, category or listing URLs pasted from a browser.

**How a search is read (measured live 2026-10-06, see listing_parser.py)**:
open it logged out, wait for the listings, read the 24 the page embeds
(a logged-out page gives no more by scrolling). With `--details` each
listing's own page is opened too. The loop itself is `page_flow.run`,
shared by all three engines; this file only provides the Playwright page
session.

Example:
    python3 playwright_scraper.py --query bike --location nyc
    python3 playwright_scraper.py --query sofa --location london --max-price 200 --details
    python3 playwright_scraper.py --category vehicles --location nyc --sort creation_time_descend
    python3 playwright_scraper.py --url "https://www.facebook.com/marketplace/item/1800446554437755/"
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from typing import Optional

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    Browser = BrowserContext = Page = None
    async_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = _IMPORT_ERROR
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

import env_config
import listing_parser as lp
import page_flow
import scraper_api_engine
import scraper_api_client
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH
from proxy_pool import Proxy, ProxyPool, ProxyParseError, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaClient

ENGINE_NAME = "playwright"

# --- the handful of engine constants that vary per site (CLAUDE.md §5) ---
NAV_TIMEOUT_MS = 45_000
# The shell arrives first and the results are streamed in after it;
# page_flow polls for them (PAINT_WAIT_S). This pause only lets a
# client-side redirect land before the first look.
READINESS_WAIT_MS = 1_500

GRAPHQL_PATH = lp.GRAPHQL_PATH
DIALOG_CLOSE_SELECTOR = lp.DIALOG_CLOSE_SELECTOR
# A POST from the page itself, so it carries the page's own cookies and
# origin — the same request the page makes when it is scrolled.
POST_FORM_JS = """async (data) => {
  const r = await fetch('/api/graphql/', {method: 'POST', credentials: 'include',
    headers: {'content-type': 'application/x-www-form-urlencoded'}, body: data});
  return [r.status, await r.text()];
}"""

log = logging.getLogger("playwright_scraper")


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def _nonnegative_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return ivalue


def _nonnegative_float(value: str) -> float:
    fvalue = float(value)
    if fvalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return fvalue


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Facebook Marketplace scraper (listings, logged out) — Playwright engine",
        epilog="Credentials belong in .env / FACEBOOK_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    page_flow.add_marketplace_arguments(p)
    p.add_argument("--max-results", type=_positive_int, default=100, help="Listings to write in total (a search gives at most 24); the rest is left out and the run marked capped")
    p.add_argument("--delay-between-pages", type=_nonnegative_float, default=2.0, help="Politeness delay between page loads, seconds")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None, help="Output path (default: facebook_marketplace.<format>)")
    p.add_argument("--retries", type=_nonnegative_int, default=2, help="Retries on a single page's navigation failure")
    p.add_argument("--retry-delay", type=_nonnegative_float, default=3.0)
    p.add_argument("--proxy", default=None, help="A single proxy, e.g. http://login:pass@host:port (or set FACEBOOK_PROXY)")
    p.add_argument("--proxy-file", default=None, help="One proxy per line, same formats as --proxy")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=3)
    p.add_argument("--twocaptcha-key", default=None, help="(or set TWOCAPTCHA_KEY)")
    p.add_argument("--captcha-api", default=None, help="Override the 2Captcha API base URL (testing only)")
    p.add_argument("--solve-captcha", choices=["off", "when-blocked", "always"], default="when-blocked")
    p.add_argument("--max-solves", type=_nonnegative_int, default=8, help="Cap on PAID 2Captcha solves for the whole run (0 = never pay); recorded as solves_spent")
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score (2Captcha's minScore task field)")
    p.add_argument("--cdp-endpoint", default=None, help="Connect to a remote CDP session (e.g. the 2Captcha Scraping Browser API) instead of launching locally (or set FACEBOOK_CDP_ENDPOINT)")
    p.add_argument("--scraper-api", action="store_true", help="Fetch through 2Captcha's Scraper API instead of driving this browser (needs TWOCAPTCHA_KEY; routed through FACEBOOK_CDP_ENDPOINT's Scraping Browser profile when one is set; the 24 listings a search embeds; with --details also each listing's own page)")
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument("--fp-tags", default=None, help="Fingerprint API OS filter — one of Windows, Linux, Android (no browser names, no lists)")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument("--allow-empty", action="store_true", help="Write output even if zero rows were found")
    p.add_argument("--dump-html", action="store_true", help="Save each page's HTML next to --out, on success too")
    p.add_argument("--headless", dest="headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def _default_out(fmt: str) -> str:
    return f"facebook_marketplace.{fmt}"


_resolve_urls = page_flow.resolve_urls  # the one implementation is page_flow's


async def _new_context(
    browser: Browser, proxy: Optional[Proxy], user_agent: Optional[str], *, reuse_default: bool = False,
) -> BrowserContext:
    """Over --cdp-endpoint, reuse the profile's own default context: it
    holds the cookies that make the profile worth reusing — the same fix
    shein-scraper needed live. A local browser gets a fresh isolated
    context per URL, so no cookie from one exit is replayed from another
    (CLAUDE.md §8)."""
    if reuse_default and browser.contexts:
        return browser.contexts[0]
    kwargs = {"locale": "en-US", "viewport": dict(lp.VIEWPORT)}  # the size decides whether Marketplace paginates (listing_parser.VIEWPORT)
    if proxy is not None:
        kwargs["proxy"] = proxy.playwright_proxy_dict()
    if user_agent:
        kwargs["user_agent"] = user_agent
    return await browser.new_context(**kwargs)


async def _close(page: Page, context: BrowserContext, *, reuse_default: bool) -> None:
    try:
        await page.close()
        if not reuse_default:
            await context.close()
    except Exception as exc:  # noqa: BLE001 — cleanup only
        log.debug("close failed: %s", exc)


async def _enable_scraping_browser_auto_solve(context: BrowserContext, page: Page) -> None:
    """Only meaningful over --cdp-endpoint — see the identical helper in
    every sibling repo's playwright_scraper.py."""
    try:
        session = await context.new_cdp_session(page)
        session.on("Captcha.detected", lambda *_: log.info("[Scraping Browser API] captcha detected"))
        session.on("Captcha.solveFinished", lambda *_: log.info("[Scraping Browser API] captcha solved"))
        session.on("Captcha.solveFailed", lambda *_: log.warning("[Scraping Browser API] captcha solve failed"))
        await session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
    except Exception as exc:  # noqa: BLE001 — optional enhancement, never fatal
        log.warning("Captcha.setAutoSolve unavailable on this CDP session (continuing without it): %s", exc)


async def _maybe_solve_captcha(
    *, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3,
) -> Optional[dict]:
    if policy == "off" or client is None:
        return None
    log.warning("Local captcha solving is disabled: token delivery is not implemented; no paid task created. "
                "Use the Scraping Browser CDP auto-solve integration.")
    return {"action": "unsupported_delivery"}


async def _connect_over_cdp(pw, cdp_endpoint: str):
    """Bounded, retried through a still-locked profile, and a 401 says the
    endpoint expired (scraper_api_client.connect_with_retry, CLAUDE.md §26).
    Credentials never reach the message."""
    return await scraper_api_client.connect_with_retry(
        lambda: pw.chromium.connect_over_cdp(cdp_endpoint), redact=redact_credentials, log=log,
    )


class _PlaywrightSession:
    """page_flow.PageSession over one Playwright page. The page's own
    pagination requests are kept, with their answers, for take_requests()."""

    def __init__(self, page: Page, context: BrowserContext, *, reuse_default: bool):
        self.page, self.context, self.reuse_default = page, context, reuse_default
        self._requests: list = []
        self._pending: set = set()
        page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        if GRAPHQL_PATH not in response.url or not lp.is_pagination_request(response.request.post_data):
            return
        task = asyncio.ensure_future(self._read(response))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _read(self, response) -> None:
        try:
            body = await response.text()
        except Exception as exc:  # noqa: BLE001 — a body gone with a navigation is not an error
            log.debug("Could not read a pagination answer: %s", exc)
            return
        self._requests.append((response.request.post_data, body))

    async def dismiss_dialog(self) -> bool:
        try:
            button = self.page.locator(DIALOG_CLOSE_SELECTOR).first
            if await button.count():
                await button.click(timeout=3000)
                return True
        except Exception as exc:  # noqa: BLE001 — no dialog, or it went by itself
            log.debug("No login dialog closed: %s", exc)
        return False

    async def wheel(self, pixels: int) -> None:
        size = self.page.viewport_size or {"width": 1280, "height": 720}
        await self.page.mouse.move(size["width"] // 2, size["height"] // 2)  # over the feed, not the header
        await self.page.mouse.wheel(0, pixels)

    async def take_requests(self) -> list:
        if self._pending:
            await asyncio.wait(list(self._pending), timeout=10)
        requests, self._requests = self._requests, []
        return requests

    async def post_form(self, data: str) -> tuple:
        status, body = await self.page.evaluate(POST_FORM_JS, data)
        return int(status), body

    async def goto(self, url: str) -> Optional[int]:
        response = await self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await self.page.wait_for_timeout(READINESS_WAIT_MS)
        return response.status if response is not None else None

    async def content(self) -> str:
        return await self.page.content()

    async def current_url(self) -> str:
        return self.page.url

    async def wait(self, seconds: float) -> None:
        await self.page.wait_for_timeout(int(seconds * 1000))


    async def close(self) -> None:
        await _close(self.page, self.context, reuse_default=self.reuse_default)


class _PlaywrightEngine:
    """page_flow.Engine: a page per URL on the run's browser. Over
    --cdp-endpoint the profile's default context is reused (its cookies are
    the point of reusing a profile)."""

    name = ENGINE_NAME
    readiness_s = READINESS_WAIT_MS / 1000
    can_paginate = True

    def __init__(self, browser: Browser, args: argparse.Namespace, *, autosolve: bool, user_agent: Optional[str], client):
        self.browser, self.args, self.autosolve, self.user_agent, self.client = browser, args, autosolve, user_agent, client

    async def open(self, proxy) -> _PlaywrightSession:
        reuse_default = bool(self.args.cdp_endpoint)
        context = await _new_context(self.browser, proxy, self.user_agent, reuse_default=reuse_default)
        page = await context.new_page()
        if self.autosolve:
            await _enable_scraping_browser_auto_solve(context, page)
        return _PlaywrightSession(page, context, reuse_default=reuse_default)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def solve_captcha(self, session, *, html: str, url: str):
        return await _maybe_solve_captcha(html=html, url=url, client=self.client, policy=self.args.solve_captcha,
                                          min_score=self.args.min_score)


async def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    args._solve_budget = page_flow.SolveBudget(args.max_solves)
    try:
        urls, skipped = _resolve_urls(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    stop = page_flow.validate_common(args, urls=urls, skipped=skipped, print_err=lambda m: print(m, file=sys.stderr))
    if stop is not None:
        return stop
    if args.scraper_api:
        args.out = args.out or _default_out(args.format)
        return await scraper_api_engine.run(args, urls=urls, started_at=started_at)
    if async_playwright is None:
        print(f"Error: playwright is not installed ({_PLAYWRIGHT_IMPORT_ERROR}). "
              f"pip install -r requirements-playwright.txt && playwright install chromium", file=sys.stderr)
        return EXIT_CRASH
    args.out = args.out or _default_out(args.format)

    try:
        proxies = load_proxies(args.proxy, args.proxy_file)
    except ProxyParseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    proxy_pool = ProxyPool(proxies, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries) if proxies else None

    client = None
    if args.twocaptcha_key and (args.solve_captcha != "off" or args.fingerprint):
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api)
    user_agent = None
    if args.fingerprint and not refuse_if_cdp(args.cdp_endpoint):
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)
                if user_agent:
                    log.info("Fingerprint applied: user agent %s", user_agent)

    try:
        async with async_playwright() as pw:
            if args.cdp_endpoint:
                if args.proxy or args.proxy_file:
                    log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
                    proxy_pool = None
                try:
                    browser = await _connect_over_cdp(pw, args.cdp_endpoint)
                except RuntimeError as exc:
                    log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
                    return page_flow.finish(args, products=[], blocked=False, remote_api_error=True, engine_name=ENGINE_NAME,
                                            urls=urls, started_at=started_at,
                                            pages_completed=0, failed_pages=[])
            else:
                browser = await pw.chromium.launch(headless=args.headless)
            engine = _PlaywrightEngine(browser, args, autosolve=bool(args.cdp_endpoint) and args.solve_captcha != "off",
                                       user_agent=user_agent, client=client)
            try:
                return await page_flow.run(engine, args, urls=urls,
                                           proxy_pool=proxy_pool, client=client, started_at=started_at)
            finally:
                await browser.close()
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args()
    args = env_config.apply_env(args)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
