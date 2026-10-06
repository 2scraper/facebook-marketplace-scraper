#!/usr/bin/env python3
"""smoke_test.py — one file of plain functions with inline/synthetic-
fixture checks. No pytest, no conftest. `tests/test_smoke.py` wraps this as
a single pytest entry point so `pytest` also works, without a second copy
of the checks.

**What the fixtures are**: every fixture in `tests/fixtures/` comes from a
REAL logged-out capture of facebook.com/marketplace — keyword searches in
New York (plain and with a price range) and London, the New York
vehicles category, a search with no results, a listing's own page, and
the answer for an address that is gone. They are NOT verbatim: each is
rebuilt from the page's own JSON keeping only the paths listing_parser.py
reads, with their real values, and dropping session tokens and every
other field (see each file's header). Values are asserted against what
those captures hold, not just that a column is populated (CLAUDE.md §10).

Run directly: `python3 smoke_test.py`
"""
from __future__ import annotations

import asyncio
import inspect as _inspect
import json
import re
import tempfile
from pathlib import Path

import listing_parser as lp
import captcha_solver
import diff_runs
import env_config
import output_writer
import page_flow
import proxy_pool
import puppeteer_scraper
import scraper_api_client
import selenium_scraper

try:
    import playwright_scraper
except Exception as exc:  # pragma: no cover — this import itself must never fail
    raise AssertionError(f"playwright_scraper must import cleanly even without playwright installed: {exc}") from exc

ROOT = Path(__file__).parent

RESULTS = []  # (name, ok, detail)


def check(name):
    """Runs the decorated function IMMEDIATELY (at module-load time) and
    records the outcome — same pattern as every other family member's
    smoke_test.py; every check function is named `_` because only RESULTS
    is ever read, nothing looks a check up by name."""
    def decorator(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc)))
        except Exception as exc:  # a check that crashes is still a failure, not an uncaught traceback
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        except SystemExit as exc:  # a CLI helper exiting inside a check would otherwise end the whole suite silently
            RESULTS.append((name, False, f"SystemExit: {exc}"))
        return fn
    return decorator


def asyncio_run_maybe(mod, args):
    """playwright_scraper.run()/puppeteer_scraper.run() are coroutines;
    selenium_scraper.run() is plain sync."""
    result = mod.run(args)
    if _inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


_ENGINES = (playwright_scraper, selenium_scraper, puppeteer_scraper)
_FIX = ROOT / "tests" / "fixtures"


def _fx(name):
    path = next(_FIX.glob(f"fb_mp_{name}_2026100?.*"))
    return path.read_text(encoding="utf-8")


_PAGES = ("search_bike_nyc", "search_bike_nyc_price", "search_sofa_london", "vehicles_nyc", "search_empty", "item", "not_found")
_BIKE_URL = "https://www.facebook.com/marketplace/nyc/search/?query=bike"
_BIKE_IDS = ["1655081135947682", "4565211807048416", "2228196147757925", "1872601297420384", "1388456549659831", "2465182687340007"]
_ITEM_ID = "1800446554437755"
_ITEM_URL = "https://www.facebook.com/marketplace/item/1800446554437755/"
_SAPI_ARGV = ["--url", "https://www.facebook.com/marketplace/nyc/search/?query=bike"]
_CHROMIUM_ERROR = ('<html><head><title>www.facebook.com</title></head><body><div id="main-message">'
                   '<h1>This site can’t be reached</h1><div class="error-code">ERR_TIMED_OUT</div></div></body></html>')


# --------------------------------------------------------------------------- #
# Engine import/CLI hygiene (CLAUDE.md §6)
# --------------------------------------------------------------------------- #
@check("engines import cleanly regardless of installed drivers")
def _():
    for mod in _ENGINES:
        assert hasattr(mod, "build_arg_parser")
        assert hasattr(mod, "run")


@check("each engine imports its driver at MODULE level, guarded by try/except ImportError (an ast walk, not a substring: a driver imported inside a function makes the no-driver skip meaningless — CLAUDE.md §10)")
def _():
    import ast as _ast
    drivers = {"playwright_scraper": "playwright", "selenium_scraper": "selenium", "puppeteer_scraper": "pyppeteer"}
    for mod in _ENGINES:
        tree = _ast.parse((ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8"))
        guarded = [n for n in tree.body if isinstance(n, _ast.Try)
                   and any(isinstance(h.type, _ast.Name) and h.type.id == "ImportError" for h in n.handlers)
                   and any(isinstance(s, _ast.ImportFrom) and (s.module or "").split(".")[0] == drivers[mod.__name__]
                           or isinstance(s, _ast.Import) and any(a.name.split(".")[0] == drivers[mod.__name__] for a in s.names)
                           for s in n.body)]
        assert guarded, f"{mod.__name__}: no module-level guarded import of {drivers[mod.__name__]}"


@check("no module uses a name it never imports, defines or assigns (compileall proves a file PARSES, not that its names RESOLVE — CLAUDE.md §10)")
def _():
    import ast as _ast
    import builtins
    for path in sorted(ROOT.glob("*.py")):
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        bound = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
            elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                bound.add(node.name)
                a = node.args if not isinstance(node, _ast.ClassDef) else None
                if a is not None:
                    bound |= {x.arg for x in a.args + a.kwonlyargs + a.posonlyargs}
                    bound |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
            elif isinstance(node, _ast.Name) and isinstance(node.ctx, (_ast.Store, _ast.Del)):
                bound.add(node.id)
            elif isinstance(node, _ast.arg):
                bound.add(node.arg)
            elif isinstance(node, _ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, _ast.alias):
                bound.add((node.asname or node.name).split(".")[0])
        used = {n.id for n in _ast.walk(tree) if isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Load)}
        assert not (used - bound), f"{path.name}: unresolved names {sorted(used - bound)}"


@check("no forbidden overclaiming wording in any shipped .py/.md/.yml file")
def _():
    # Built from pieces so this file can be scanned too (CLAUDE.md §22: the
    # check used to exempt its own file, where the phrases sat verbatim).
    anti = "anti" + "detect"
    banned = (
        "cloud" + " browser", anti + " browser", "2scraper " + anti + " browser",
        "gate." + "2prx.com", "--" + anti, anti + "_local_api",
    )
    exempt_names = {"CLAUDE.md"}
    venvs = {p.parent for p in ROOT.rglob("pyvenv.cfg")}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in (".py", ".md", ".html", ".toml", ".cfg", ".yml", ".yaml"):
            continue
        if path.name in exempt_names or path.name.startswith("2scraper"):
            continue
        if ".git" in path.parts or "__pycache__" in path.parts or any(v in path.parents for v in venvs):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for phrase in banned:
            assert phrase not in text, f"{path.relative_to(ROOT)}: contains banned phrase {phrase!r}"


@check("nothing shipped still names the repo this one was bootstrapped from (facebook-pages-scraper's module, flags, output names or site)")
def _():
    # Mentions of the companion repo (facebook-ads-scraper, the Ad Library) are
    # legitimate cross-references; what must not survive is its CODE.
    leftovers = ("ad_parser", "page_parser", "run_state", "INSTAGRAM_", "instagram_results", "facebook_ads.",
                 "facebook_pages.", "--posts", "--region", "--active-status", "AdLibrary", "profile_header_renderer")
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in (".py", ".md", ".html", ".toml", ".yml", ".txt", ".example", ""):
            continue
        if ".git" in path.parts or "__pycache__" in path.parts or "fixtures" in path.parts or path.name == "smoke_test.py":
            continue
        if any(part.endswith(".egg-info") or part in ("build", "dist") or part.startswith(".venv") for part in path.parts):
            continue  # build artefacts, git-ignored and never shipped
        text = path.read_text(encoding="utf-8", errors="ignore")
        for word in leftovers:
            assert word not in text, f"{path.relative_to(ROOT)}: still says {word!r}"


@check("all three engines expose the identical --flag set (CLAUDE.md §4)")
def _():
    def flag_set(mod):
        return {opt for a in mod.build_arg_parser()._actions for opt in a.option_strings if opt.startswith("--")}

    pw, se, pu = flag_set(playwright_scraper), flag_set(selenium_scraper), flag_set(puppeteer_scraper)
    all_engines = pw | se | pu
    for name, flags in (("playwright_scraper", pw), ("selenium_scraper", se), ("puppeteer_scraper", pu)):
        missing = all_engines - flags
        assert not missing, f"{name} is missing {sorted(missing)} that (an)other engine(s) define — flag sets have drifted apart"


@check("all three engines share the same default output filename stem and the same defaults")
def _():
    for mod in _ENGINES:
        assert mod._default_out("json") == "facebook_marketplace.json"
        a = mod.build_arg_parser().parse_args(["--query", "bike", "--location", "nyc"])
        assert (a.max_solves, a.delay_between_pages, a.retries, a.max_results, a.details, a.sort) == (8, 2.0, 2, 100, False, None), mod.__name__


@check("every top-level module is in the Dockerfile COPY and pyproject py-modules (a module left out breaks the image on every run — CLAUDE.md §16)")
def _():
    if not (ROOT / "Dockerfile").exists() and not (ROOT / "pyproject.toml").exists():
        return  # the Docker image's own copy of this suite ships neither (CLAUDE.md §22)
    modules = sorted(pth.stem for pth in ROOT.glob("*.py"))
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    listed = set(re.findall(r'"([a-z_]+)"', pyproject.split("py-modules", 1)[1].split("]", 1)[0]))
    for mod in modules:
        assert f"{mod}.py" in docker, f"{mod}.py missing from the Dockerfile COPY"
        assert mod in listed, f"{mod} missing from pyproject py-modules"
    assert listed <= set(modules), f"pyproject lists modules that do not exist: {sorted(listed - set(modules))}"


@check("BOT_CHALLENGE_MARKERS is empty, and the generic markers match NONE of the real captures (CLAUDE.md §18: count a marker on a good page first)")
def _():
    assert lp.BOT_CHALLENGE_MARKERS == (), "no captcha vendor appeared on any Marketplace capture"
    for name in _PAGES:
        html = _fx(name)
        assert not captcha_solver.detect_from_html(html, lp.BOT_CHALLENGE_MARKERS), f"{name}: a generic marker fires on a real Marketplace page"
        assert not page_flow.is_challenge(html), name


# --------------------------------------------------------------------------- #
# output_writer — exit codes / precedence / dedupe (CLAUDE.md §9)
# --------------------------------------------------------------------------- #
@check("exit codes and STATUS_BY_EXIT match the family contract exactly")
def _():
    expected = {0: "complete", 1: "crashed", 2: "bad_usage", 3: "blocked", 4: "empty", 5: "remote_api_error", 6: "partial"}
    assert output_writer.STATUS_BY_EXIT == expected


def _mk_product(sku, price=None, **kw):
    defaults = dict(
        sku=sku, source="facebook.com", category="listing", title="An example listing",
        brand=None, price=price, currency=None, price_source=None,
        product_url=f"https://www.facebook.com/marketplace/item/{sku}/",
        image_url=None, scraped_at="2026-10-06T00:00:00Z",
        listing_id=sku, city="New York", is_sold=False,
    )
    defaults.update(kw)
    return output_writer.Product(**defaults)


@check("finish_run: rows gathered by a run that did not finish are PARTIAL (6) with the cause in stop_reason — never 5/3 with a file (CLAUDE.md §25; audit 2026-09-30 got exit 5 AND a written file). Rewrites the old pinned 'exit 5 with products' position deliberately.")
def _():
    cases = (
        (dict(blocked=True, remote_api_error=True), "remote_api_error"),
        (dict(blocked=False, remote_api_error=True), "remote_api_error"),
        (dict(blocked=True, remote_api_error=False), "blocked"),
        (dict(blocked=False, remote_api_error=False, failed_pages=[3]), "failed_pages"),
        (dict(blocked=False, remote_api_error=False, rejected_rows=2), "rejected_rows"),
    )
    for kw, reason in cases:
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            kw = {"failed_pages": None, **kw}
            code = output_writer.finish_run(
                products=[_mk_product("1")], out_path=out, fmt="json", engine="test", url="u",
                pages_requested=3, pages_completed=2, allow_empty=False, started_at=0.0, **kw,
            )
            assert code == output_writer.EXIT_PARTIAL, (kw, code)
            assert Path(out).exists(), "already-collected products must still be written out"
            meta = json.loads(Path(f"{out}.meta.json").read_text())
            assert meta["status"] == "partial" and meta["stop_reason"] == reason, (kw, meta)
            if reason == "rejected_rows":
                assert meta["rejected_rows"] == 2
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "z.json")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=0,
            failed_pages=None, blocked=False, remote_api_error=True, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_REMOTE_API_ERROR and not Path(out).exists(), "5 promises no file"

