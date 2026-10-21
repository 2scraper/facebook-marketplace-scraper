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
those captures hold, not just that a column is populated.

Run directly: `python3 smoke_test.py`
"""
from __future__ import annotations

import asyncio
import inspect as _inspect
import json
import os
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
# Engine import/CLI hygiene
# --------------------------------------------------------------------------- #
@check("engines import cleanly regardless of installed drivers")
def _():
    for mod in _ENGINES:
        assert hasattr(mod, "build_arg_parser")
        assert hasattr(mod, "run")


@check("each engine imports its driver at MODULE level, guarded by try/except ImportError (an ast walk, not a substring: a driver imported inside a function makes the no-driver skip meaningless)")
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


@check("no module uses a name it never imports, defines or assigns (compileall proves a file PARSES, not that its names RESOLVE)")
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
    # Built from pieces so this file can be scanned too (the
    # check used to exempt its own file, where the phrases sat verbatim).
    anti = "anti" + "detect"
    banned = (
        "cloud" + " browser", anti + " browser", "2scraper " + anti + " browser",
        "gate." + "2prx.com", "--" + anti, anti + "_local_api",
    )
    exempt_names = {"CLAUDE" + ".md"}  # a maintainer's local, untracked notes file
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


@check("no shipped file cites a private maintainer document or its numbered sections — the public cannot open it, so a rule is stated in this repo's own words")
def _():
    # Assembled from pieces so this check does not match its own source.
    tokens = ("CLAUDE" + ".md", "claude_" + "2scraper", "2scraper_" + "repo_rules", "play" + "book", "\u00a7")
    section_ref = re.compile(r"\bSec\.\s?\d")
    exempt_names = {"CLAUDE" + ".md"}  # a maintainer's local, untracked notes file
    venvs = {p.parent for p in ROOT.rglob("pyvenv.cfg")}
    scanned = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.name in exempt_names or path.name.startswith("2scraper"):
            continue
        if path.suffix not in (".py", ".md", ".html", ".toml", ".cfg", ".yml", ".yaml", ".txt", ".example") \
                and path.name not in (".gitignore", ".dockerignore", "Dockerfile"):
            continue
        rel = path.relative_to(ROOT).parts
        if ".git" in rel or "__pycache__" in rel or any(v in path.parents for v in venvs):
            continue
        if any(part.endswith(".egg-info") or part in ("build", "dist") for part in rel):
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in tokens:
            assert token not in text, f"{path.relative_to(ROOT)}: cites a private document ({token!r})"
        assert not section_ref.search(text), f"{path.relative_to(ROOT)}: cites a numbered section of a private document"
    assert scanned >= 10, f"citation guard scanned only {scanned} files"


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


@check("all three engines expose the identical --flag set")
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


@check("every top-level module is in the Dockerfile COPY and pyproject py-modules (a module left out breaks the image on every run)")
def _():
    if not (ROOT / "Dockerfile").exists() and not (ROOT / "pyproject.toml").exists():
        return  # the Docker image's own copy of this suite ships neither
    modules = sorted(pth.stem for pth in ROOT.glob("*.py"))
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    listed = set(re.findall(r'"([a-z_]+)"', pyproject.split("py-modules", 1)[1].split("]", 1)[0]))
    for mod in modules:
        assert f"{mod}.py" in docker, f"{mod}.py missing from the Dockerfile COPY"
        assert mod in listed, f"{mod} missing from pyproject py-modules"
    assert listed <= set(modules), f"pyproject lists modules that do not exist: {sorted(listed - set(modules))}"


@check("BOT_CHALLENGE_MARKERS is empty, and the generic markers match NONE of the real captures (count a marker on a good page first)")
def _():
    assert lp.BOT_CHALLENGE_MARKERS == (), "no captcha vendor appeared on any Marketplace capture"
    for name in _PAGES:
        html = _fx(name)
        assert not captcha_solver.detect_from_html(html, lp.BOT_CHALLENGE_MARKERS), f"{name}: a generic marker fires on a real Marketplace page"
        assert not page_flow.is_challenge(html), name


# --------------------------------------------------------------------------- #
# output_writer — exit codes / precedence / dedupe
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


@check("finish_run: rows gathered by a run that did not finish are PARTIAL (6) with the cause in stop_reason — never 5/3 with a file (audit 2026-09-30 got exit 5 AND a written file). Rewrites the old pinned 'exit 5 with products' position deliberately.")
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


@check("listing_row on the REAL New York search: values, not coverage — the site's order, price as a number, the displayed price kept, city/state, listed time in UTC, delivery types; currency left None for a bare '$' (the search does not say which dollar)")
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


@check("fixtures are scrubbed and stay scrubbed: no session token, no seller but the REDACTED placeholder, no link-shim signature — guarded by PATTERN")
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
    dress = "https://www.facebook.com/marketplace/nyc/bicycles/"
    jeans = "https://www.facebook.com/marketplace/nyc/furniture/"
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

@check("the credential scanner FINDS a planted key in every shape seen in the family (JSON-quoted, JSON-escaped, 32-hex next to a key word) and ignores placeholders, type hints and Python-name mappings — a scanner that cannot fail is not one")
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

@check("no workflow imports a local module inline — tests.yml calls ci_checks.py instead (an inline heredoc import is red only on the first push)")
def _():
    import re as _re
    if not (ROOT / ".github").is_dir():
        return  # the Docker image ships no .github/
    local = {p.stem for p in ROOT.glob("*.py")}
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text(encoding="utf-8")
        for m in _re.finditer(r"^\s*(?:from\s+([A-Za-z_]\w*)\s+import|import\s+([A-Za-z_]\w*))", text, _re.M):
            name = m.group(1) or m.group(2)
            assert name not in local, f"{wf.name}: imports local module {name!r} inline"
    assert "ci_checks.py --sample-check" in (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

@check(".gitignore covers every artefact a run writes: .env copies, --dump-html challenge screenshots, *.pageN dumps, live/ — while sample outputs, fixtures and .env.example stay tracked")
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
# page_flow — the ONE fetch loop, driven with a fake engine
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


@check("engine ops set, derived from page_flow's AST (every session.<op> / engine.<op> the loop uses): each engine's session and engine class provides all of them; the Scraper API engine provides all but the pagination ops, and says it cannot paginate")
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


# --------------------------------------------------------------------------- #
# Audit 2026-10-14
# --------------------------------------------------------------------------- #
@check("atomic writer: a file being REPLACED keeps its own mode (0640 stays 0640, 0664 stays 0664); a NEW file gets 0666 minus the umask, under umask 022, 002 and 077 — never a flat 0600/0644")
def _():
    import os as _os
    import stat as _stat
    old = _os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory() as td:
            for umask, expected in ((0o022, 0o644), (0o002, 0o664), (0o077, 0o600)):
                _os.umask(umask)
                target = Path(td) / f"new_{umask:o}.json"
                output_writer.write_json([_mk_product("s1")], str(target))
                got = _stat.S_IMODE(target.stat().st_mode)
                assert got == expected, f"new file under umask {umask:o}: {got:o}, expected {expected:o}"
            for umask in (0o022, 0o002, 0o077):
                _os.umask(umask)
                for existing in (0o640, 0o664):
                    for fmt in ("json", "csv"):
                        target = Path(td) / f"keep_{umask:o}_{existing:o}.{fmt}"
                        target.write_text("old", encoding="utf-8")
                        target.chmod(existing)
                        output_writer.write_output([_mk_product("s1")], str(target), fmt)
                        got = _stat.S_IMODE(target.stat().st_mode)
                        assert got == existing, f"umask {umask:o}: existing {existing:o} became {got:o}"
    finally:
        _os.umask(old)


@check("CSV formula guard: the sidecar records csv_cells_escaped (formula-shaped STRING cells only, never a number), merged UNDER the caller's own extra; JSON keeps the site's text and carries no such count")
def _():
    rows = [_mk_product("s1", -5.0, title='=HYPERLINK("x")', city="+cmd", state="-33.8"),
            _mk_product("s2", 10.0, title="'quoted", city="@home")]
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "o.csv")
        rc = output_writer.finish_run(products=rows, out_path=out, fmt="csv", engine="t", url=_BIKE_URL,
                                      pages_requested=1, pages_completed=1, failed_pages=None, blocked=False,
                                      remote_api_error=False, allow_empty=False, started_at=0.0)
        meta = json.loads(Path(out + ".meta.json").read_text(encoding="utf-8"))
        assert rc == 0 and meta["csv_cells_escaped"] == 4, meta.get("csv_cells_escaped")
        assert meta["csv_text_encoding"] == "apostrophe-v1"
        text = Path(out).read_text(encoding="utf-8")
        assert "'=HYPERLINK" in text and ",-5.0," in text and "''quoted" in text, text
        out2 = str(Path(td) / "c.csv")
        output_writer.finish_run(products=rows, out_path=out2, fmt="csv", engine="t", url=_BIKE_URL,
                                 pages_requested=1, pages_completed=1, failed_pages=None, blocked=False,
                                 remote_api_error=False, allow_empty=False, started_at=0.0,
                                 extra_meta={"csv_cells_escaped": "caller"})
        assert json.loads(Path(out2 + ".meta.json").read_text(encoding="utf-8"))["csv_cells_escaped"] == "caller"
        out3 = str(Path(td) / "j.json")
        output_writer.finish_run(products=rows, out_path=out3, fmt="json", engine="t", url=_BIKE_URL,
                                 pages_requested=1, pages_completed=1, failed_pages=None, blocked=False,
                                 remote_api_error=False, allow_empty=False, started_at=0.0)
        assert "csv_cells_escaped" not in json.loads(Path(out3 + ".meta.json").read_text(encoding="utf-8"))
        assert json.loads(Path(out3).read_text(encoding="utf-8"))[0]["title"] == '=HYPERLINK("x")'


@check("zero rows because the parser refused every record, nothing else wrong: exit 5 with stop_reason parse_error (our failure), not remote_api_error and not 4")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "o.json")
        rc = output_writer.finish_run(products=[], out_path=out, fmt="json", engine="t", url=_BIKE_URL,
                                      pages_requested=1, pages_completed=1, failed_pages=None, blocked=False,
                                      remote_api_error=False, allow_empty=True, started_at=0.0, rejected_rows=3)
        meta = json.loads(Path(out + ".meta.json").read_text(encoding="utf-8"))
        assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta["stop_reason"] == "parse_error", (rc, meta["stop_reason"])
        rc = output_writer.finish_run(products=[], out_path=out, fmt="json", engine="t", url=_BIKE_URL,
                                      pages_requested=1, pages_completed=1, failed_pages=None, blocked=False,
                                      remote_api_error=True, allow_empty=True, started_at=0.0, rejected_rows=3)
        meta = json.loads(Path(out + ".meta.json").read_text(encoding="utf-8"))
        assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta["stop_reason"] == "remote_api_error", meta["stop_reason"]


@check("every engine refuses a negative --retries (a loop that would run zero times and never navigate), --retry-delay, --delay-between-pages, --max-solves and --proxy-block-retries with an argparse error naming the flag; 0 retries stays legal (one attempt)")
def _():
    import contextlib
    import io
    for mod in _ENGINES:
        assert mod.build_arg_parser().parse_args(["--retries", "0"]).retries == 0, mod.__name__
        for flag, value in (("--retries", "-1"), ("--retry-delay", "-1"), ("--delay-between-pages", "-0.5"),
                            ("--max-solves", "-1"), ("--proxy-block-retries", "-1"), ("--max-results", "0")):
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    mod.build_arg_parser().parse_args([flag, value])
                raise AssertionError(f"{mod.__name__}: {flag} {value} was accepted")
            except SystemExit as exc:
                assert exc.code == 2, (mod.__name__, flag, exc.code)
            assert f"error: argument {flag}" in err.getvalue(), (mod.__name__, flag, err.getvalue()[-200:])


@check("FACEBOOK_URL from the environment or .env is only a default: a typed --query/--category builds its own search and a typed --urls-file is read (both with a warning), never refused or overridden; a TYPED --url with --query is still a usage error")
def _():
    import os as _os
    saved = _os.environ.get("FACEBOOK_URL")
    env_url = "https://www.facebook.com/marketplace/la/search/?query=env"
    _os.environ["FACEBOOK_URL"] = env_url
    try:
        for mod in _ENGINES:
            p = mod.build_arg_parser()
            args = env_config.apply_env(p.parse_args(["--query", "bike", "--location", "nyc"]), dotenv_path="/nonexistent/.env")
            assert args.url == env_url and mod._resolve_urls(args) == ([_BIKE_URL], 0), mod.__name__
            f = _urls_file([_ITEM_URL])
            args = env_config.apply_env(p.parse_args(["--urls-file", str(f)]), dotenv_path="/nonexistent/.env")
            assert mod._resolve_urls(args) == ([_ITEM_URL], 0), (mod.__name__, mod._resolve_urls(args))
            args = env_config.apply_env(p.parse_args([]), dotenv_path="/nonexistent/.env")
            assert mod._resolve_urls(args) == ([env_url], 0), mod.__name__
            args = env_config.apply_env(p.parse_args(["--query", "bike", "--location", "nyc", "--url", _BIKE_URL]),
                                        dotenv_path="/nonexistent/.env")
            try:
                mod._resolve_urls(args)
                raise AssertionError(f"{mod.__name__}: a typed --url with --query must be refused")
            except ValueError:
                pass
    finally:
        if saved is None:
            _os.environ.pop("FACEBOOK_URL", None)
        else:
            _os.environ["FACEBOOK_URL"] = saved


@check("diff_runs refuses a JSON run against a CSV run (every number would read as changed) and still diffs two CSV runs of the same rows as unchanged")
def _():
    rows = [_mk_product("s1", 9.5), _mk_product("s2", 0.0)]
    with tempfile.TemporaryDirectory() as td:
        outs = {}
        for name, fmt in (("a.json", "json"), ("a.csv", "csv"), ("b.csv", "csv")):
            out = str(Path(td) / name)
            output_writer.finish_run(products=rows, out_path=out, fmt=fmt, engine="t", url=_BIKE_URL, pages_requested=1,
                                     pages_completed=1, failed_pages=None, blocked=False, remote_api_error=False,
                                     allow_empty=False, started_at=0.0)
            outs[name] = out
        try:
            diff_runs.diff(outs["a.json"], outs["a.csv"])
            raise AssertionError("a JSON run against a CSV run must be refused")
        except SystemExit as exc:
            assert "same --format" in str(exc), exc
        r = diff_runs.diff(outs["a.csv"], outs["b.csv"])
        assert not r["changed"] and not r["added"], r


@check("the credential scan reads an IGNORED .env copy (.env.bak) that git ls-files never lists, leaves .env and .env.example alone, and reports a scan of zero files instead of passing it")
def _():
    import importlib.util
    import shutil
    import subprocess
    if not (ROOT / ".github").is_dir():
        return
    spec = importlib.util.spec_from_file_location("ci_checks_envbak", ROOT / ".github" / "ci_checks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    key = "TWOCAPTCHA_KEY=" + "0123456789abcdef" * 2
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mod.ROOT = root
        assert ("(repository)", 0, "scanned_no_files") in mod.scan(), "an empty scan must not pass"
        (root / ".gitignore").write_text(".env*\n!.env.example\n", encoding="utf-8")
        (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
        (root / ".env").write_text(key + "\n", encoding="utf-8")
        (root / ".env.example").write_text("TWOCAPTCHA_KEY=\n", encoding="utf-8")
        (root / ".env.bak").write_text(key + "\n", encoding="utf-8")
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q", str(root)], check=True)
        found = {path for path, _line, _rule in mod.scan()}
        assert found == {".env.bak"}, found


def _canary_check_snippets():
    """Every `python3 -c "..."` block in the canary workflow that reads canary_exit."""
    import textwrap
    lines = (ROOT / ".github" / "workflows" / "canary.yml").read_text(encoding="utf-8").splitlines()
    snippets, i = [], 0
    while i < len(lines):
        if lines[i].strip() == 'python3 -c "':
            body = []
            i += 1
            while lines[i].strip() != '"':
                body.append(lines[i])
                i += 1
            text = textwrap.dedent("\n".join(body))
            if "canary_exit" in text:
                # bash would expand these inside the double quotes; this check runs the text as-is
                assert not any(ch in text for ch in "$`\\"), "canary snippet uses a bash-special character"
                snippets.append(text)
        i += 1
    return snippets


@check("every way the canary check step fails prints an ::error:: line: a nonzero exit with or without the proxy secret, and a missing or wrong output file (a red job with no annotation hid a proxy failure on 2026-10-20)")
def _():
    import subprocess
    import sys
    if not (ROOT / ".github").is_dir():
        return  # the Docker image ships no workflows; the repository's own CI runs this check
    snippets = _canary_check_snippets()
    assert snippets, "no canary check step found in .github/workflows/canary.yml"
    with tempfile.TemporaryDirectory() as tmp:
        for snippet in snippets:
            for has_proxy in ("true", "false"):
                for code in ("0", "1", "3", "4", "5", "6"):
                    env = dict(os.environ, HAS_PROXY=has_proxy, canary_exit=code,
                               canary_details_exit=code, CANARY_REGION="US")
                    proc = subprocess.run([sys.executable, "-c", snippet], cwd=tmp, env=env,
                                          capture_output=True, text=True, timeout=60)
                    # code 0 in an empty directory: the sidecar is missing, which must fail too
                    assert proc.returncode != 0, (has_proxy, code, "an empty directory passed")
                    assert "::error::" in proc.stdout, (has_proxy, code, proc.stdout[-300:], proc.stderr[-300:])


# --------------------------------------------------------------------------- #
# Proxy traffic savers: the pre-check and media blocking
# --------------------------------------------------------------------------- #
def _status_proxy(status):
    """A local stand-in for a proxy gateway that answers every request,
    CONNECT included, with `status`. Returns (server, hits)."""
    import http.server
    import socketserver
    import threading
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self):
            hits.append((self.command, self.path, self.headers.get("Proxy-Authorization") is not None))
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Proxy-Authenticate", 'Basic realm="stub"')
            self.end_headers()
        do_CONNECT = do_GET = do_HEAD = _answer

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hits


class _NoBrowser:
    """Stands in for every engine's browser entry point: the pre-check must
    stop the run before any of them is reached."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        raise AssertionError("a browser was started")

    def __getattr__(self, name):  # selenium's `webdriver.Chrome`
        return self


