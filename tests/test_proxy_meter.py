"""proxy_meter: the byte count against a stub proxy that serves known sizes,
the budget, and every engine's way of counting.

The stub proxy IS the site: it answers absolute-URI requests for
http://meter.test/... itself and counts every byte that crosses its sockets,
which is the ground truth the meter is compared with. Nothing leaves this
machine.

The real-browser tests run when that engine and a Chrome/Chromium binary are
present; with PROXY_METER_REQUIRE=<playwright|pyppeteer|selenium> (CI's
engine jobs) a missing engine or browser is a failure, not a skip.
"""
import argparse
import asyncio
import json
import os
import shutil
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy_meter  # noqa: E402

REQUIRE = os.environ.get("PROXY_METER_REQUIRE", "")
SITE = "http://meter.test/"
PAGES = {
    "/": ("text/html", b"<html><head><link rel='stylesheet' href='/style.css'><script src='/app.js'></script>"
          b"</head><body>" + b"<p>row</p>" * 2000
          + b"<script>fetch('/data.json').then(function (r) { return r.text(); })"
            b".then(function (t) { document.title = 'done ' + t.length; });</script></body></html>"),
    "/app.js": ("application/javascript", b"var a = 1;" * 5000),
    "/style.css": ("text/css", b"p { color: #123456; }" * 2000),
    "/data.json": ("application/json", b'{"k": "' + b"v" * 30000 + b'"}'),
}
PAGE_BYTES = sum(len(body) for _, body in PAGES.values())  # ~152 KB of bodies