@check("finish_run precedence: blocked+zero-products respects --allow-empty for WHETHER to write, never for the STATUS")
def _():
    with tempfile.TemporaryDirectory() as td:
        out_a = str(Path(td) / "a.json")
        code = output_writer.finish_run(
            products=[], out_path=out_a, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=True, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert Path(out_a).exists(), "--allow-empty means a zero-product outcome DOES get written"
        meta = json.loads(Path(f"{out_a}.meta.json").read_text())
        assert meta["status"] == "blocked", "--allow-empty must never launder this into 'complete'"

        out_b = str(Path(td) / "b.json")
        code = output_writer.finish_run(
            products=[], out_path=out_b, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert not Path(out_b).exists(), "without --allow-empty, a zero-product outcome writes nothing"


@check("finish_run: zero products without --allow-empty writes neither file nor sidecar")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_ZERO_PRODUCTS
        assert not Path(out).exists()
        assert not Path(f"{out}.meta.json").exists()


@check("finish_run: partial (failed pages, some products) writes output and reports EXIT_PARTIAL")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=2, pages_completed=1, failed_pages=[2],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_PARTIAL
        assert Path(out).exists()
        meta = json.loads(Path(f"{out}.meta.json").read_text())
        assert meta["status"] == "partial"


@check("finish_run: a clean run with products writes output and reports EXIT_OK/complete")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a"), _mk_product("b")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_OK
        data = json.loads(Path(out).read_text())
        assert len(data) == 2


@check("merge_pages dedupes by sku, last-write-wins, in fetch order not arrival order")
def _():
    batch1 = [_mk_product("a", title="A v1"), _mk_product("b", title="B")]
    batch2 = [_mk_product("a", title="A v2"), _mk_product("c", title="C")]  # "a" edited between runs
    merged = output_writer.merge_pages([batch1, batch2])
    skus = [p.sku for p in merged]
    assert skus == ["a", "b", "c"], f"expected batch-order with new items appended, got {skus}"
    a = next(p for p in merged if p.sku == "a")
    assert a.title == "A v2", "later batch's value must win for a repeated sku"


@check("write_csv writes a header even for zero rows")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.csv")
        output_writer.write_csv([], out)
        text = Path(out).read_text()
        assert text.strip() != ""
        assert "sku" in text.splitlines()[0]


# --------------------------------------------------------------------------- #
# proxy_pool — parsing, redaction, dead-marking (family-shared, no site knowledge)
# --------------------------------------------------------------------------- #
@check("proxy_pool rejects a malformed proxy string with ProxyParseError")
def _():
    try:
        proxy_pool.load_proxies("not a proxy!!", None)
        raise AssertionError("expected ProxyParseError")
    except proxy_pool.ProxyParseError:
        pass


@check("proxy_pool parses a credentialed proxy and masks it in logs")
def _():
    proxies = proxy_pool.load_proxies("http://user:secretpass@host.example:8080", None)
    assert len(proxies) == 1
    p = proxies[0]
    assert p.has_auth
    masked = p.masked()
    assert "secretpass" not in masked
    assert "host.example" in masked


@check("proxy_pool.redact_credentials strips login:password out of an arbitrary string")
def _():
    raw = "connect failed: ws://myuser:mysecret@cb.2captcha.com:9222 (5 attempts)"
    redacted = proxy_pool.redact_credentials(raw)
    assert "mysecret" not in redacted
    assert "myuser" not in redacted


# --------------------------------------------------------------------------- #
# captcha_solver — generic + widget-specific detection (family-shared)
# --------------------------------------------------------------------------- #
@check("captcha_solver.detect_from_html finds generic bot-challenge markers")
def _():
    assert captcha_solver.detect_from_html("<html>please complete the g-recaptcha below</html>")
    assert not captcha_solver.detect_from_html("<html><body>ordinary page, no widgets</body></html>")


@check("captcha_solver.identify_widget extracts a Turnstile sitekey")
def _():
    html = '<div class="cf-turnstile" data-sitekey="0x4AAA_example"></div>'
    signal = captcha_solver.identify_widget(html)
    assert signal is not None
    assert signal.captcha_type == captcha_solver.CaptchaType.CLOUDFLARE_TURNSTILE
    assert signal.sitekey == "0x4AAA_example"


# --------------------------------------------------------------------------- #
# env_config — FACEBOOK_* keys, placeholder detection, precedence
# --------------------------------------------------------------------------- #
@check("env_config.ENV_KEYS matches .env.example exactly, in both directions")
def _():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = {line.split("=", 1)[0] for line in example.splitlines() if "=" in line and not line.startswith("#")}
    assert documented == set(env_config.ENV_KEYS), (documented, set(env_config.ENV_KEYS))


@check("env_config uses FACEBOOK_ prefixed keys, not a leftover key of the repo this was bootstrapped from")
def _():
    for key in env_config.ENV_KEYS:
        assert key == "TWOCAPTCHA_KEY" or key.startswith("FACEBOOK_"), f"unexpected env key {key!r}"


@check("env_config._is_placeholder treats a braced {...} fragment as unset")
def _():
    assert env_config._is_placeholder("")
    assert env_config._is_placeholder(None)
    assert env_config._is_placeholder("{login}-zone-scraping_browser:{password}@cb.2captcha.com")
    assert not env_config._is_placeholder("a-real-looking-value-123")


@check("env_config.apply_env never overrides an explicitly-set CLI flag")
def _():
    import os as _os
    ns = __import__("argparse").Namespace(proxy="http://explicit:pass@host:1")
    _os.environ["FACEBOOK_PROXY"] = "http://from-env:pass@host:2"
    try:
        env_config.apply_env(ns, dotenv_path="/nonexistent/.env")
        assert ns.proxy == "http://explicit:pass@host:1"
    finally:
        del _os.environ["FACEBOOK_PROXY"]


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# listing_parser — inputs
# --------------------------------------------------------------------------- #
@check("search_url: a keyword search or a category in a location, with the site's own filter names (minPrice, maxPrice, sortBy, daysSinceListed); a bad location, both or neither of query/category, a bad sort, days or price range are refused")
def _():
    from urllib.parse import parse_qs, urlparse
    assert lp.search_url(location="nyc", query=" bike ") == _BIKE_URL
    u = lp.search_url(location="london", query="sofa", min_price=100, max_price=300, sort="price_ascend", days_since_listed=7)
    assert urlparse(u).path == "/marketplace/london/search/" and parse_qs(urlparse(u).query) == {
        "query": ["sofa"], "minPrice": ["100"], "maxPrice": ["300"], "sortBy": ["price_ascend"], "daysSinceListed": ["7"]}, u
    assert lp.search_url(location="nyc", category="vehicles") == "https://www.facebook.com/marketplace/nyc/vehicles/"
    assert "sortBy" not in lp.search_url(location="nyc", query="x", sort="best_match"), "the default sort is the site's own URL"
    for kw in (dict(location="", query="x"), dict(location="item", query="x"), dict(location="nyc"), dict(location="nyc", query="x", category="y"),
               dict(location="nyc", query="x", sort="cheapest"), dict(location="nyc", query="x", days_since_listed=2),
               dict(location="nyc", query="x", min_price=-1), dict(location="nyc", query="x", min_price=10, max_price=5),
               dict(location="new york", query="x")):
        try:
            lp.search_url(**kw)
            raise AssertionError(f"accepted {kw}")
        except ValueError:
            pass


@check("normalize_input: a pasted search (filters kept, tracking dropped), a category page and a listing URL (any tab, any host) become ONE address; the bare location page (it embeds no listings), other Facebook pages and other hosts are refused WITH the reason")
def _():
    assert lp.normalize_input("https://www.facebook.com/marketplace/nyc/search?query=bike&ref=x&referral_code=y") == _BIKE_URL
    assert lp.normalize_input("facebook.com/marketplace/nyc/search/?query=bike&minPrice=100&sortBy=price_ascend") == \
        _BIKE_URL + "&minPrice=100&sortBy=price_ascend"
    assert lp.normalize_input("https://m.facebook.com/marketplace/item/1800446554437755/?ref=search") == _ITEM_URL
    assert lp.normalize_input("https://www.facebook.com/marketplace/nyc/vehicles") == "https://www.facebook.com/marketplace/nyc/vehicles/"
    for bad in ("https://www.facebook.com/marketplace/nyc/", "https://www.facebook.com/marketplace/", "https://www.facebook.com/nike",
                "https://www.facebook.com/marketplace/nyc/search/", "https://example.com/marketplace/item/1800446554437755/",
                "https://www.facebook.com/marketplace/item/abc/", "", "bike"):
        assert lp.normalize_input(bad) is None, bad
    assert "embeds no listings" in lp.refusal_reason("https://www.facebook.com/marketplace/nyc/")
    assert "not a facebook.com/marketplace" in lp.refusal_reason("https://www.facebook.com/nike")
    assert lp.classify(_ITEM_URL) == ("item", _ITEM_ID) and lp.classify(_BIKE_URL) == ("search", "nyc")
    assert lp.fetch_url(_BIKE_URL) == _BIKE_URL + "&locale=en_US" and lp.fetch_url(_ITEM_URL) == _ITEM_URL + "?locale=en_US"


# --------------------------------------------------------------------------- #
# listing_parser — the REAL captures
# --------------------------------------------------------------------------- #
@check("page_state on every REAL capture: listings = content (a search, a category, a listing's page); the site's own 'No listings found' unit = empty; the error route = not_found; a Marketplace route with no feed = loading; a login redirect = login; Chromium's own error page = unknown")
def _():
    for name in ("search_bike_nyc", "search_bike_nyc_price", "search_sofa_london", "vehicles_nyc", "item"):
        assert lp.page_state(_fx(name)) == "content", name
    assert lp.page_state(_fx("search_empty")) == "empty"
    assert lp.page_state(_fx("not_found")) == "not_found"
    shell = '<script type="application/json">{"canonicalRouteName":"comet.fbweb.CometMarketplaceSearchRoute"}</script>'
    assert lp.page_state(shell) == "loading"
    assert lp.page_state("<html><title>Log in</title></html>", final_url="https://www.facebook.com/login/?next=x") == "login"
    assert lp.page_state(_CHROMIUM_ERROR) == "unknown"


def _rows(name):
    return [lp.listing_row(n, search="s", position=i, scraped_at="x") for i, n in enumerate(lp.listings(lp.json_blocks(_fx(name))), 1)]


@check("listing_row on the REAL New York search: values, not coverage (CLAUDE.md §10) — the site's order, price as a number, the displayed price kept, city/state, listed time in UTC, delivery types; currency left None for a bare '$' (the search does not say which dollar)")
def _():
    rows = _rows("search_bike_nyc")
    assert [r.listing_id for r in rows] == _BIKE_IDS and [r.position for r in rows] == list(range(1, 7))
    r = rows[1]
    assert (r.sku, r.source, r.category, r.title) == ("facebook-listing-4565211807048416", "facebook.com", "listing", "Trek FX 2 Hybrid Bike Gen 4")
    assert (r.price, r.currency, r.price_text, r.price_source, r.brand) == (620.0, None, "$620", "search", None)
    assert (r.city, r.state, r.location_text) == ("North Bergen", "NJ", "North Bergen, New Jersey")
    assert (r.listed_at, r.is_sold, r.is_pending, r.category_id) == ("2026-10-04T01:45:36Z", False, False, "1658310421102081")
    assert r.product_url == "https://www.facebook.com/marketplace/item/4565211807048416/" and r.image_url.startswith("https://scontent")
    assert json.loads(rows[2].delivery_types_json) == ["IN_PERSON", "DOOR_PICKUP"]
    assert all(x.description is None and x.condition is None for x in rows), "the search alone carries no detail"


@check("the REAL price-filtered search (minPrice=100, maxPrice=300, cheapest first) honoured the filter; London prices are GBP from the unambiguous £, free items are 0.0 not None; vehicles carry their mileage subtitle and a struck-out earlier price")
def _():
    priced = _rows("search_bike_nyc_price")
    assert [r.price for r in priced] == [100.0] * 4 and all(100 <= r.price <= 300 for r in priced)
    london = _rows("search_sofa_london")
    assert {(r.currency, r.price, r.price_text) for r in london} == {("GBP", 0.0, "£0")} and london[0].state is None
    assert london[2].city == "Ilford" and len(json.loads(london[2].delivery_types_json)) == 4
    cars = _rows("vehicles_nyc")
    assert [r.subtitle for r in cars] == ["1K miles", "7K miles", "73K miles"]
    assert (cars[0].price, cars[0].original_price) == (15.0, 20.0) and cars[1].price == 1100.0 and cars[1].price_text == "$1,100"


@check("a price sort with daysSinceListed is flagged up front as a known-empty search (measured: 'No listings found' for both price sorts, New York and London; each filter alone and other sorts returned listings)")
def _():
    for sort in ("price_ascend", "price_descend"):
        assert lp.known_empty_combination(lp.search_url(location="nyc", query="bike", sort=sort, days_since_listed=7))
    for kw in (dict(sort="price_ascend"), dict(days_since_listed=7), dict(sort="creation_time_descend", days_since_listed=7),
               dict(sort="distance_ascend", days_since_listed=30)):
        assert lp.known_empty_combination(lp.search_url(location="nyc", query="bike", **kw)) is None, kw


@check("pagination helpers on the REAL captured request and answer: the page's own form is recognised by its query name; the next request changes ONLY the cursor; page_info reads has_next/cursor; a rate-limit answer is recognised")
def _():
    form = (_FIX / "fb_mp_pagination_form_20261006.txt").read_text(encoding="utf-8").splitlines()[-1]
    answer = (_FIX / "fb_mp_pagination_answer_20261006.txt").read_text(encoding="utf-8")
    assert lp.is_pagination_request(form) and not lp.is_pagination_request("fb_api_req_friendly_name=SomethingElse&variables={}")
    has_next, cursor = lp.page_info(lp.graphql_payloads(answer))
    assert has_next is True and cursor and cursor.startswith('{"pg":')
    from urllib.parse import parse_qsl
    before, after = dict(parse_qsl(form)), dict(parse_qsl(lp.next_request(form, "NEXT")))
    assert {k for k in before if before[k] != after[k]} == {"variables"}
    vb, va = json.loads(before["variables"]), json.loads(after["variables"])
    assert va["cursor"] == "NEXT" and {k: v for k, v in va.items() if k != "cursor"} == {k: v for k, v in vb.items() if k != "cursor"}
    assert vb["params"]["bqf"]["query"] == "bike", "the search's own params travel with every page"
    assert lp.next_request("a=1&b=2", "x") is None
    assert [n["id"] for n in lp.listings(lp.graphql_payloads(answer))][:2] == ["1632021981833671", "1738292700784721"]
    assert lp.is_rate_limited('{"errors":[{"message":"Rate limit exceeded","severity":"CRITICAL"}]}') and not lp.is_rate_limited(answer)
    assert lp.page_info(lp.json_blocks(_fx("search_bike_nyc")))[0] is not False, "the embedded 24 do not say they are the end"


@check("currency_from_symbol names a currency only when the symbol names ONE: £/€/zł/R$ yes; $, ¥, kr no")
def _():
    for text, code in (("£0", "GBP"), ("€50", "EUR"), ("120 zł", "PLN"), ("R$ 90", "BRL"), ("CA$40", "CAD"), ("₹500", "INR")):
        assert lp.currency_from_symbol(text) == code, text
    for text in ("$110", "¥3000", "200 kr", None, "", "110"):
        assert lp.currency_from_symbol(text) is None, text


@check("a listing's own REAL page: description, the currency code (USD — the search's '$' alone could not say), category, condition, location text, photos, shipping and status; matched on the listing's id, never the first listing on the page")
def _():
    r = lp.item_row(_fx("item"), _ITEM_ID, scraped_at="x")
    assert (r.sku, r.title, r.price, r.currency, r.price_source) == ("facebook-listing-1800446554437755", "Schwinn Mountain Bike", 110.0, "USD", "item_page")
    assert r.description == "Great condition haven’t been used \nComes with helmet \nGreat present"
    assert (r.category_name, r.category_slug, r.condition, r.status) == ("Bicycles", "bicycles", "Used - like new", "AVAILABLE")
    assert (r.location_text, r.shipping_offered, r.listed_at) == ("Emerson, NJ", False, "2026-10-04T23:20:17Z")
    assert len(json.loads(r.photo_urls_json)) == 1 and r.image_url == json.loads(r.photo_urls_json)[0]
    assert json.loads(r.attributes_json) == [{"name": "Condition", "value": "Used - like new"}]
    assert lp.item_row(_fx("item"), "999999999999", scraped_at="x") is None, "another id is no row, never the page's first listing"


@check("the seller is never collected: no seller column exists, and a listing that DOES carry one (17 of 370 captured listings named a private seller) gives a row without a trace of it")
def _():
    assert not any("seller" in f for f in output_writer.PRODUCT_FIELD_NAMES)
    nodes = lp.listings(lp.json_blocks(_fx("search_bike_nyc_price")))
    carrying = [n for n in nodes if isinstance(n.get("marketplace_listing_seller"), dict)]
    assert carrying, "the fixture keeps one seller-carrying listing (redacted) so this check has something to catch"
    for n in carrying:
        row = lp.listing_row(n, search="s", position=1, scraped_at="x")
        assert "REDACTED" not in json.dumps(vars(row)), "a seller field leaked into a row"


@check("Product field order: family-common fields first, Marketplace fields after")
def _():
    expected_head = ["sku", "source", "category", "title", "brand", "price", "currency",
                     "price_source", "product_url", "image_url", "scraped_at"]
    assert output_writer.PRODUCT_FIELD_NAMES[: len(expected_head)] == expected_head
    tail = output_writer.PRODUCT_FIELD_NAMES[len(expected_head):]
    for name in ("listing_id", "price_text", "original_price", "city", "state", "location_text", "listed_at", "is_sold",
                 "is_pending", "status", "category_id", "subtitle", "delivery_types_json", "description", "category_name",
                 "category_slug", "condition", "attributes_json", "photo_urls_json", "shipping_offered", "search_url", "position"):
        assert name in tail, f"{name} missing from Product's site-specific tail"


@check("fixtures are scrubbed and stay scrubbed: no session token, no seller but the REDACTED placeholder, no link-shim signature — guarded by PATTERN (CLAUDE.md §10)")
def _():
    token = re.compile(r'["\']?(?:fb_dtsg|lsd|jazoest|__hsi|DTSGInitialData)["\']?\s*[:=]\s*["{]')
    seller = re.compile(r'"marketplace_listing_seller":\{(?!"__typename":"User","name":"REDACTED-not-verbatim")')
    shim = re.compile(r"[?&]h=(?!REDACTED)[A-Za-z0-9_-]{8,}")
    files = sorted(_FIX.glob("*"))
    assert len(files) == 9, [f.name for f in files]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for name, pattern in (("token", token), ("seller", seller), ("link-shim signature", shim)):
            m = pattern.search(text)
            assert m is None, f"{path.name}: {name} {m.group(0)[:60]!r}"


# --------------------------------------------------------------------------- #
# CLI validation — bad usage never crashes, never writes output
# --------------------------------------------------------------------------- #
def _urls_file(lines):
    f = Path(tempfile.mkdtemp()) / "urls.txt"
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


@check("each engine resolves --query/--category + --location into ONE search URL; --url / --urls-file take pasted searches, categories and listings; a non-Marketplace line is skipped (never fetched); duplicates collapse")
def _():
    for mod in _ENGINES:
        args = mod.build_arg_parser().parse_args(["--query", "bike", "--location", "nyc"])
        assert mod._resolve_urls(args) == ([_BIKE_URL], 0), mod.__name__
        args = mod.build_arg_parser().parse_args(["--urls-file", str(_urls_file([
            "# a comment", _BIKE_URL, _BIKE_URL + "&ref=x", _ITEM_URL, "https://www.facebook.com/marketplace/nyc/", "https://www.facebook.com/nike"]))])
        assert mod._resolve_urls(args) == ([_BIKE_URL, _ITEM_URL], 2), mod.__name__


@check("each engine: no input, --query without --location, --query with --url, a bad --sort/--days-since-listed/price range, an input with nothing usable and an --out directory that does not exist are all EXIT_BAD_USAGE before anything launches, and write nothing")
def _():
    for mod in _ENGINES:
        for argv in ([], ["--query", "bike"], ["--query", "bike", "--location", "nyc", "--url", _BIKE_URL],
                     ["--query", "bike", "--location", "nyc", "--min-price", "50", "--max-price", "10"],
                     ["--query", "bike", "--location", "new york"], ["--url", "https://www.facebook.com/marketplace/nyc/"],
                     ["--query", "bike", "--location", "nyc", "--out", "/nonexistent_dir_8812/o.json"]):
            with tempfile.TemporaryDirectory() as td:
                out = str(Path(td) / "out.json")
                args = mod.build_arg_parser().parse_args(["--out", out, *argv])  # a later --out in argv wins
                code = asyncio_run_maybe(mod, args)
                assert code == output_writer.EXIT_BAD_USAGE, (mod.__name__, argv, code)
                assert not Path(out).exists()
        for argv in (["--sort", "cheapest"], ["--days-since-listed", "2"]):
            try:
                mod.build_arg_parser().parse_args(["--query", "x", "--location", "nyc", *argv])
                raise AssertionError(f"{mod.__name__}: accepted {argv}")
            except SystemExit:
                pass


@check("each engine: a malformed --proxy is EXIT_BAD_USAGE, not a crash, and writes nothing")
def _():
    _IMPORT_ERROR_ATTR = {
        "playwright_scraper": "_PLAYWRIGHT_IMPORT_ERROR",
        "selenium_scraper": "_SELENIUM_IMPORT_ERROR",
        "puppeteer_scraper": "_PYPPETEER_IMPORT_ERROR",
    }
    for mod in _ENGINES:
        if getattr(mod, _IMPORT_ERROR_ATTR[mod.__name__], None) is not None:
            continue
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            args = mod.build_arg_parser().parse_args(["--url", _BIKE_URL, "--proxy", "not a proxy!!", "--out", out])
            assert asyncio_run_maybe(mod, args) == output_writer.EXIT_BAD_USAGE, mod.__name__
            assert not Path(out).exists()


@check("selenium_scraper refuses a credentialed --cdp-endpoint with EXIT_BAD_USAGE")
def _():
    args = selenium_scraper.build_arg_parser().parse_args(["--url", _BIKE_URL, "--cdp-endpoint", "ws://user:pass@cb.2captcha.com:9222"])
    assert selenium_scraper.run(args) == output_writer.EXIT_BAD_USAGE


@check("each engine rejects --max-results 0 at the argparse level")
def _():
    for mod in _ENGINES:
        try:
            mod.build_arg_parser().parse_args(["--url", _BIKE_URL, "--max-results", "0"])
            raise AssertionError(f"{mod.__name__}: expected argparse to reject --max-results 0")
        except SystemExit:
            pass


# --------------------------------------------------------------------------- #
# diff_runs / scraper_api_client — sanity only (family-shared, no site knowledge)
# --------------------------------------------------------------------------- #
@check("diff_runs reports added/removed/changed between two real finish_run() outputs")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("a", title="A v1"), _mk_product("b", title="B")],
            out_path=old_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("a", title="A v1"), _mk_product("c", title="C")],
            out_path=new_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        result = diff_runs.diff(old_out, new_out)
        assert result["added"] == ["c"]
        assert result["removed"] == [] and result["left_selection"] == ["b"], "a Page is removed only when its address is gone"