class _SeleniumOptions:
    """selenium's Options, for a venv without selenium."""

    def __init__(self):
        self.debugger_address = None

    def add_argument(self, *a):
        pass

    def set_capability(self, *a):
        pass

    def add_experimental_option(self, *a):
        pass


def _engine_with_no_browser(mod):
    import contextlib
    from unittest import mock
    fake = _NoBrowser()
    attr = {"playwright_scraper": "async_playwright", "selenium_scraper": "webdriver",
            "puppeteer_scraper": "pyppeteer_launch"}[mod.__name__]
    patches = contextlib.ExitStack()
    patches.enter_context(mock.patch.object(mod, attr, fake))
    if mod is selenium_scraper:
        patches.enter_context(mock.patch.object(mod, "Options", _SeleniumOptions))
        patches.enter_context(mock.patch.object(mod, "Service", None))
    return fake, patches


def _run_through_stub_proxy(mod, status, argv=()):
    import contextlib
    import io
    server, hits = _status_proxy(status)
    port = server.server_address[1]
    fake, patch = _engine_with_no_browser(mod)
    err = io.StringIO()
    try:
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "run.json")
            Path(out).write_text("[]")  # a previous good run's data stays as it was
            args = mod.build_arg_parser().parse_args(
                ["--query", "bike", "--location", "nyc", "--out", out,
                 "--proxy", f"http://user:S3cretPw@127.0.0.1:{port}", *argv])
            with patch, contextlib.redirect_stderr(err):
                code = asyncio_run_maybe(mod, args)
            attempt_p = Path(out + output_writer.ATTEMPT_META_SUFFIX)
            attempt = json.loads(attempt_p.read_text()) if attempt_p.exists() else {}
            files = sorted(p.name for p in Path(td).iterdir())
            data = Path(out).read_text()
    finally:
        server.shutdown()
        server.server_close()
    return code, fake.calls, hits, err.getvalue(), attempt, files, data


