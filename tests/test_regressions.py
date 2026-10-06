"""Failure and recovery scenarios: exercise the shared flow without network or paid tasks."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import page_flow as flow
import playwright_scraper as playwright
import puppeteer_scraper as puppeteer
import selenium_scraper as selenium

FIX = Path(__file__).parent / 'fixtures'
SEARCH = (FIX / 'fb_mp_search_bike_nyc_20261006.html').read_text()
ITEM = (FIX / 'fb_mp_item_20261005.html').read_text()
URL = 'https://www.facebook.com/marketplace/nyc/search/?query=bike'
SHELL = '<script type="application/json">{"canonicalRouteName":"comet.fbweb.CometMarketplaceSearchRoute"}</script>'


class Session:
    def __init__(self, engine):
        self.engine, self.reads, self.url = engine, 0, ''

    async def goto(self, target):
        e = self.engine
        self.url = target
        e.gotos += 1
        if e.mode == 'navigation_error':
            raise RuntimeError('navigation failed')
        if e.mode == 'recover_once' and e.gotos == 1:
            raise RuntimeError('net::ERR_TIMED_OUT')
        if e.mode == 'detail_raises' and '/item/' in target:
            raise RuntimeError('Target closed')
        return 200

    async def content(self):
        self.reads += 1
        if self.engine.mode == 'content_races' and self.reads in (2, 3):
            raise RuntimeError('Execution context was destroyed, most likely because of a navigation')
        if self.engine.mode == 'content_races' and self.reads == 1:
            return SHELL
        if '/item/' in self.url:
            listing_id = self.url.split('/item/')[1].split('/')[0]
            return ITEM.replace('1800446554437755', listing_id)
        if self.engine.mode == 'bad_listing':
            return SEARCH.replace('"id":"1655081135947682"', '"id":"not-a-number"')
        return SEARCH

    async def current_url(self):
        return self.url

    async def wait(self, seconds):
        return None

    async def close(self):
        self.engine.closed += 1
        if self.engine.mode == 'close_raises':
            raise RuntimeError('browser has disconnected')


class Engine:
    name = 'fake'
    readiness_s = 0

    def __init__(self, mode):
        self.mode, self.gotos, self.closed = mode, 0, 0

    async def open(self, proxy):
        if self.mode == 'open_raises':
            raise RuntimeError('Browser.newContext: Target page, context or browser has been closed')
        return Session(self)

    async def sleep(self, seconds):
        return None

    async def solve_captcha(self, session, *, html, url):
        return None


def run_flow(mode, *extra):
    args = playwright.build_arg_parser().parse_args(['--url', URL, *extra])
    args._solve_budget = flow.SolveBudget(args.max_solves)
    engine = Engine(mode)
    with tempfile.TemporaryDirectory() as td:
        args.out = str(Path(td) / 'out.json')
        rc = asyncio.run(flow.run(engine, args, urls=[URL], proxy_pool=None, client=None, started_at=0.0))
        meta_p = Path(args.out + '.meta.json')
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else None
        rows = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else None
    return rc, meta, rows, engine


class FailureScenarios(unittest.TestCase):
    def test_navigation_error_is_bounded_and_reported(self):
        rc, meta, rows, engine = run_flow('navigation_error', '--retries', '2')
        self.assertEqual(rc, 5)
        self.assertEqual(engine.gotos, 3)
        self.assertEqual(engine.closed, 1)

    def test_one_navigation_error_recovers(self):
        rc, meta, rows, engine = run_flow('recover_once')
        self.assertEqual(rc, 0)
        self.assertEqual(meta['product_count'], 6)

    def test_content_racing_a_navigation_is_waited_out(self):
        rc, meta, rows, engine = run_flow('content_races')
        self.assertEqual(rc, 0)

    def test_a_malformed_listing_is_rejected_not_fatal(self):
        rc, meta, rows, engine = run_flow('bad_listing')
        self.assertEqual(rc, 6)
        self.assertEqual(meta['product_count'], 5)
        self.assertEqual(meta['rejected_rows'], 1)

    def test_a_detail_page_that_fails_keeps_the_search_row(self):
        rc, meta, rows, engine = run_flow('detail_raises', '--details', '--max-results', '2', '--retries', '0')
        self.assertEqual(rc, 6)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['price_source'] == 'search' for r in rows))

    def test_details_fill_every_row(self):
        rc, meta, rows, engine = run_flow('ok', '--details', '--max-results', '3')
        self.assertEqual(rc, 0)
        self.assertEqual([r['currency'] for r in rows], ['USD'] * 3)
        self.assertEqual(meta['details_read'], 3)

    def test_session_close_failure_is_not_a_crash(self):
        rc, meta, rows, engine = run_flow('close_raises')
        self.assertEqual(rc, 0)

    def test_browser_open_failure_is_fetch_error(self):
        rc, meta, rows, engine = run_flow('open_raises', '--allow-empty')
        self.assertEqual(rc, 5)
        self.assertEqual(meta['failed_urls'][0]['reason'], 'fetch_error')


class EngineParity(unittest.TestCase):
    def test_every_engine_parses_the_same_arguments(self):
        argv = ['--query', 'bike', '--location', 'nyc', '--min-price', '10', '--max-price', '90', '--sort', 'price_ascend',
                '--days-since-listed', '7', '--details', '--max-results', '5', '--format', 'csv']
        parsed = [vars(m.build_arg_parser().parse_args(argv)) for m in (playwright, selenium, puppeteer)]
        self.assertEqual(parsed[0], parsed[1])
        self.assertEqual(parsed[0], parsed[2])


if __name__ == '__main__':
    unittest.main()