@check("diff_runs refuses to compare a non-'complete' run")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("a")], out_path=old_out, fmt="json", engine="test", url="u",
            pages_requested=2, pages_completed=1, failed_pages=[2],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("a")], out_path=new_out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        try:
            diff_runs.diff(old_out, new_out)
            raise AssertionError("expected a refusal — old run is 'partial', not 'complete'")
        except SystemExit:
            pass


@check("scraper_api_client.TwoCaptchaClient._require_key rejects a missing/empty key")
def _():
    client = scraper_api_client.TwoCaptchaClient("")
    try:
        client._require_key()
        raise AssertionError("expected TwoCaptchaAuthError")
    except scraper_api_client.TwoCaptchaAuthError:
        pass


@check("scraper_api_client honors --captcha-api override, not the module-level API_BASE")
def _():
    client = scraper_api_client.TwoCaptchaClient("fakekey", api_base="https://mock.example.test")
    assert client.api_base == "https://mock.example.test"
    assert client.api_base != scraper_api_client.API_BASE


def _diff_run(td, name, rows, url, **kw):
    out = str(Path(td) / name)
    kw.setdefault("allow_empty", False)
    output_writer.finish_run(products=rows, out_path=out, fmt="json", engine="t", url=url, pages_requested=1,
                             pages_completed=1, failed_pages=None, blocked=False, remote_api_error=False,
                             started_at=0.0, **kw)
    return out