@check("proxy pre-check: a gateway 407 or 402 ends EVERY browser engine at exit 5 before a browser starts — after ONE CONNECT for facebook.com/robots.txt; the message names FACEBOOK_PROXY and never the password; <out>.latest_attempt.meta.json says failed/proxy_auth_failed; the data is untouched and no <out>.meta.json is written")
def _():
    for mod in _ENGINES:
        for status, words in ((407, "refused the credential"), (402, "balance exhausted")):
            code, started, hits, text, attempt, files, data = _run_through_stub_proxy(mod, status)
            where = (mod.__name__, status)
            assert code == output_writer.EXIT_REMOTE_API_ERROR, (where, code, text[-300:])
            assert started == 0, (where, "browser started")
            assert [h[:2] for h in hits] == [("CONNECT", "www.facebook.com:443")], (where, hits)
            assert words in text and "FACEBOOK_PROXY" in text and "No browser was started" in text, (where, text)
            assert "S3cretPw" not in text and "S3cretPw" not in json.dumps(attempt), where
            assert (attempt.get("status"), attempt.get("stop_reason"), attempt.get("data_updated")) == \
                ("failed", "proxy_auth_failed", False), (where, attempt)
            assert files == ["run.json", "run.json" + output_writer.ATTEMPT_META_SUFFIX] and data == "[]", (where, files)