class StubProxy:
    """A forward proxy that serves PAGES itself and counts bytes per path."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self.per_path = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._serve, daemon=True).start()

    def site_bytes(self):
        """Bytes in + out for the stub site's own paths (a browser's own
        background CONNECTs to its vendor are not the page's traffic)."""
        with self.lock:
            return sum(v for k, v in self.per_path.items() if k in PAGES or k.startswith("/"))

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        buf = b""
        try:
            while True:
                while b"\r\n\r\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                head, buf = buf.split(b"\r\n\r\n", 1)
                lines = head.split(b"\r\n")
                method, target = lines[0].decode("latin-1").split(" ")[:2]
                length = 0
                for line in lines[1:]:
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                while len(buf) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                received = len(head) + 4 + length
                buf = buf[length:]
                if method == "CONNECT":
                    path = target
                    body, status, ctype = b"", "403 Forbidden", "text/plain"
                else:
                    path = ("/" + target.split("://", 1)[1].split("/", 1)[1]) if "://" in target else target
                    path = path.split("?", 1)[0]
                    ctype, body = PAGES.get(path, ("text/plain", b"not found"))
                    status = "200 OK" if path in PAGES else "404 Not Found"
                response = ("HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nCache-Control: no-store\r\n"
                            "Connection: %s\r\n\r\n" % (status, ctype, len(body),
                                                        "close" if method == "CONNECT" else "keep-alive")).encode()
                if method != "HEAD":
                    response += body
                conn.sendall(response)
                with self.lock:
                    self.per_path[path] = self.per_path.get(path, 0) + received + len(response)
                if method == "CONNECT":
                    return
        except OSError:
            return
        finally:
            conn.close()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def chrome_binary():
    for candidate in (os.environ.get("CHROME_BIN"), "/usr/bin/chromium", "/usr/bin/chromium-browser",
                      "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
        if candidate and Path(candidate).exists():
            return candidate
    return shutil.which("chromium") or shutil.which("google-chrome")


def unavailable(test, engine, why):
    if REQUIRE == engine:
        test.fail("PROXY_METER_REQUIRE=%s but %s" % (engine, why))
    test.skipTest(why)


class Budget(unittest.TestCase):
    def test_flag_default_and_validation(self):
        parser = argparse.ArgumentParser()
        proxy_meter.add_budget_argument(parser)
        self.assertEqual(parser.parse_args([]).proxy_budget_mb, 10)
        self.assertEqual(parser.parse_args(["--proxy-budget-mb", "0"]).proxy_budget_mb, 0)
        self.assertEqual(parser.parse_args(["--proxy-budget-mb", "2.5"]).proxy_budget_mb, 2.5)
        for bad in ("-1", "x", "nan", "inf"):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--proxy-budget-mb", bad])

    def test_running_total_budget_and_callback(self):
        meter = proxy_meter.ProxyMeter(1, active=True)
        fired = []
        meter.on_exceeded(lambda: fired.append(1))
        self.assertFalse(meter.add(600_000))
        meter.check()
        self.assertFalse(meter.add(400_000), "exactly the budget is not over it")
        self.assertTrue(meter.add(1))
        meter.add(5)
        self.assertEqual(fired, [1], "the callback fires once")
        with self.assertRaises(proxy_meter.ProxyBudgetExceeded):
            meter.check()
        self.assertEqual(meter.sidecar(), {"proxy_bytes": 1_000_006, "proxy_budget_bytes": 1_000_000,
                                           "proxy_budget_exceeded": True})
        self.assertEqual(meter.summary_line(), "Proxy traffic: 1.0 MB of 1 MB budget")

    def test_zero_is_unlimited(self):
        meter = proxy_meter.ProxyMeter(0, active=True)
        meter.add(10 ** 12)
        self.assertFalse(meter.exceeded)
        self.assertEqual(meter.sidecar()["proxy_budget_bytes"], None)

    def test_inactive_meter_counts_nothing(self):
        meter = proxy_meter.ProxyMeter(0.001, active=False)
        meter.add(10 ** 9)
        self.assertEqual(meter.sidecar(), {"proxy_bytes": None, "proxy_budget_bytes": None,
                                           "proxy_budget_exceeded": False})
        self.assertFalse(meter.exceeded)

    def test_start_replaces_the_run_meter(self):
        meter = proxy_meter.start(3, active=True)
        self.assertIs(proxy_meter.current(), meter)
        self.assertEqual(meter.budget_bytes, 3_000_000)
        proxy_meter.start(None, active=False)
        self.assertEqual(proxy_meter.current().budget_bytes, 10_000_000)


class HttpClient(unittest.TestCase):
    def test_requests_through_the_stub_proxy_matches_its_count(self):
        import requests
        proxy = StubProxy()
        try:
            meter = proxy_meter.ProxyMeter(0, active=True)
            session = requests.Session()
            session.hooks["response"].append(meter.requests_hook)
            for path in PAGES:
                response = session.get(SITE + path.lstrip("/"), proxies={"http": proxy.url}, timeout=10)
                self.assertEqual(response.status_code, 200)
            session.post(SITE + "data.json", data=b"x" * 5000, proxies={"http": proxy.url}, timeout=10)
            time.sleep(0.2)
            truth = proxy.site_bytes()
            self.assertGreater(truth, PAGE_BYTES)
            self.assertLess(abs(meter.bytes - truth) / truth, 0.02, (meter.bytes, truth))
        finally:
            proxy.close()


class CdpEvents(unittest.TestCase):
    def test_loading_finished_and_request_sizes(self):
        meter = proxy_meter.ProxyMeter(0, active=True)
        meter.feed_cdp("Network.requestWillBeSent", {"requestId": "1", "request": {
            "method": "POST", "url": "http://x.test/a?b=1", "headers": {"A": "bc"}, "postData": "12345"}})
        sent = len("POST /a?b=1 HTTP/1.1\r\n") + 2 + len("A") + len("bc") + 4 + 5
        self.assertEqual(meter.bytes, sent)
        meter.feed_cdp("Network.responseReceived", {"requestId": "1", "response": {"status": 200}})
        meter.feed_cdp("Network.dataReceived", {"requestId": "1", "encodedDataLength": 400})
        meter.feed_cdp("Network.loadingFinished", {"requestId": "1", "encodedDataLength": 1000})
        self.assertEqual(meter.bytes, sent + 1000, "loadingFinished carries the whole length")
        meter.feed_cdp("Network.responseReceived", {"requestId": "2", "response": {"status": 200}})
        meter.feed_cdp("Network.dataReceived", {"requestId": "2", "encodedDataLength": 300})
        meter.feed_cdp("Network.loadingFailed", {"requestId": "2"})
        self.assertEqual(meter.bytes, sent + 1300, "a failed load counts what arrived")
        # Chrome's error page for a failed navigation: a finished "load" of
        # ~250 KB with no response, generated locally (seen through a refusing proxy).
        meter.feed_cdp("Network.loadingFailed", {"requestId": "e", "errorText": "net::ERR_TUNNEL_CONNECTION_FAILED"})
        meter.feed_cdp("Network.dataReceived", {"requestId": "e", "encodedDataLength": 0})
        meter.feed_cdp("Network.loadingFinished", {"requestId": "e", "encodedDataLength": 250609})
        meter.feed_cdp("Network.responseReceived", {"requestId": "c", "response": {"fromDiskCache": True}})
        meter.feed_cdp("Network.loadingFinished", {"requestId": "c", "encodedDataLength": 900})
        meter.feed_cdp("Network.requestWillBeSent", {"requestId": "n", "request": {
            "method": "GET", "url": "chrome://new-tab-page/x.js", "headers": {"A": "b"}}})
        meter.feed_cdp("Network.responseReceived", {"requestId": "n",
                                                    "response": {"url": "chrome://new-tab-page/x.js"}})
        meter.feed_cdp("Network.loadingFinished", {"requestId": "n", "encodedDataLength": 6575})
        self.assertEqual(meter.bytes, sent + 1300,
                         "an error page, a cache hit or a chrome:// page crossed no proxy")
        before = meter.bytes
        meter.feed_cdp("Network.requestWillBeSent", {"requestId": "3", "request": {"method": "GET", "url": "data:x"}})
        self.assertEqual(meter.bytes, before, "a data: URL never crosses the proxy")
        meter.feed_cdp("Network.requestWillBeSent", {"requestId": "4", "redirectResponse": {"encodedDataLength": 77},
                                                     "request": {"method": "GET", "url": "http://x.test/"}})
        self.assertEqual(meter.bytes, before + 77 + len("GET / HTTP/1.1\r\n") + 2)

    def test_selenium_log_is_teed_not_stolen(self):
        def entry(method, **params):
            return {"message": json.dumps({"message": {"method": method, "params": params}})}

        class Driver:
            def __init__(self):
                self.log = [entry("Network.responseReceived", requestId="a", response={}),
                            entry("Network.loadingFinished", requestId="a", encodedDataLength=600)]
                self.sent = []

            def get_log(self, kind):
                out, self.log = self.log, []
                return out

            def execute_cdp_cmd(self, cmd, params):
                self.sent.append((cmd, params))

        meter = proxy_meter.ProxyMeter(0.001, active=True)  # 1,000 bytes
        driver = Driver()
        meter.attach_selenium(driver, keep_entries=True)
        meter.poll_selenium(driver)
        self.assertEqual(meter.bytes, 600)
        driver.log += [entry("Network.responseReceived", requestId="b", response={}),
                       entry("Network.loadingFinished", requestId="b", encodedDataLength=500)]
        got = driver.get_log("performance")
        self.assertEqual(len(got), 4, "the engine still reads what the meter polled")
        self.assertEqual(meter.bytes, 1100)
        self.assertTrue(meter.exceeded)
        self.assertIn(("Network.setBlockedURLs", {"urls": ["*"]}), driver.sent)
        self.assertEqual(driver.get_log("performance"), [])
        inactive = proxy_meter.ProxyMeter(1, active=False)
        other = Driver()
        inactive.attach_selenium(other)
        self.assertFalse(hasattr(other, "_proxy_meter_poll"), "no proxy: the driver is left alone")

    def test_pyppeteer_session_and_block(self):
        class Client:
            def __init__(self):
                self.handlers, self.sent = {}, []

            def on(self, name, fn):
                self.handlers[name] = fn

            async def send(self, cmd, params=None):
                self.sent.append((cmd, params))

        client = Client()

        async def create():
            return client

        page = argparse.Namespace(target=argparse.Namespace(createCDPSession=create))
        meter = proxy_meter.ProxyMeter(0.001, active=True)

        async def go():
            await meter.attach_pyppeteer(page)
            client.handlers["Network.responseReceived"]({"requestId": "a", "response": {}})
            client.handlers["Network.loadingFinished"]({"requestId": "a", "encodedDataLength": 2000})
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(go())
        self.assertIn(("Network.enable", {}), client.sent)
        self.assertEqual(meter.bytes, 2000)
        self.assertIn(("Network.setBlockedURLs", {"urls": ["*"]}), client.sent)

    def test_pyppeteer_block_from_a_worker_thread(self):
        """The budget can trip on a thread with no event loop (a concurrent
        worker): the block is still sent, on the page's own loop."""
        sent = []

        class Client:
            def on(self, name, fn):
                pass

            async def send(self, cmd, params=None):
                sent.append(cmd)

        async def create():
            return Client()

        page = argparse.Namespace(target=argparse.Namespace(createCDPSession=create))
        meter = proxy_meter.ProxyMeter(0.001, active=True)

        async def go():
            await meter.attach_pyppeteer(page)
            worker = threading.Thread(target=meter.add, args=(5000,))
            worker.start()
            worker.join()
            for _ in range(5):
                await asyncio.sleep(0)

        asyncio.run(go())
        self.assertTrue(meter.exceeded)
        self.assertIn("Network.setBlockedURLs", sent)


class PlaywrightHooks(unittest.TestCase):
    def test_sizes_and_budget_route(self):
        class Request:
            method, url, headers, post_data_buffer = "GET", "http://x.test/", {"a": "b"}, None

            def sizes(self):
                return {"requestHeadersSize": 100, "requestBodySize": 0,
                        "responseHeadersSize": 200, "responseBodySize": 5000}

        class Failed(Request):
            def sizes(self):
                raise RuntimeError("no sizes for a failed request")

        class Route:
            def __init__(self):
                self.done = []

            def abort(self, reason=None):
                self.done.append("abort")

            def fallback(self):
                self.done.append("fallback")

        meter = proxy_meter.ProxyMeter(0.006, active=True)
        route = Route()
        meter.playwright_budget_route(route)
        meter.playwright_request_done(Request())
        self.assertEqual(meter.bytes, 5300)
        meter.playwright_request_done(Failed())
        self.assertEqual(meter.bytes, 5300 + len("GET / HTTP/1.1\r\n") + 2 + 1 + 1 + 4)
        meter.playwright_request_done(Request())
        self.assertTrue(meter.exceeded)
        meter.playwright_budget_route(route)
        self.assertEqual(route.done, ["fallback", "abort"])


def _assert_close(test, meter, proxy, tolerance=0.10):
    time.sleep(0.3)
    truth = proxy.site_bytes()
    test.assertGreater(truth, PAGE_BYTES, proxy.per_path)
    test.assertLess(abs(meter.bytes - truth) / truth, tolerance, (meter.bytes, truth, proxy.per_path))


class RealBrowsers(unittest.TestCase):
    """The engines' own byte reports against the stub proxy's count."""

    def test_playwright(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return unavailable(self, "playwright", "playwright is not installed")
        proxy = StubProxy()
        meter = proxy_meter.ProxyMeter(0, active=True)

        async def go():
            async with async_playwright() as pw:
                try:
                    browser = await pw.chromium.launch(headless=True)
                except Exception as exc:  # noqa: BLE001
                    return str(exc).splitlines()[0]
                context = await browser.new_context(proxy={"server": proxy.url})
                await meter.attach_playwright_async(context)
                page = await context.new_page()
                await page.goto(SITE)
                await page.wait_for_function("document.title.indexOf('done') === 0", timeout=10000)
                await page.wait_for_timeout(500)
                await context.close()
                await browser.close()
            return None

        try:
            failed = asyncio.run(go())
        finally:
            proxy.close()
        if failed:
            return unavailable(self, "playwright", "Chromium did not launch: %s" % failed)
        _assert_close(self, meter, proxy)

    def test_playwright_budget_aborts_further_requests(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return unavailable(self, "playwright", "playwright is not installed")
        proxy = StubProxy()
        meter = proxy_meter.ProxyMeter(0.05, active=True)  # 50 KB: spent by the first page
        errors = []

        async def go():
            async with async_playwright() as pw:
                try:
                    browser = await pw.chromium.launch(headless=True)
                except Exception as exc:  # noqa: BLE001
                    return str(exc).splitlines()[0]
                context = await browser.new_context(proxy={"server": proxy.url})
                await meter.attach_playwright_async(context)
                page = await context.new_page()
                await page.goto(SITE)
                await page.wait_for_timeout(1500)
                try:
                    await page.goto(SITE + "?second")
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
                await context.close()
                await browser.close()
            return None

        try:
            failed = asyncio.run(go())
        finally:
            proxy.close()
        if failed:
            return unavailable(self, "playwright", "Chromium did not launch: %s" % failed)
        self.assertTrue(meter.exceeded)
        self.assertTrue(errors and "ERR_BLOCKED_BY_CLIENT" in errors[0], errors)
        self.assertEqual(sum(1 for k in proxy.per_path if k == "/"), 1)

    def test_pyppeteer(self):
        try:
            from pyppeteer import launch
        except ImportError:
            return unavailable(self, "pyppeteer", "pyppeteer is not installed")
        binary = chrome_binary()
        if not binary:
            return unavailable(self, "pyppeteer", "no Chrome/Chromium binary (set CHROME_BIN)")
        proxy = StubProxy()
        meter = proxy_meter.ProxyMeter(0, active=True)

        async def go():
            browser = await launch(headless=True, executablePath=binary, handleSIGINT=False, handleSIGTERM=False,
                                   handleSIGHUP=False, args=["--no-sandbox", "--proxy-server=" + proxy.url])
            try:
                page = await browser.newPage()
                await meter.attach_pyppeteer(page)
                await page.goto(SITE)
                await page.waitForFunction("document.title.indexOf('done') === 0", {"timeout": 10000})
                await asyncio.sleep(0.5)
            finally:
                await browser.close()

        try:
            asyncio.run(go())
        finally:
            proxy.close()
        _assert_close(self, meter, proxy)

    def test_selenium(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
        except ImportError:
            return unavailable(self, "selenium", "selenium is not installed")
        binary = chrome_binary()
        if not binary:
            return unavailable(self, "selenium", "no Chrome/Chromium binary (set CHROME_BIN)")
        proxy = StubProxy()
        meter = proxy_meter.ProxyMeter(0, active=True)
        options = Options()
        for arg in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--proxy-server=" + proxy.url):
            options.add_argument(arg)
        options.binary_location = binary
        proxy_meter.selenium_logging_prefs(options)
        driver_path = os.environ.get("CHROMEDRIVER") or shutil.which("chromedriver")
        try:
            driver = webdriver.Chrome(service=Service(driver_path) if driver_path else Service(), options=options)
        except Exception as exc:  # noqa: BLE001
            proxy.close()
            return unavailable(self, "selenium", "chromedriver did not start: %s" % str(exc).splitlines()[0])
        try:
            meter.attach_selenium(driver, keep_entries=False)
            driver.get(SITE)
            deadline = time.time() + 10
            while not driver.title.startswith("done") and time.time() < deadline:
                time.sleep(0.2)
            time.sleep(0.5)
            meter.poll_selenium(driver)
        finally:
            driver.quit()
            proxy.close()
        _assert_close(self, meter, proxy)


if __name__ == "__main__":
    unittest.main()