@check("diff_runs refuses different selections, never calls a currency switch a price change (even at the same number), reads a capped top-N's missing SKU as left_selection, and rejects a sidecar that does not describe its file (audit 2026-09-30)")
def _():
    dress, jeans = "https://us.shein.com/pdsearch/dress/", "https://us.shein.com/pdsearch/jeans/"
    with tempfile.TemporaryDirectory() as td:
        a = _diff_run(td, "a.json", [_mk_product("s1", 9.93, currency="USD")], dress)
        b = _diff_run(td, "b.json", [_mk_product("s1", 19.93, currency="EUR")], jeans)
        try:
            diff_runs.diff(a, b)
            raise AssertionError("different selections must be refused")
        except SystemExit as exc:
            assert "different selections" in str(exc)
        r = diff_runs.diff(a, b, allow_different_scope=True)
        assert not r["changed"] and len(r["currency_changed"]) == 1

        c = _diff_run(td, "c.json", [_mk_product("s1", 10.0, currency="USD")], dress)
        e = _diff_run(td, "e.json", [_mk_product("s1", 10.0, currency="EUR")], dress + "?")
        r = diff_runs.diff(c, e)
        assert r["currency_changed"] and not r["changed"], "same number, other currency must still be reported"

        f = _diff_run(td, "f.json", [_mk_product("s1"), _mk_product("s2")], dress, max_results=2)
        g = _diff_run(td, "g.json", [_mk_product("s1"), _mk_product("s3")], dress, max_results=2)
        r = diff_runs.diff(f, g)
        assert r["capped"] and r["left_selection"] == ["s2"] and r["removed"] == [] and r["added"] == ["s3"]
        h = _diff_run(td, "h.json", [_mk_product("s1"), _mk_product("s2")], dress, max_results=50)
        i = _diff_run(td, "i.json", [_mk_product("s1")], dress, max_results=50)
        r = diff_runs.diff(h, i)
        assert r["removed"] == [] and r["left_selection"] == ["s2"] and not r["capped"], \
            "a missing row whose address the new run did not find gone is left_selection, not removed (Pages)"

        Path(g).write_text("[]", encoding="utf-8")
        try:
            diff_runs.diff(f, g)
            raise AssertionError("a sidecar whose hash does not match must be refused")
        except SystemExit as exc:
            assert "output_sha256" in str(exc)