@check("proxy pre-check: Selenium checks the proxy the way Chrome will use it there — WITHOUT the password it cannot send — and a 407 then says to whitelist the IP; Playwright and pyppeteer send the credential")
def _():
    sent = {}
    for mod in _ENGINES:
        code, _started, hits, text, _a, _f, _d = _run_through_stub_proxy(mod, 407)
        sent[mod.__name__] = hits[0][2]
        if mod is selenium_scraper:
            assert "IP whitelist" in text, text
    assert sent == {"playwright_scraper": True, "selenium_scraper": False, "puppeteer_scraper": True}, sent


@check("proxy pre-check: a 403 through the tunnel, an unreachable proxy or no proxy at all is not a verdict on the proxy — the run goes on to the browser exactly as before")
def _():
    for mod in _ENGINES:
        code, started, hits, _t, attempt, _f, _d = _run_through_stub_proxy(mod, 403)
        # No pre-check verdict. Since --proxy-budget-mb, a proxied run that wrote no data records its own
        # outcome and traffic in the attempt sidecar — never as the pre-check's proxy_auth_failed.
        assert started == 1 and len(hits) == 1, (mod.__name__, started, hits, attempt)
        assert attempt == {} or (attempt.get("stop_reason") != "proxy_auth_failed" and "proxy_bytes" in attempt), \
            (mod.__name__, attempt)
    assert proxy_pool.precheck_proxy("http://user:pass@127.0.0.1:1", "https://x.invalid/robots.txt", 3) is None
    assert proxy_pool.precheck_proxy(None, "https://www.facebook.com/robots.txt") is None
    assert proxy_pool.robots_url(_BIKE_URL) == "https://www.facebook.com/robots.txt"
    pool = proxy_pool.ProxyPool([proxy_pool.parse_proxy_line("h:1:u:p"), proxy_pool.parse_proxy_line("g:2")])
    assert pool.peek() is pool.next() and pool.peek().host == "g", "peek is the proxy the first search will use"
    assert proxy_pool.parse_proxy_line("h:1:user:pass").url() == "http://user:pass@h:1"
    assert proxy_pool.parse_proxy_line("h:1:user:pass").url(with_auth=False) == "http://h:1"


def scraper_api_engine_mod():
    import scraper_api_engine
    return scraper_api_engine


async def _zero():
    return 0


@check("proxy pre-check: never sent over --cdp-endpoint or --scraper-api (no local proxy there), with a --proxy given too")
def _():
    from unittest import mock
    seen = []
    with mock.patch.object(proxy_pool, "precheck_proxy", lambda *a, **k: seen.append(a)), \
            mock.patch.object(page_flow, "run", side_effect=AssertionError("stop here")), \
            mock.patch.object(scraper_api_engine_mod(), "run", lambda *a, **k: _zero()):
        for mod in (playwright_scraper, puppeteer_scraper):
            fake, patch = _engine_with_no_browser(mod)
            with tempfile.TemporaryDirectory() as td, patch:
                for extra in (["--cdp-endpoint", "ws://127.0.0.1:1"], ["--scraper-api"]):
                    args = mod.build_arg_parser().parse_args(["--query", "bike", "--location", "nyc", "--out", str(Path(td) / "o.json"),
                                                              "--proxy", "http://user:pass@127.0.0.1:9", *extra])
                    asyncio_run_maybe(mod, args)
        with tempfile.TemporaryDirectory() as td:
            args = selenium_scraper.build_arg_parser().parse_args(["--query", "bike", "--location", "nyc", "--out", str(Path(td) / "o.json"),
                                                                   "--proxy", "http://user:pass@127.0.0.1:9", "--scraper-api"])
            selenium_scraper.run(args)
    assert seen == [], seen


@check("media: blocked only for a LOCAL browser behind a proxy; --load-media keeps it; never over CDP or with no proxy; the hidden --force-block-media turns it on for a free measurement")
def _():
    import argparse as _argparse
    ns = _argparse.Namespace
    sbm = proxy_pool.should_block_media
    assert sbm(ns(load_media=False), proxy_in_use=True) is True
    assert sbm(ns(load_media=True), proxy_in_use=True) is False
    assert sbm(ns(load_media=False), proxy_in_use=False) is False
    assert sbm(ns(load_media=False), proxy_in_use=True, over_cdp=True) is False
    assert sbm(ns(load_media=False, force_block_media=True), proxy_in_use=False) is True
    for mod in _ENGINES:
        parser = mod.build_arg_parser()
        assert parser.parse_args(["--query", "x"]).load_media is False
        assert "--load-media" in parser.format_help() and "--force-block-media" not in parser.format_help(), mod.__name__


class _Route:
    def __init__(self, kind):
        import argparse as _argparse
        self.request = _argparse.Namespace(resource_type=kind)
        self.done = None

    async def abort(self, reason=None):
        self.done = ("abort", reason)

    async def continue_(self):
        self.done = ("continue",)


@check("media (Playwright): images, media and fonts are aborted, documents, scripts, XHR/fetch and stylesheets go on; the route is installed on a proxied local context only, and the run's sidecar records media_blocked")
def _():
    import argparse as _argparse
    for kind, want in (("image", "abort"), ("media", "abort"), ("font", "abort"), ("document", "continue"),
                       ("script", "continue"), ("xhr", "continue"), ("fetch", "continue"), ("stylesheet", "continue")):
        route = _Route(kind)
        asyncio.run(playwright_scraper._media_route(route))
        assert route.done[0] == want, (kind, route.done)

    class Page:
        def on(self, *a):
            pass

    class Context:
        def __init__(self):
            self.routes = []

        async def route(self, pattern, handler):
            self.routes.append((pattern, handler))

        async def new_page(self):
            return Page()

    class Browser:
        def __init__(self):
            self.contexts = [Context()]
            self.made = []

        async def new_context(self, **kw):
            self.made.append(Context())
            return self.made[-1]

    proxy = proxy_pool.parse_proxy_line("h:1:u:p")
    for argv, use_proxy, cdp, want in (([], True, False, True), (["--load-media"], True, False, False),
                                       ([], False, False, False), ([], True, True, False),
                                       (["--force-block-media"], False, False, True)):
        args = playwright_scraper.build_arg_parser().parse_args(["--query", "x", *argv])
        args.cdp_endpoint = "ws://h:1" if cdp else None
        browser = Browser()
        engine = playwright_scraper._PlaywrightEngine(browser, args, autosolve=False, user_agent=None, client=None)
        session = asyncio.run(engine.open(proxy if use_proxy else None))
        routes = session.context.routes
        assert bool(routes) is want and engine.media_blocked is want, (argv, use_proxy, cdp, routes)
        if want:
            assert routes[0] == ("**/*", playwright_scraper._media_route)
    args = playwright_scraper.build_arg_parser().parse_args(["--url", _BIKE_URL, "--max-results", "6"])
    args._solve_budget = page_flow.SolveBudget(0)
    engine = _FakeEngine({"*": {"html": _fx("search_bike_nyc")}}, None)
    engine.can_paginate = False
    engine.media_blocked = True
    with tempfile.TemporaryDirectory() as td:
        args.out = str(Path(td) / "o.json")
        args.delay_between_pages = 0
        asyncio.run(page_flow.run(engine, args, urls=[_BIKE_URL], proxy_pool=None, client=None, started_at=0.0))
        assert json.loads(Path(args.out + ".meta.json").read_text())["media_blocked"] is True


class _CDPClient:
    def __init__(self):
        self.sent, self.handlers = [], {}

    def on(self, name, fn):
        self.handlers[name] = fn

    async def send(self, cmd, params=None):
        self.sent.append((cmd, params))


def _pyppeteer_page():
    import argparse as _argparse
    sessions = []

    async def create():
        sessions.append(_CDPClient())
        return sessions[-1]
    async def set_viewport(_size):
        pass
    page = _argparse.Namespace(_client=_CDPClient(), target=_argparse.Namespace(createCDPSession=create),
                               setViewport=set_viewport)
    return page, sessions


@check("media (pyppeteer): an authenticated proxy's own Fetch interception also fails images/media/fonts (no second Fetch domain on the page); a proxy without a password gets proxy_pool.pyppeteer_block_media; nothing without blocking")
def _():
    async def paused(page, *events):
        for event in events:
            page._client.handlers["Fetch.requestPaused"](event)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    auth_proxy = proxy_pool.parse_proxy_line("h:1:u:p")
    page, sessions = _pyppeteer_page()
    took = asyncio.run(puppeteer_scraper._authenticate_if_needed(page, auth_proxy, block_media=True))
    asyncio.run(paused(page, {"requestId": "i", "resourceType": "Image"}, {"requestId": "d", "resourceType": "Document"},
                       {"requestId": "f", "resourceType": "Font"}))
    sent = page._client.sent
    assert took is True and sessions == []
    assert [c for c, _ in sent].count("Fetch.enable") == 1
    assert ("Fetch.failRequest", {"requestId": "i", "errorReason": "BlockedByClient"}) in sent
    assert ("Fetch.failRequest", {"requestId": "f", "errorReason": "BlockedByClient"}) in sent
    assert ("Fetch.continueRequest", {"requestId": "d"}) in sent

    page, _ = _pyppeteer_page()
    asyncio.run(puppeteer_scraper._authenticate_if_needed(page, auth_proxy, block_media=False))
    asyncio.run(paused(page, {"requestId": "i", "resourceType": "Image"}))
    assert ("Fetch.continueRequest", {"requestId": "i"}) in page._client.sent, "no blocking unless asked"

    class Browser:
        def __init__(self, page):
            self.page = page

        async def newPage(self):
            return self.page
    for proxy, block, want_session in ((proxy_pool.parse_proxy_line("h:1"), True, True),
                                       (auth_proxy, True, False), (None, False, False)):
        page, sessions = _pyppeteer_page()
        asyncio.run(puppeteer_scraper._open_page(Browser(page), proxy=proxy, user_agent=None, autosolve=False,
                                                 block_media=block))
        assert bool(sessions) is want_session, (proxy, block, sessions)
        if want_session:
            patterns = dict(sessions[0].sent)["Fetch.enable"]["patterns"]
            assert sorted(p["resourceType"] for p in patterns) == ["Font", "Image", "Media"]


@check("media (Selenium): Network.setBlockedURLs by extension (images, video, fonts — never scripts or styles) right after a proxied local driver is created; none without a proxy, with --load-media or over --cdp-endpoint")
def _():
    from unittest import mock

    class Driver:
        def __init__(self, *a, **k):
            self.sent = []

        def execute_cdp_cmd(self, cmd, params):
            self.sent.append((cmd, params))

        def quit(self):
            pass

    Opts = _SeleniumOptions
    fake_webdriver = type("W", (), {"Chrome": Driver})
    with mock.patch.object(selenium_scraper, "webdriver", fake_webdriver), \
            mock.patch.object(selenium_scraper, "Options", Opts), mock.patch.object(selenium_scraper, "Service", None):
        for argv, use_proxy, cdp, want in (([], True, False, True), (["--load-media"], True, False, False),
                                           ([], False, False, False), ([], True, True, False)):
            args = selenium_scraper.build_arg_parser().parse_args(["--query", "x", *argv])
            args.cdp_endpoint = "ws://127.0.0.1:9222" if cdp else None
            engine = selenium_scraper._SeleniumEngine(args, user_agent=None, client=None)
            session = asyncio.run(engine.open(proxy_pool.parse_proxy_line("h:1") if use_proxy else None))
            blocked = dict(session.driver.sent).get("Network.setBlockedURLs", {}).get("urls", [])
            assert bool(blocked) is want and engine.media_blocked is want, (argv, use_proxy, cdp, session.driver.sent)
            if want:
                assert all(p in blocked for p in ("*.jpg", "*.webp?*", "*.mp4", "*.woff2"))
                assert not any(p.startswith(("*.js", "*.css")) for p in blocked)