@check("the credential scanner FINDS a planted key in every shape seen in the family (JSON-quoted, JSON-escaped, 32-hex next to a key word) and ignores placeholders, type hints and Python-name mappings — a scanner that cannot fail is not one (CLAUDE.md §24/§25)")
def _():
    import importlib.util
    scanner = ROOT / ".github" / "ci_checks.py"
    if not (ROOT / ".github").is_dir():
        return
    spec = importlib.util.spec_from_file_location("ci_checks_planted", scanner)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fake32 = "0123456789abcdef" * 2
    for planted in ('"api_key": "a8f3k2m9q7x1z5b4"', '{\\"api_key\\": \\"a8f3k2m9q7x1z5b4\\"}',
                    "TWOCAPTCHA_KEY=" + fake32, '"clientKey":"' + fake32 + '"'):
        assert mod.scan_text("planted.txt", planted), f"scanner missed a planted credential: {planted!r}"
    for harmless in ("api_key: Optional[str] = None", "TWOCAPTCHA_KEY=your-key-here", '"TWOCAPTCHA_KEY": "twocaptcha_key",'):
        assert not mod.scan_text("ok.txt", harmless), f"false positive: {harmless!r}"

@check("no workflow imports a local module inline — tests.yml calls ci_checks.py instead (CLAUDE.md §26: an inline heredoc import is red only on the first push)")
def _():
    import re as _re
    if not (ROOT / ".github").is_dir():
        return  # the Docker image ships no .github/ (CLAUDE.md §22)
    local = {p.stem for p in ROOT.glob("*.py")}
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text(encoding="utf-8")
        for m in _re.finditer(r"^\s*(?:from\s+([A-Za-z_]\w*)\s+import|import\s+([A-Za-z_]\w*))", text, _re.M):
            name = m.group(1) or m.group(2)
            assert name not in local, f"{wf.name}: imports local module {name!r} inline"
    assert "ci_checks.py --sample-check" in (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

@check(".gitignore covers every artefact a run writes (CLAUDE.md §22/§26): .env copies, --dump-html challenge screenshots, *.pageN dumps, live/ — while sample outputs, fixtures and .env.example stay tracked")
def _():
    import subprocess as _sp
    if not (ROOT / ".git").exists():
        return
    must_ignore = [".env", ".env.bak", ".env.local", "facebook_marketplace_debug_1.html",
                   "out.json.page3", "live/x.html", "facebook_marketplace.json", "run.json"]
    must_keep = [".env.example", "sample_output.json", "sample_output.csv", "tests/fixtures/fb_mp_search_bike_nyc_20261006.html",
                 "tests/fixtures/fb_mp_item_20261005.html"]
    for path in must_ignore:
        assert _sp.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0, f"not ignored: {path}"
    for path in must_keep:
        assert _sp.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode != 0, f"wrongly ignored: {path}"


@check("diff_runs on Marketplace: a price drop is a price change, a listing marked sold is a field change, a listing gone from the 24 is left_selection, one whose own page the new run found gone is removed")
def _():
    with tempfile.TemporaryDirectory() as td:
        def run_(name, rows, **meta):
            out = str(Path(td) / name)
            output_writer.finish_run(products=rows, out_path=out, fmt="json", engine="t", url=_BIKE_URL, pages_requested=1,
                                     pages_completed=1, failed_pages=None, blocked=False, remote_api_error=False,
                                     allow_empty=False, started_at=0.0, max_results=100, extra_meta={"selection": {"x": 1}, **meta})
            return out
        a = run_("a.json", [_mk_product("1", 100.0, currency="USD"), _mk_product("2", 50.0), _mk_product("3"), _mk_product("4")])
        b = run_("b.json", [_mk_product("1", 80.0, currency="USD"), _mk_product("2", 50.0, is_sold=True)],
                 not_found_urls=["https://www.facebook.com/marketplace/item/4/"])
        r = diff_runs.diff(a, b)
        assert r["changed"] == [{"sku": "1", "old_price": 100.0, "new_price": 80.0, "title": "An example listing"}], r["changed"]
        assert r["field_changes"] == [{"sku": "2", "fields": {"is_sold": {"old": False, "new": True}}}], r["field_changes"]
        assert r["removed"] == ["4"] and r["left_selection"] == ["3"], r


# --------------------------------------------------------------------------- #
# page_flow — the ONE fetch loop (CLAUDE.md §26), driven with a fake engine
# --------------------------------------------------------------------------- #
_SESSION = {"playwright_scraper": "_PlaywrightSession", "selenium_scraper": "_SeleniumSession", "puppeteer_scraper": "_PyppeteerSession"}
_ENGINE = {"playwright_scraper": "_PlaywrightEngine", "selenium_scraper": "_SeleniumEngine", "puppeteer_scraper": "_PyppeteerEngine"}


@check("the fetch loop exists ONCE: no engine parses or finishes a run itself, each calls page_flow.run and page_flow.resolve_urls; the CDP connect goes through connect_with_retry; pyppeteer disconnects instead of closing the remote browser; no engine captures network responses it would never read")
def _():
    for mod in _ENGINES:
        src = (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
        for fragment in ("finish_run(", "listing_row(", "item_row(", "listings(", "page_state(", "take_responses"):
            assert fragment not in src, f"{mod.__name__}: {fragment!r} outside page_flow"
        assert "lp.is_pagination_request(" in src and "can_paginate = True" in src, \
            f"{mod.__name__}: network capture is for the page's own pagination requests only, filtered by the shared rule"
        assert src.count("page_flow.run(") == 1 and mod._resolve_urls is page_flow.resolve_urls, mod.__name__
    for mod in (playwright_scraper, puppeteer_scraper):
        assert "scraper_api_client.connect_with_retry(" in (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
    assert "reuse_default and browser.contexts" in (ROOT / "playwright_scraper.py").read_text(encoding="utf-8")
    pup = (ROOT / "puppeteer_scraper.py").read_text(encoding="utf-8")
    assert "_bounded(browser.disconnect()" in pup and "_release(remote_browser, remote=True)" in pup


@check("§26 ops set, derived from page_flow's AST (every session.<op> / engine.<op> the loop uses): each engine's session and engine class provides all of them; the Scraper API engine provides all but the pagination ops, and says it cannot paginate")
def _():
    import ast as _ast
    import scraper_api_engine
    tree = _ast.parse((ROOT / "page_flow.py").read_text(encoding="utf-8"))
    ops = {"session": set(), "engine": set()}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name) and node.value.id in ops:
            ops[node.value.id].add(node.attr)
    assert {"goto", "content", "current_url", "wait", "close"} <= ops["session"], ops
    assert {"open", "sleep", "solve_captcha", "readiness_s", "name"} <= ops["engine"], ops
    pairs = [(getattr(m, _SESSION[m.__name__]), getattr(m, _ENGINE[m.__name__]), m.__name__) for m in _ENGINES]
    pairs.append((scraper_api_engine._ScraperApiSession, scraper_api_engine.ScraperApiEngine, "scraper_api_engine"))
    paging = {"dismiss_dialog", "wheel", "take_requests", "post_form"}
    assert paging <= ops["session"], ops
    for sc, ec, name in pairs:
        need = ops["session"] if getattr(ec, "can_paginate", False) else ops["session"] - paging
        assert not [o for o in need if not hasattr(sc, o)], (name, [o for o in need if not hasattr(sc, o)])
        assert not [o for o in ops["engine"] if not hasattr(ec, o)], name
    assert scraper_api_engine.ScraperApiEngine.can_paginate is False
    assert all(getattr(getattr(m, _ENGINE[m.__name__]), "can_paginate") is True for m in _ENGINES)


def _canonical(url):
    """The address page_flow opened, minus the locale it adds (lp.fetch_url)."""
    assert url.endswith("locale=en_US"), f"opened without locale=en_US: {url}"
    return url[: -len("locale=en_US") - 1]


class _FakeSession:
    """Answers one address the way the site does: `pages` is what content()
    returns over time (each wait() moves one step on, so a shell can paint)."""

    def __init__(self, engine, script):
        self.engine, self.script, self.closed = engine, dict(script), False
        self.pages = list(script.get("pages") or [script.get("html", "")])
        self.step = 0
        self.dismissed, self.wheels, self.requested, self.posted, self.waited = 0, 0, False, [], 0.0

    async def goto(self, url):
        self.engine.asked.append(url)
        step = self.script.get("goto")
        if isinstance(step, Exception):
            raise step
        return self.script.get("status", 200)

    async def content(self):
        return self.pages[min(self.step, len(self.pages) - 1)]

    async def current_url(self):
        return self.script.get("final_url", self.engine.asked[-1])

    async def wait(self, seconds):
        self.step += 1
        self.waited += seconds

    async def dismiss_dialog(self):
        self.dismissed += 1
        return True

    async def wheel(self, pixels):
        self.wheels += 1

    async def take_requests(self):
        first = self.script.get("first_request")
        if first and self.wheels >= self.script.get("request_after", 1) and not self.requested:
            self.requested = True
            return [first]
        return []

    async def post_form(self, data):
        self.posted.append(data)
        answers = self.script.get("answers") or []
        answer = answers[min(len(self.posted) - 1, len(answers) - 1)] if answers else (200, "")
        return answer(len(self.posted)) if callable(answer) else answer

    async def close(self):
        self.closed = True


class _FakeEngine:
    name = "fake"
    readiness_s = 0

    def __init__(self, by_url, solve=None):
        self.by_url, self.sessions, self.slept, self.asked, self._solve = by_url, [], [], [], solve
        self.fetched = []

    async def open(self, proxy):
        engine = self

        class _S(_FakeSession):
            async def goto(self, url):
                engine.fetched.append(url)
                url = _canonical(url)
                script = engine.by_url.get(url)
                if script is None and "/marketplace/item/" in url:
                    script = engine.by_url.get("item*")
                if script is None:
                    script = engine.by_url.get("*", {})
                _FakeSession.__init__(self, engine, script)
                return await _FakeSession.goto(self, url)
        sess = _S(self, {})
        self.sessions.append(sess)
        return sess

    async def sleep(self, seconds):
        self.slept.append(seconds)

    async def solve_captcha(self, session, *, html, url):
        return self._solve() if self._solve else None


def _pflow(by_url, argv, *, solve=None, proxy_pool=None, paginate=False):
    args = playwright_scraper.build_arg_parser().parse_args([*argv, "--delay-between-pages", "0"])
    args._solve_budget = page_flow.SolveBudget(args.max_solves)
    urls, _skipped = page_flow.resolve_urls(args)
    engine = _FakeEngine(by_url, solve)
    engine.can_paginate = paginate
    with tempfile.TemporaryDirectory() as td:
        args.out = str(Path(td) / "o.json")
        rc = asyncio.run(page_flow.run(engine, args, urls=urls, proxy_pool=proxy_pool, client=None, started_at=0.0))
        meta_p = Path(args.out + ".meta.json")
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else None
        rows = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else None
    assert all(s.closed for s in engine.sessions), "every opened session must be closed"
    return rc, meta, rows, engine


def _item_page(listing_id):
    """The REAL listing page, re-keyed to another listing id."""
    return _fx("item").replace(_ITEM_ID, listing_id)


def _answer(n, *, has_next=True):
    """The REAL pagination answer, re-keyed so answer n carries new ids."""
    text = (_FIX / "fb_mp_pagination_answer_20261006.txt").read_text(encoding="utf-8")
    text = re.sub(r'"id":"(\d+)","marketplace_listing_title"', lambda m: f'"id":"7{n:03d}{m.group(1)[-9:]}","marketplace_listing_title"', text)
    return text.replace('"has_next_page":true', '"has_next_page":%s' % ("true" if has_next else "false"))


_RATE_LIMITED = '{"errors":[{"message":"Rate limit exceeded","severity":"CRITICAL","code":1675004}]}'


def _paged(**kw):
    form = (_FIX / "fb_mp_pagination_form_20261006.txt").read_text(encoding="utf-8").splitlines()[-1]
    return {"html": _fx("search_bike_nyc"), "first_request": (form, _answer(0)), **kw}


@check("page_flow PAGINATION past the embedded 24 (measured 2026-10-06/07: after closing the login dialog a wheel scroll makes the page send its own next-page request, which then answers with each next cursor): rows in order, positions continuous, --max-results caps and marks the run capped, every request carries the previous answer's cursor and is spaced PAGINATION_DELAY_S apart")
def _():
    answers = [(200, _answer(n)) for n in range(1, 10)]
    rc, meta, rows, eng = _pflow({_BIKE_URL: _paged(answers=answers, request_after=2)}, ["--url", _BIKE_URL, "--max-results", "20"], paginate=True)
    s = eng.sessions[0]
    assert rc == output_writer.EXIT_OK and len(rows) == 20 and len({r["sku"] for r in rows}) == 20, (rc, rows and len(rows))
    assert [r["position"] for r in rows] == list(range(1, 21)) and rows[6]["listing_id"].startswith("7000")
    assert meta["capped"] is True and meta["pages"][0]["pagination"] == "limit" and meta["status"] == "complete"
    assert s.dismissed >= s.wheels >= 2, "the dialog is closed before every wheel round"
    from urllib.parse import parse_qsl
    cursors = [json.loads(dict(parse_qsl(d))["variables"])["cursor"] for d in s.posted]
    first_cursor = lp.page_info(lp.graphql_payloads(_answer(0)))[1]
    assert cursors[0] == first_cursor, "the first replay continues from the page's own answer"
    assert s.waited >= len(s.posted) * page_flow.PAGINATION_DELAY_S, "spaced, not hammered"


@check("page_flow pagination endings: has_next false = complete; 'Rate limit exceeded' = partial (stop_reason rate_limited) with every row read kept; a page that never sends the request = partial (stalled); three answers with nothing new = partial (stalled); an HTTP error = partial; an engine that cannot paginate (Scraper API) = the 24, capped")
def _():
    rc, meta, rows, eng = _pflow({_BIKE_URL: _paged(answers=[(200, _answer(1)), (200, _answer(2, has_next=False))])},
                                 ["--url", _BIKE_URL, "--max-results", "500"], paginate=True)
    assert rc == output_writer.EXIT_OK and len(rows) == 6 + 6 + 6 + 6 and meta["pages"][0]["pagination"] == "end" and meta["capped"] is False, (rc, len(rows or []), meta and meta["pages"])
    rc, meta, rows, eng = _pflow({_BIKE_URL: _paged(answers=[(200, _answer(1)), (200, _RATE_LIMITED)])},
                                 ["--url", _BIKE_URL, "--max-results", "500"], paginate=True)
    assert rc == output_writer.EXIT_PARTIAL and meta["stop_reason"] == "rate_limited" and len(rows) == 18, (rc, meta and meta["stop_reason"], len(rows or []))
    assert len(eng.sessions[0].posted) == 2, "a rate limit ends the search's pagination, it is not hammered"
    rc, meta, rows, eng = _pflow({_BIKE_URL: {"html": _fx("search_bike_nyc")}}, ["--url", _BIKE_URL, "--max-results", "500"], paginate=True)
    assert rc == output_writer.EXIT_PARTIAL and meta["stop_reason"] == "stalled" and len(rows) == 6
    assert eng.sessions[0].wheels == page_flow.WHEEL_ROUNDS and meta["pages"][0]["pagination"] == "no_request"
    rc, meta, rows, eng = _pflow({_BIKE_URL: _paged(answers=[(200, _answer(0))])}, ["--url", _BIKE_URL, "--max-results", "500"], paginate=True)
    assert rc == output_writer.EXIT_PARTIAL and meta["stop_reason"] == "stalled" and len(eng.sessions[0].posted) == page_flow.PAGINATION_STALL_ROUNDS
    rc, meta, rows, eng = _pflow({_BIKE_URL: _paged(answers=[(500, "")])}, ["--url", _BIKE_URL, "--max-results", "500"], paginate=True)
    assert rc == output_writer.EXIT_PARTIAL and len(rows) == 12 and meta["pages"][0]["pagination"] == "error"
    assert len(eng.sessions[0].posted) == page_flow.PAGINATION_RETRIES + 1, "a failed request is retried before giving up"
    rc, meta, rows, eng = _pflow({_BIKE_URL: _paged(answers=[(500, ""), (200, _answer(1, has_next=False))])},
                                 ["--url", _BIKE_URL, "--max-results", "500"], paginate=True)
    assert rc == output_writer.EXIT_OK and len(rows) == 18 and meta["pages"][0]["pagination"] == "end", "one failure, then an answer: read on"


@check("page_flow END TO END on the REAL captures: a search's listings in the site's order, every address opened with locale=en_US; --max-results caps the rows and marks the run capped; two searches finding one listing write it once, the first search's")
def _():
    rc, meta, rows, eng = _pflow({_BIKE_URL: {"html": _fx("search_bike_nyc")}}, ["--query", "bike", "--location", "nyc"])
    assert rc == output_writer.EXIT_OK and [r["listing_id"] for r in rows] == _BIKE_IDS, (rc, rows)
    assert eng.fetched == [_BIKE_URL + "&locale=en_US"] and rows[0]["search_url"] == _BIKE_URL
    assert meta["selection"] == {"mode": "marketplace", "urls": [_BIKE_URL], "details": False, "max_results": 100}
    assert meta["capped"] is True and meta["pages"] == [{"url": _BIKE_URL, "kind": "search", "state": "content", "rows": 6,
                                                         "pagination": "unsupported", "pagination_answers": 0, "result": "ok"}]
    rc, meta, rows, eng = _pflow({_BIKE_URL: {"html": _fx("search_bike_nyc")}}, ["--url", _BIKE_URL, "--max-results", "4"])
    assert len(rows) == 4 and meta["capped"] is True and meta["status"] == "complete"
    other = "https://www.facebook.com/marketplace/nyc/search/?query=bicycle"
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("search_bike_nyc")}}, ["--urls-file", str(_urls_file([_BIKE_URL, other]))])
    assert len(rows) == 6 and {r["search_url"] for r in rows} == {_BIKE_URL} and [p["rows"] for p in meta["pages"]] == [6, 0]