@check("media: every local-browser engine consults should_block_media and calls its own blocker (a source check: the suite runs without a browser binary)")
def _():
    for name, call in (("playwright_scraper", "_media_route"), ("selenium_scraper", "selenium_block_media"),
                       ("puppeteer_scraper", "pyppeteer_block_media")):
        source = (ROOT / f"{name}.py").read_text(encoding="utf-8")
        assert "should_block_media(" in source and f"{call}" in source, name
        assert "page_flow.precheck_proxy(" in source, name


# --------------------------------------------------------------------------- #
# Proxy traffic meter and --proxy-budget-mb (proxy_meter.py; its own byte
# counts against a stub proxy are in tests/test_proxy_meter.py)
# --------------------------------------------------------------------------- #
import proxy_meter  # noqa: E402

_SOFA_URL = "https://www.facebook.com/marketplace/nyc/search/?query=sofa"


class _MeteredEngine(_FakeEngine):
    """_FakeEngine whose navigations, wheel scrolls and pagination requests
    cost `cost` proxy bytes each."""

    def __init__(self, by_url, cost):
        super().__init__(by_url)
        self.cost = cost
        self.can_paginate = True

    async def open(self, proxy):
        session = await super().open(proxy)
        for name in ("goto", "wheel", "post_form"):
            original = getattr(session, name)

            async def metered(*a, _original=original):
                proxy_meter.current().add(self.cost)
                return await _original(*a)
            setattr(session, name, metered)
        return session


def _metered_run(by_url, urls, *, budget_mb, cost, active=True, argv=()):
    import contextlib
    import io
    args = playwright_scraper.build_arg_parser().parse_args(
        ["--url", urls[0], "--max-results", "500", "--delay-between-pages", "0",
         "--proxy-budget-mb", str(budget_mb), *argv])
    args._solve_budget = page_flow.SolveBudget(0)
    proxy_meter.start(args.proxy_budget_mb, active=active)
    engine = _MeteredEngine(by_url, cost)
    err = io.StringIO()
    try:
        with tempfile.TemporaryDirectory() as td:
            args.out = str(Path(td) / "o.json")
            Path(args.out).write_text("[]")  # a previous run's data
            with contextlib.redirect_stderr(err):
                rc = asyncio.run(page_flow.run(engine, args, urls=urls, proxy_pool=None, client=None, started_at=0.0))
            read = lambda suffix: json.loads(Path(args.out + suffix).read_text()) if Path(args.out + suffix).exists() else None  # noqa: E731
            meta, attempt, rows = read(".meta.json"), read(output_writer.ATTEMPT_META_SUFFIX), read("")
    finally:
        proxy_meter.start(None, active=False)
    return rc, meta, attempt, rows, engine, err.getvalue()


@check("proxy budget: once the metered bytes pass --proxy-budget-mb the run sends no further pagination request and opens no further search — the rows already read are kept, exit 6, stop_reason proxy_budget_exceeded, the sidecar carries proxy_bytes / proxy_budget_bytes / proxy_budget_exceeded, and one 'Proxy traffic' line is printed")
def _():
    answers = [(200, _answer(n)) for n in range(1, 10)]
    # 1,000-byte budget, 400 bytes per navigation, wheel or request: over it on the first replayed request.
    rc, meta, attempt, rows, eng, err = _metered_run({_BIKE_URL: _paged(answers=answers), "*": _paged(answers=answers)},
                                                     [_BIKE_URL, _SOFA_URL], budget_mb=0.001, cost=400)
    assert rc == output_writer.EXIT_PARTIAL, (rc, err[-500:])
    assert len(eng.sessions) == 1 and len(eng.sessions[0].posted) == 1, "no request and no search after the budget"
    assert rows and len(rows) == 18, len(rows or [])
    assert meta["status"] == "partial" and meta["stop_reason"] == "proxy_budget_exceeded", meta
    assert (meta["proxy_bytes"], meta["proxy_budget_bytes"], meta["proxy_budget_exceeded"]) == (1200, 1000, True), meta
    assert meta["pages"][0]["pagination"] == "proxy_budget_exceeded" and meta["pages"][1]["result"] == "not_attempted", meta["pages"]
    assert err.count("Proxy traffic: 0.0 MB of 0.001 MB budget") == 1, err[-500:]
    assert attempt is None, "a run that wrote its rows has nothing to put in the attempt sidecar"
    # Spent by the first wheel round: no second round, no request.
    rc, meta, _a, rows, eng, err = _metered_run({_BIKE_URL: _paged(answers=answers, request_after=3)}, [_BIKE_URL],
                                                budget_mb=0.001, cost=600)
    assert rc == output_writer.EXIT_PARTIAL and eng.sessions[0].wheels == 1 and not eng.sessions[0].posted, (rc, eng.sessions[0].wheels)
    assert len(rows) == 6 and meta["stop_reason"] == "proxy_budget_exceeded"


@check("proxy budget: spent before any listing was read -> exit 5 (not 4, not 3); the previous data and .meta.json stay, and <out>.latest_attempt.meta.json records stop_reason proxy_budget_exceeded with the byte count; --details is not started after the budget")
def _():
    rc, meta, attempt, rows, eng, err = _metered_run({"*": {"html": "<html><title>x</title></html>"}},
                                                     [_BIKE_URL, _SOFA_URL], budget_mb=0.001, cost=5000)
    assert rc == output_writer.EXIT_REMOTE_API_ERROR, (rc, err[-400:])
    assert rows == [] and meta is None, "the previous data stays and no .meta.json is written"
    assert (attempt["stop_reason"], attempt["proxy_bytes"], attempt["proxy_budget_exceeded"]) == \
        ("proxy_budget_exceeded", 5000, True), attempt
    assert attempt["data_updated"] is False and len(eng.sessions) == 1, attempt
    assert "Proxy traffic: 0.0 MB of 0.001 MB budget" in err
    # A navigation that failed after spending the budget is not retried.
    rc, meta, attempt, rows, eng, err = _metered_run({"*": {"goto": RuntimeError("net::ERR_TIMED_OUT")}}, [_BIKE_URL],
                                                     budget_mb=0.001, cost=5000, argv=["--retries", "2", "--retry-delay", "0"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and len(eng.fetched) == 1, (rc, eng.fetched)
    assert attempt["stop_reason"] == "proxy_budget_exceeded", attempt
    rc, meta, _a, rows, eng, err = _metered_run({_BIKE_URL: {"html": _fx("search_bike_nyc")}}, [_BIKE_URL],
                                                budget_mb=0.001, cost=5000, argv=["--details"])
    assert rc == output_writer.EXIT_PARTIAL and len(eng.sessions) == 1 and len(rows) == 6, (rc, len(eng.sessions))


@check("proxy budget: with no local proxy nothing is metered or stopped — proxy_bytes is null, no 'Proxy traffic' line; --proxy-budget-mb 0 is unlimited; a run under its budget is complete (exit 0) and records its bytes")
def _():
    end = [(200, _answer(1)), (200, _answer(2, has_next=False))]
    rc, meta, _a, rows, _e, err = _metered_run({_BIKE_URL: _paged(answers=end)}, [_BIKE_URL], budget_mb=0.001,
                                               cost=10 ** 9, active=False)
    assert rc == output_writer.EXIT_OK and meta["proxy_bytes"] is None and meta["proxy_budget_exceeded"] is False, meta
    assert "Proxy traffic" not in err
    rc, meta, _a, rows, _e, err = _metered_run({_BIKE_URL: _paged(answers=end)}, [_BIKE_URL], budget_mb=0, cost=10 ** 9)
    assert rc == output_writer.EXIT_OK and meta["proxy_budget_bytes"] is None and meta["proxy_bytes"] > 10 ** 9, meta
    rc, meta, _a, rows, _e, err = _metered_run({_BIKE_URL: _paged(answers=end)}, [_BIKE_URL], budget_mb=10, cost=1000)
    assert rc == output_writer.EXIT_OK and meta["proxy_bytes"] == 4000 and meta["proxy_budget_bytes"] == 10 ** 7, (rc, meta)
    assert "Proxy traffic: 0.0 MB of 10 MB budget" in err


@check("proxy budget: finish_run puts the budget stop above every other outcome — a run that also met a block or a failed page still ends 6 with rows and 5 without, stop_reason proxy_budget_exceeded")
def _():
    import dataclasses
    rows = [output_writer.Product(**{f.name: None for f in dataclasses.fields(output_writer.Product)
                                     if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING})]
    rows[0].sku = "facebook-marketplace-1"
    try:
        for products, blocked, failed, want in ((rows, True, [], 6), ([], True, [], 5), (rows, False, [2], 6), ([], False, [], 5)):
            proxy_meter.start(0.001, active=True)
            proxy_meter.current().add(5000)
            with tempfile.TemporaryDirectory() as td:
                out = str(Path(td) / "o.json")
                rc = output_writer.finish_run(products=products, out_path=out, fmt="json", engine="t", url="u",
                                              pages_requested=2, pages_completed=1, failed_pages=failed, blocked=blocked,
                                              remote_api_error=False, allow_empty=False, started_at=0.0)
                path = Path(out + (".meta.json" if products else output_writer.ATTEMPT_META_SUFFIX))
                meta = json.loads(path.read_text())
            assert rc == want and meta["stop_reason"] == "proxy_budget_exceeded", (blocked, failed, rc, meta)
            assert meta["proxy_budget_exceeded"] is True and meta["proxy_bytes"] == 5000, meta
    finally:
        proxy_meter.start(None, active=False)


@check("proxy budget: --proxy-budget-mb is on every engine (default 10, 0 = unlimited, a negative or non-number value is a usage error naming the flag); the meter is active only for a LOCAL proxy — never over --cdp-endpoint, --scraper-api or with no proxy")
def _():
    import argparse
    import contextlib
    import io
    for mod in _ENGINES:
        parser = mod.build_arg_parser()
        assert parser.parse_args(["--query", "x"]).proxy_budget_mb == 10, mod.__name__
        assert "--proxy-budget-mb" in parser.format_help(), mod.__name__
        for bad in ("-1", "lots"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                try:
                    parser.parse_args(["--query", "x", "--proxy-budget-mb", bad])
                    raise AssertionError("accepted %s" % bad)
                except SystemExit as exc:
                    assert exc.code == 2
            assert "--proxy-budget-mb" in err.getvalue(), err.getvalue()
        source = (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
        assert source.index("page_flow.start_proxy_meter(") < source.index("page_flow.precheck_proxy("), mod.__name__
    pool = proxy_pool.ProxyPool([proxy_pool.parse_proxy_line("h:1")])
    ns = lambda **k: argparse.Namespace(**{"proxy_budget_mb": 10, "cdp_endpoint": None, "scraper_api": False, **k})  # noqa: E731
    try:
        assert page_flow.start_proxy_meter(ns(), pool).active is True
        assert page_flow.start_proxy_meter(ns(), None).active is False
        assert page_flow.start_proxy_meter(ns(cdp_endpoint="ws://h"), pool).active is False
        assert page_flow.start_proxy_meter(ns(scraper_api=True), pool).active is False
    finally:
        proxy_meter.start(None, active=False)


@check("proxy meter wiring: Playwright counts a proxied LOCAL context (requestfinished + requestfailed) and adds the budget route after the media route; Selenium tees its performance log; pyppeteer opens a counting CDP session; none of it over --cdp-endpoint or with no proxy")
def _():
    import argparse
    from unittest import mock

    class Context:
        def __init__(self):
            self.routes, self.events = [], []

        def on(self, name, fn):
            self.events.append(name)

        async def route(self, pattern, handler):
            self.routes.append(handler)

        async def new_page(self):
            return type("P", (), {"on": lambda *a: None})()

    class Browser:
        def __init__(self):
            self.contexts = [Context()]

        async def new_context(self, **kw):
            return Context()

    proxy = proxy_pool.parse_proxy_line("h:1:u:p")
    try:
        for use_proxy, cdp, argv, want_events, want_routes in (
                (True, False, [], True, 2), (True, False, ["--proxy-budget-mb", "0"], True, 1),
                (True, False, ["--load-media"], True, 1), (False, False, [], False, 0), (True, True, [], False, 0)):
            args = playwright_scraper.build_arg_parser().parse_args(["--query", "x", *argv])
            args.cdp_endpoint = "ws://h:1" if cdp else None
            proxy_meter.start(args.proxy_budget_mb, active=use_proxy and not cdp)
            engine = playwright_scraper._PlaywrightEngine(Browser(), args, autosolve=False, user_agent=None, client=None)
            session = asyncio.run(engine.open(proxy if use_proxy else None))
            context = session.context
            assert (sorted(set(context.events)) == ["requestfailed", "requestfinished"]) is want_events, (argv, context.events)
            assert len(context.routes) == want_routes, (argv, use_proxy, cdp, context.routes)
            if want_routes and args.proxy_budget_mb:
                assert context.routes[-1] == proxy_meter.current().playwright_budget_route_async

        class Driver:
            def __init__(self, *a, **k):
                self.sent = []

            def execute_cdp_cmd(self, cmd, params):
                self.sent.append((cmd, params))

            def get_log(self, kind):
                return []

            def quit(self):
                pass

        with mock.patch.object(selenium_scraper, "webdriver", type("W", (), {"Chrome": Driver})), \
                mock.patch.object(selenium_scraper, "Options", _SeleniumOptions), \
                mock.patch.object(selenium_scraper, "Service", None):
            for use_proxy, cdp, want in ((True, False, True), (False, False, False), (True, True, False)):
                args = selenium_scraper.build_arg_parser().parse_args(["--query", "x"])
                args.cdp_endpoint = "ws://127.0.0.1:9222" if cdp else None
                proxy_meter.start(10, active=use_proxy and not cdp)
                engine = selenium_scraper._SeleniumEngine(args, user_agent=None, client=None)
                session = asyncio.run(engine.open(proxy_pool.parse_proxy_line("h:1") if use_proxy else None))
                assert hasattr(session.driver, "_proxy_meter_poll") is want, (use_proxy, cdp)

        attached = []

        async def fake_attach(self, page):
            attached.append(page)

        async def fake_launch(**kw):
            return "browser"

        async def fake_open_page(browser, **kw):
            return argparse.Namespace(on=lambda *a: None)
        with mock.patch.object(puppeteer_scraper, "_launch", fake_launch), \
                mock.patch.object(puppeteer_scraper, "_open_page", fake_open_page), \
                mock.patch.object(proxy_meter.ProxyMeter, "attach_pyppeteer", fake_attach):
            for use_proxy, remote, want in ((True, None, 1), (False, None, 0), (True, "remote", 0)):
                attached.clear()
                args = puppeteer_scraper.build_arg_parser().parse_args(["--query", "x"])
                proxy_meter.start(10, active=use_proxy and not remote)
                engine = puppeteer_scraper._PyppeteerEngine(args, remote_browser=remote, autosolve=False,
                                                            user_agent=None, client=None)
                asyncio.run(engine.open(proxy if use_proxy else None))
                assert len(attached) == want, (use_proxy, remote, attached)
    finally:
        proxy_meter.start(None, active=False)


@check("canary: both runs pass --proxy-budget-mb 25 (above the scraper's 10 MB default) and each is followed by an always-run 'Proxy traffic' step for its own output; that step writes '**canary**: proxy traffic X.X MB of 25 MB' to the job summary with the budget read from the sidecar's proxy_budget_bytes (not a fixed number), is red with '::error::stopped: proxy budget 25 MB exceeded' when the run stopped at the budget, and copes with a missing sidecar")
def _():
    import subprocess
    import sys
    if not (ROOT / ".github").is_dir():
        return  # the Docker image ships no workflows; the repository's own CI runs this check
    text = (ROOT / ".github" / "workflows" / "canary.yml").read_text(encoding="utf-8")
    runs = [line for line in text.replace("\\\n", " ").splitlines() if "_scraper.py" in line and "--out" in line]
    # 25 MB, measured 2026-10-21: the --details run used 11.4 MB, over the 10 MB default.
    assert len(runs) == 2 and all(re.search(r"--proxy-budget-mb 25(?!\d)", line) for line in runs), runs
    steps = text.split("- name: ")
    for out, label in (("canary.json", "canary"), ("canary_details.json", "canary (details)")):
        summary_steps = [s for s in steps if s.startswith("Proxy traffic") and
                         re.search(r'python3 \.github/proxy_traffic_summary\.py %s "%s"\s*$' % (re.escape(out), re.escape(label)),
                                   s, re.M)]
        assert len(summary_steps) == 1 and "if: always()" in summary_steps[0], (out, "no summary step")
        assert text.index("--out " + out) < text.index("proxy_traffic_summary.py " + out)
    script = str(ROOT / ".github" / "proxy_traffic_summary.py")
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.md"
        env = dict(os.environ, GITHUB_STEP_SUMMARY=str(summary))

        def summarise(meta=None, attempt=None):
            for suffix, data in ((".meta.json", meta), (".latest_attempt.meta.json", attempt)):
                path = Path(tmp) / ("canary.json" + suffix)
                path.unlink() if path.exists() else None
                if data is not None:
                    path.write_text(json.dumps(data), encoding="utf-8")
            return subprocess.run([sys.executable, script, "canary.json", "canary"], cwd=tmp, env=env,
                                  capture_output=True, text=True, timeout=60)
        proc = summarise({"proxy_bytes": 4_321_000, "proxy_budget_bytes": 25 * 10 ** 6, "proxy_budget_exceeded": False,
                          "finished_at": 2})
        assert proc.returncode == 0 and "::error::" not in proc.stdout, proc.stdout
        assert summary.read_text().splitlines()[-1] == "**canary**: proxy traffic 4.3 MB of 25 MB"
        proc = summarise(None, {"proxy_bytes": 25_200_000, "proxy_budget_bytes": 25 * 10 ** 6,
                                "proxy_budget_exceeded": True, "finished_at": 3})
        assert proc.returncode == 1 and "::error::stopped: proxy budget 25 MB exceeded" in proc.stdout, proc.stdout
        assert summary.read_text().splitlines()[-1] == "**canary**: proxy traffic 25.2 MB of 25 MB"
        # The budget is the run's own, not a fixed figure: a 10 MB run reads "of 10 MB",
        # a sidecar with no proxy_budget_bytes falls back to 10 MB, and null (no cap) reads "of no".
        proc = summarise({"proxy_bytes": 10_200_000, "proxy_budget_bytes": 10 ** 7, "proxy_budget_exceeded": True,
                          "finished_at": 2})
        assert proc.returncode == 1 and "::error::stopped: proxy budget 10 MB exceeded" in proc.stdout, proc.stdout
        assert summary.read_text().splitlines()[-1] == "**canary**: proxy traffic 10.2 MB of 10 MB"
        proc = summarise({"proxy_bytes": 2_000_000, "proxy_budget_exceeded": False, "finished_at": 2})
        assert summary.read_text().splitlines()[-1] == "**canary**: proxy traffic 2.0 MB of 10 MB", "fallback 10"
        proc = summarise({"proxy_bytes": 2_000_000, "proxy_budget_bytes": None, "proxy_budget_exceeded": False,
                          "finished_at": 2})
        assert summary.read_text().splitlines()[-1] == "**canary**: proxy traffic 2.0 MB of no", "no cap"
        proc = summarise({"proxy_bytes": 1, "proxy_budget_exceeded": False, "finished_at": 1},
                         {"proxy_bytes": 10_200_000, "proxy_budget_exceeded": True, "finished_at": 5})
        assert proc.returncode == 1, "the newer of the two sidecars is the run's"
        proc = summarise()
        assert proc.returncode == 0 and "not recorded" in summary.read_text().splitlines()[-1], proc.stdout
        proc = summarise({"proxy_bytes": None, "proxy_budget_exceeded": False})
        assert proc.returncode == 0 and "no local proxy" in summary.read_text().splitlines()[-1]
        (Path(tmp) / "canary.json.meta.json").write_text("{not json", encoding="utf-8")
        proc = subprocess.run([sys.executable, script, "canary.json", "canary"], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0 and "unreadable" in proc.stdout, proc.stdout


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