@check("page_flow --details: each listing's own page is opened after the search, in order, and fills description, condition, category, photos and the currency code (price_source item_page); a listing gone by then is status REMOVED, kept from the search; a listing URL as input is read from its own page")
def _():
    by = {_BIKE_URL: {"html": _fx("search_bike_nyc")}, "item*": {"html": ""}}
    for lid in _BIKE_IDS[:3]:
        by[lp.item_url(lid)] = {"html": _item_page(lid)}
    by[lp.item_url(_BIKE_IDS[3])] = {"html": _fx("not_found")}
    rc, meta, rows, eng = _pflow(by, ["--query", "bike", "--location", "nyc", "--details", "--max-results", "4"])
    assert [_canonical(u) for u in eng.fetched] == [_BIKE_URL] + [lp.item_url(i) for i in _BIKE_IDS[:4]], eng.fetched
    assert [r["currency"] for r in rows] == ["USD", "USD", "USD", None] and rows[0]["price_source"] == "item_page"
    assert rows[0]["condition"] == "Used - like new" and rows[0]["description"].startswith("Great condition")
    assert rows[0]["price"] == 50.0, "the search's price is kept; the detail page adds, it does not overwrite with another listing's"
    assert rows[3]["status"] == "REMOVED" and rows[3]["price_source"] == "search" and meta["details_read"] == 3
    assert meta["not_found_urls"] == [lp.item_url(_BIKE_IDS[3])] and rc == output_writer.EXIT_OK
    rc, meta, rows, eng = _pflow({_ITEM_URL: {"html": _fx("item")}}, ["--url", _ITEM_URL])
    assert rc == output_writer.EXIT_OK and len(rows) == 1 and rows[0]["currency"] == "USD" and rows[0]["search_url"] is None


@check("page_flow on the REAL no-results and gone pages: no results is empty (exit 4, nothing written, never retried); a listing URL that is gone is not_found (exit 4); a search shell that paints late is read; one that never paints is not_painted")
def _():
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("search_empty")}}, ["--query", "zqxwvjkpqzzz", "--location", "nyc", "--retries", "3"])
    assert rc == output_writer.EXIT_ZERO_PRODUCTS and meta is None and len(eng.asked) == 1
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("not_found")}}, ["--url", _ITEM_URL])
    assert rc == output_writer.EXIT_ZERO_PRODUCTS and meta is None
    shell = '<script type="application/json">{"canonicalRouteName":"comet.fbweb.CometMarketplaceSearchRoute"}</script>'
    rc, meta, rows, eng = _pflow({"*": {"pages": [shell] * 4 + [_fx("search_bike_nyc")]}}, ["--url", _BIKE_URL])
    assert rc == output_writer.EXIT_OK and len(rows) == 6
    rc, meta, rows, eng = _pflow({"*": {"html": shell}}, ["--url", _BIKE_URL, "--allow-empty"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta["stop_reason"] == "not_painted", meta


@check("page_flow on blocks and failures: a login redirect and a non-facebook page under 403 are blocked (exit 3); without a pool the run STOPS after 3 blocks in a row, the rest not_attempted, rows already read kept as partial (6) — also when the block comes during --details; a proxy's 407 is fetch_error; a dead proxy is bounded")
def _():
    login = {"html": "<html><title>Log in to Facebook</title></html>", "final_url": "https://www.facebook.com/login/"}
    rc, meta, rows, eng = _pflow({"*": login}, ["--url", _BIKE_URL])
    assert rc == output_writer.EXIT_BLOCKED and meta is None
    many = [f"https://www.facebook.com/marketplace/nyc/search/?query=q{i}" for i in range(6)]
    rc, meta, rows, eng = _pflow({"*": {"html": _CHROMIUM_ERROR, "status": 403}}, ["--urls-file", str(_urls_file(many))])
    assert rc == output_writer.EXIT_BLOCKED and len(eng.asked) == page_flow.STOP_AFTER_CONSECUTIVE_BLOCKS
    rc, meta, rows, eng = _pflow({_BIKE_URL: {"html": _fx("search_bike_nyc")}, "*": {"html": _CHROMIUM_ERROR, "status": 429}},
                                 ["--urls-file", str(_urls_file([_BIKE_URL] + many))])
    reasons = [f["reason"] for f in meta["failed_urls"]]
    assert rc == output_writer.EXIT_PARTIAL and len(rows) == 6 and meta["stop_reason"] == "blocked"
    assert reasons.count("not_attempted") == 6 - page_flow.STOP_AFTER_CONSECUTIVE_BLOCKS and reasons.count("blocked") == 3
    rc, meta, rows, eng = _pflow({_BIKE_URL: {"html": _fx("search_bike_nyc")}, "item*": {"html": _CHROMIUM_ERROR, "status": 429}},
                                 ["--url", _BIKE_URL, "--details"])
    assert rc == output_writer.EXIT_PARTIAL and len(rows) == 6 and len(eng.asked) == 1 + page_flow.STOP_AFTER_CONSECUTIVE_BLOCKS, len(eng.asked)
    assert all(r["description"] is None for r in rows)
    rc, meta, rows, eng = _pflow({"*": {"html": "<html><body></body></html>", "status": 407}}, ["--url", _BIKE_URL, "--allow-empty"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta["failed_urls"][0]["reason"] == "fetch_error"
    rc, meta, rows, eng = _pflow({"*": {"goto": RuntimeError("net::ERR_PROXY_CONNECTION_FAILED")}}, ["--url", _BIKE_URL, "--retries", "1"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta is None and len(eng.asked) == 2


@check("page_flow and the paid-solve budget: a page carrying a generic challenge marker (not a facebook.com route) gets at most --max-solves solves across the WHOLE run")
def _():
    challenge = '<html><div class="g-recaptcha" data-sitekey="x"></div></html>'
    solves = []
    many = [f"https://www.facebook.com/marketplace/nyc/search/?query=q{i}" for i in range(3)]
    rc, meta, rows, eng = _pflow({"*": {"html": challenge, "status": 403}},
                                 ["--urls-file", str(_urls_file(many)), "--max-solves", "1"],
                                 solve=lambda: solves.append(1) or {"action": "warning_solver_error"})
    assert len(solves) == 1 and rc == output_writer.EXIT_BLOCKED, (len(solves), rc)


class _FakeScrapeClient:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def scrape_url(self, url, *, data_format, timeout, cdp_url):
        self.calls.append((_canonical(url), cdp_url))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return scraper_api_client.ScrapeResult(target_status=status, headers={}, body=body)


def _sapi(answers, argv, cdp="ws://u:p@cb.example:9222"):
    import scraper_api_engine
    args = playwright_scraper.build_arg_parser().parse_args([*argv, "--delay-between-pages", "0"])
    args._solve_budget = page_flow.SolveBudget(args.max_solves)
    urls, _skipped = page_flow.resolve_urls(args)
    client = _FakeScrapeClient(answers)
    engine = scraper_api_engine.ScraperApiEngine(client, cdp)
    slept = []

    async def _sleep(seconds):
        if engine.fatal is None:
            slept.append(seconds)
    engine.sleep = _sleep
    with tempfile.TemporaryDirectory() as td:
        args.out = str(Path(td) / "o.json")
        rc = asyncio.run(page_flow.run(engine, args, urls=urls, proxy_pool=None, client=None, started_at=0.0))
        meta_p = Path(args.out + ".meta.json")
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else None
        rows = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else None
    return rc, meta, rows, client, slept


@check("--scraper-api END TO END on the REAL captures (a fake Scraper API client): one call per page, routed through the CDP profile when set; a search's listings, then with --details one call per listing; no results is exit 4; a refused key is exit 5 with no further calls; a transient error is retried")
def _():
    rc, meta, rows, client, _ = _sapi([(200, _fx("search_bike_nyc"))], ["--query", "bike", "--location", "nyc"])
    assert rc == output_writer.EXIT_OK and len(rows) == 6 and meta["engine"] == "scraper_api" and client.calls == [(_BIKE_URL, "ws://u:p@cb.example:9222")]
    rc, meta, rows, client, _ = _sapi([(200, _fx("search_bike_nyc")), (200, _item_page(_BIKE_IDS[0]))],
                                      ["--url", _BIKE_URL, "--details", "--max-results", "1"], cdp=None)
    assert rc == output_writer.EXIT_OK and rows[0]["currency"] == "USD" and [c[0] for c in client.calls] == [_BIKE_URL, lp.item_url(_BIKE_IDS[0])]
    rc, meta, rows, client, _ = _sapi([(200, _fx("search_bike_nyc"))], ["--url", _BIKE_URL, "--max-results", "500"])
    assert rc == output_writer.EXIT_OK and len(rows) == 6 and meta["capped"] is True and meta["pages"][0]["pagination"] == "unsupported", \
        "no live page to paginate: the embedded listings, honestly capped"
    rc, meta, rows, client, _ = _sapi([(200, _fx("search_empty"))], ["--url", _BIKE_URL])
    assert rc == output_writer.EXIT_ZERO_PRODUCTS and len(client.calls) == 1
    refused = scraper_api_client.TwoCaptchaAuthError("Scraper API: invalid/missing TWOCAPTCHA_KEY")
    many = [f"https://www.facebook.com/marketplace/nyc/search/?query=q{i}" for i in range(3)]
    rc, meta, rows, client, slept = _sapi([refused], ["--urls-file", str(_urls_file(many))])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and len(client.calls) == 1 and slept == []
    flaky = scraper_api_client.TwoCaptchaError("Scraper API returned HTTP 502: bad gateway")
    rc, meta, rows, client, _ = _sapi([flaky, (200, _fx("search_sofa_london"))], ["--url", "https://www.facebook.com/marketplace/london/search/?query=sofa"])
    assert rc == output_writer.EXIT_OK and len(client.calls) == 2 and rows[0]["currency"] == "GBP"


@check("--scraper-api usage, all three engines, with NO engine driver needed: no key is exit 2 before any call; Selenium does not refuse a credentialed endpoint in this mode")
def _():
    for mod in _ENGINES:
        base = ["--url", _BIKE_URL, "--scraper-api", "--out", str(Path(tempfile.mkdtemp()) / "o.json")]
        a = mod.build_arg_parser().parse_args(base + ["--cdp-endpoint", "ws://u:p@cb.example:9222"])
        rc = mod.run(a) if mod is selenium_scraper else asyncio.run(mod.run(a))
        assert rc == output_writer.EXIT_BAD_USAGE, (mod.__name__, rc)
    src = (ROOT / "selenium_scraper.py").read_text(encoding="utf-8")
    assert src.index("if args.scraper_api:") < src.index("_cdp_endpoint_has_credentials(args.cdp_endpoint):\n")


@check("pyppeteer answers proxy auth over CDP Fetch, never page.authenticate() — measured 2026-10-04 on a sibling repo: current Chromium has no Network.setRequestInterception, so every proxied URL failed")
def _():
    src = (ROOT / "puppeteer_scraper.py").read_text(encoding="utf-8")
    assert "await page.authenticate(" not in src
    for op in ('"Fetch.enable"', '"Fetch.authRequired"', '"Fetch.continueWithAuth"', '"Fetch.requestPaused"', '"Fetch.continueRequest"'):
        assert op in src, op


@check("fingerprint_client.user_agent_from reads the LIVE response shape (userAgent.userAgent, measured 2026-10-04) and the documented one (userAgent.value); nothing applied when neither is there")
def _():
    import fingerprint_client as fc
    live = {"id": 6472645, "country": "US", "userAgent": {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "fullVersion": "152.0.7977.64", "platform": "Windows", "platformVersion": "10.0.0", "mobile": False}}
    assert fc.user_agent_from(live).startswith("Mozilla/5.0 (Windows NT 10.0"), "the live shape — reading only `value` made --fingerprint a no-op"
    assert fc.user_agent_from({"userAgent": {"value": "UA-doc"}}) == "UA-doc"
    for empty in ({}, {"userAgent": {}}, {"userAgent": {"userAgent": ""}}, None):
        assert fc.user_agent_from(empty) is None, empty
    for path in ("playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py"):
        assert 'log.info("Fingerprint applied: user agent %s", user_agent)' in (ROOT / path).read_text(encoding="utf-8"), path


@check("pyppeteer cleanup is BOUNDED: a page or browser whose close never returns does not hang the run (measured 2026-10-06: 20+ minutes stuck after a proxy dropped the connection); a local Chromium that will not close is killed")
def _():
    class _Never:
        killed = False

        def __init__(self):
            self.process = self

        async def close(self):
            await asyncio.sleep(3600)

        async def disconnect(self):
            await asyncio.sleep(3600)

        def kill(self):
            _Never.killed = True

    old = puppeteer_scraper.CLOSE_TIMEOUT_S
    puppeteer_scraper.CLOSE_TIMEOUT_S = 0.05
    try:
        import time as _time
        t0 = _time.monotonic()
        browser = _Never()
        asyncio.run(puppeteer_scraper._release(browser, remote=False))
        asyncio.run(puppeteer_scraper._release(browser, remote=True))
        session = puppeteer_scraper._PyppeteerSession.__new__(puppeteer_scraper._PyppeteerSession)
        session.page, session.browser, session.owns_browser = _Never(), _Never(), True
        asyncio.run(session.close())
        assert _time.monotonic() - t0 < 2 and _Never.killed
    finally:
        puppeteer_scraper.CLOSE_TIMEOUT_S = old


@check("a login page served under the address asked for (no redirect to go by — measured with the Scraper API's pool, canonical https://fi-fi.facebook.com/login) is a BLOCK (exit 3), never an unknown page; the canonical test fires on none of 66 real captures")
def _():
    login = ('<!DOCTYPE html><html id="facebook" lang="fi"><head><link rel="canonical" href="https://fi-fi.facebook.com/login" />'
             '<title>Facebook</title></head><body><form id="login_form"></form></body></html>')
    assert lp.page_state(login) == "login" and lp.is_login_page(login)
    for name in _PAGES:
        assert not lp.is_login_page(_fx(name)), name
    rc, meta, rows, eng = _pflow({"*": {"html": login, "status": 200}}, ["--url", _BIKE_URL])
    assert rc == output_writer.EXIT_BLOCKED and meta is None, rc


@check("an EMPTY Scraper API answer (no page, no target status — measured 2026-10-06) is the service failing: retried, and when it persists the run ends as remote_api_error (exit 5), never as an unreadable page")
def _():
    rc, meta, rows, client, _ = _sapi([(None, "")], _SAPI_ARGV + ["--retries", "1"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and len(client.calls) == 2, (rc, len(client.calls))


def run() -> int:
    """All @check-decorated functions above already ran at import time
    (that's the point — see the `check()` docstring) and self-registered
    into RESULTS. This just reports them."""
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"smoke_test: {passed}/{len(RESULTS)} checks passed")
    for name, detail in failed:
        print(f"  FAIL: {name}\n        {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    import sys
    sys.exit(run())
