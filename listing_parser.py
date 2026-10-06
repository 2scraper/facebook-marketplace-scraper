#!/usr/bin/env python3
"""listing_parser.py — this IS the Facebook Marketplace site knowledge.

Written 2026-10-06 from live captures of facebook.com/marketplace fetched
logged out (no account, no cookies) from a residential IP: keyword
searches in New York, Los Angeles, Chicago, London and Berlin, the same
search with a price range and with "newest first", a category page
(vehicles), the bare location page, and a listing's own page. The
trimmed captures are in `tests/fixtures/`.

What a logged-out visitor is actually served:

  - **A search answers HTTP 200 with 24 listings embedded in the page**
    (`marketplace_search.feed_units`), `has_next_page: true`. The next
    ones come from the page's own pagination request — see "Past the
    first 24" below: a programmatic scroll (what the first recon tried)
    triggers nothing, a wheel scroll at a large enough window does, and
    the request then answers 24 more per cursor until a rate limit
    (~600 listings per search from one exit, measured).
  - **The URL's filters are honoured**: `minPrice` / `maxPrice` (17
    listings, all within the range, on one capture), `sortBy`
    (`creation_time_descend`, `price_ascend`, …) and `daysSinceListed`.
  - **A listing in a search** carries its id, title, price (`amount`
    "110.00" and the displayed `formatted_amount` "$110"), a struck-out
    earlier price, the city and state, the main photo, the time it was
    listed, sold / pending flags, its category id, its delivery types
    and — on vehicles — a subtitle such as "73K miles". The search
    carries NO currency code: "$" is the US, Canadian or Australian
    dollar alike, so `currency` is filled only from an unambiguous
    symbol (£, €, …) or, with `--details`, from the listing's own page.
  - **The seller is sometimes sent** to a logged-out visitor: `null` on
    353 of 370 listings captured on 2026-10-05/06 from a Kazakh address,
    but named on every listing of a New York search fetched through an EU
    proxy later on 2026-10-06 (embedded and paginated alike). Sellers are
    private persons; the tool collects NOTHING about them, sent or not.
  - **A listing's own page** (`/marketplace/item/<id>/`) adds the
    description, the currency code, the category name, the condition
    ("Used - like new"), the location text, every photo, and whether
    shipping is offered.
  - **The location page alone** (`/marketplace/nyc/`) embeds no listings;
    a search or a category is needed.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from typing import Any, Iterator, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse

from bs4 import BeautifulSoup

from output_writer import Product

log = logging.getLogger("listing_parser")

BASE_URL = "https://www.facebook.com/marketplace"
SOURCE = "facebook.com"

ROUTE_ERROR = "comet.fbweb.CometErrorRoute"
ROUTE_ITEM = "comet.fbweb.CometMarketplaceHoistedPermalinkRoute"
NOT_FOUND_TEXT = "This content isn't available right now"
NO_RESULTS_MARKER = "MarketplaceSearchFeedNoResults"
LOGIN_PATHS = ("/login", "/checkpoint")
BOT_CHALLENGE_MARKERS: tuple = ()  # none seen on any capture

SORTS = ("best_match", "price_ascend", "price_descend", "creation_time_descend", "distance_ascend")
DAYS = (1, 7, 30)
# Symbols that name ONE currency. "$", "¥", "kr" and "R" do not.
_SYMBOL_CURRENCY = {"£": "GBP", "€": "EUR", "₹": "INR", "₩": "KRW", "₺": "TRY", "₪": "ILS", "₱": "PHP",
                    "₫": "VND", "zł": "PLN", "₴": "UAH", "₦": "NGN", "฿": "THB", "R$": "BRL", "CA$": "CAD",
                    "A$": "AUD", "US$": "USD", "MX$": "MXN", "NZ$": "NZD", "HK$": "HKD", "CHF": "CHF"}

_LOCATION_RE = re.compile(r"^[A-Za-z0-9_\-]{2,60}$")
_ITEM_ID_RE = re.compile(r"^\d{5,25}$")
_NOT_A_LOCATION = frozenset(("item", "search", "category", "you", "inbox", "notifications", "create", "selling",
                             "buying", "saved", "profile", "help", "learn_more"))
_HOSTS = ("facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com")


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def search_url(*, location: str, query: Optional[str] = None, category: Optional[str] = None,
               min_price: Optional[int] = None, max_price: Optional[int] = None, sort: Optional[str] = None,
               days_since_listed: Optional[int] = None) -> str:
    """The URL the site's own search box and filters produce. A keyword
    `query` searches; a `category` slug (vehicles, electronics, …) browses
    that category; one of the two is needed."""
    loc = (location or "").strip()
    if not _LOCATION_RE.match(loc) or loc.lower() in _NOT_A_LOCATION:
        raise ValueError(f"--location must be a Marketplace location slug or numeric id, e.g. nyc, la, london (got {location!r})")
    if bool(query) == bool(category):
        raise ValueError("give exactly one of --query or --category")
    if category is not None and not _LOCATION_RE.match(category):
        raise ValueError(f"--category must be a Marketplace category slug such as vehicles (got {category!r})")
    if sort is not None and sort not in SORTS:
        raise ValueError(f"--sort must be one of {', '.join(SORTS)}")
    if days_since_listed is not None and days_since_listed not in DAYS:
        raise ValueError("--days-since-listed must be 1, 7 or 30")
    for name, value in (("--min-price", min_price), ("--max-price", max_price)):
        if value is not None and value < 0:
            raise ValueError(f"{name} must be >= 0")
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ValueError("--min-price is above --max-price")
    params = []
    if query:
        params.append(("query", query.strip()))
    if min_price is not None:
        params.append(("minPrice", str(min_price)))
    if max_price is not None:
        params.append(("maxPrice", str(max_price)))
    if sort and sort != "best_match":
        params.append(("sortBy", sort))
    if days_since_listed is not None:
        params.append(("daysSinceListed", str(days_since_listed)))
    path = f"{BASE_URL}/{loc}/search/" if query else f"{BASE_URL}/{loc}/{category}/"
    return path + ("?" + urlencode(params) if params else "")


def fetch_url(url: str) -> str:
    """The address actually opened: plus `locale=en_US`, because the page
    language follows the browser and the error route's own words are read
    in English (the same fix facebook-pages-scraper needed — pyppeteer on a
    Russian-language machine got the Russian page despite --lang=en-US)."""
    return url + ("&" if "?" in url else "?") + "locale=en_US"


# --------------------------------------------------------------------------- #
# Past the first 24: the search's own pagination request
# --------------------------------------------------------------------------- #
# Measured 2026-10-06, logged out: once the login dialog is closed, a WHEEL
# scroll (not a programmatic scrollTo, which the earlier recon used and
# which triggers nothing) makes the page send this query for the next 24;
# after one more the page shows a "Log in or sign up" banner and stops
# asking. The request itself keeps working: sent again from the page with
# the next cursor it returned 24 new listings each time — 25 times in a row
# (573 distinct listings) before Facebook answered "Rate limit exceeded",
# which 3.5 minutes of waiting did not lift.
PAGINATION_QUERY = "CometMarketplaceSearchContentPaginationQuery"
# The window the page is opened in decides whether it paginates at all:
# measured 2026-10-06, at 1280x720 (Playwright's default) a wheel scroll
# sent no request in any variant tried, at 1366x900 it did every time.
# Every engine opens the page at this size.
VIEWPORT = {"width": 1366, "height": 900}
GRAPHQL_PATH = "/api/graphql"
# The login dialog drawn over a logged-out page; the page is opened in
# English (fetch_url), so its close button is labelled so.
DIALOG_CLOSE_SELECTOR = 'div[role="dialog"] [aria-label="Close"]'
RATE_LIMIT_TEXT = "Rate limit exceeded"


def graphql_payloads(body: str) -> List[Any]:
    """One JSON document per line, maybe behind Facebook's `for (;;);`."""
    out = []
    for line in (body or "").splitlines():
        line = line.strip()
        if line.startswith("for (;;);"):
            line = line[len("for (;;);"):]
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def is_pagination_request(post_data: Optional[str]) -> bool:
    """The page's own "next 24" request, decided on its form body."""
    return f"fb_api_req_friendly_name={PAGINATION_QUERY}" in (post_data or "")


def is_rate_limited(body: Optional[str]) -> bool:
    return RATE_LIMIT_TEXT in (body or "")


def page_info(payloads: List[Any]) -> tuple:
    """(has_next_page, end_cursor) of the LAST search connection in a page
    or a pagination answer; (None, None) when there is none."""
    has_next, cursor = None, None
    for payload in payloads:
        for node in _find(payload, "page_info"):
            info = node.get("page_info")
            if isinstance(info, dict) and "end_cursor" in info:
                has_next = info.get("has_next_page") if isinstance(info.get("has_next_page"), bool) else None
                cursor = info.get("end_cursor") if isinstance(info.get("end_cursor"), str) else None
    return has_next, cursor


def next_request(post_data: str, cursor: str) -> Optional[str]:
    """The page's own pagination form with only the cursor changed — every
    other field (the search's params, the anonymous session's lsd token) as
    the page sent it. None when the form is not one."""
    form = parse_qsl(post_data or "", keep_blank_values=True)
    out, found = [], False
    for key, value in form:
        if key == "variables":
            try:
                variables = json.loads(value)
            except ValueError:
                return None
            variables["cursor"] = cursor
            value = json.dumps(variables, separators=(",", ":"))
            found = True
        out.append((key, value))
    return urlencode(out) if found else None


def known_empty_combination(url: str) -> Optional[str]:
    """Why a search URL is known to come back empty, or None. Measured
    2026-10-06: a PRICE sort together with daysSinceListed answered "No
    listings found" in New York and London alike, while each filter alone,
    and either with another sort, returned listings. The site's answer, not
    this tool's; said up front so it is not taken for a broken scraper."""
    params = dict(parse_qsl(urlparse(url).query))
    if params.get("sortBy") in ("price_ascend", "price_descend") and params.get("daysSinceListed"):
        return ("a price sort together with --days-since-listed: Marketplace answered 'No listings found' for "
                "every such search tried (2026-10-06) — drop one of the two")
    return None


def item_url(listing_id: str) -> str:
    return f"{BASE_URL}/item/{listing_id}/"


def classify(url: str) -> Optional[tuple]:
    """('search', url) / ('item', listing id) for a Marketplace address, else None."""
    raw = (url or "").strip()
    if not raw:
        return None
    parts = urlparse(raw if "://" in raw else "https://" + raw)
    if (parts.hostname or "").lower() not in _HOSTS or parts.username or parts.port:
        return None
    seg = [s for s in parts.path.split("/") if s]
    if not seg or seg[0] != "marketplace":
        return None
    if len(seg) >= 3 and seg[1] == "item" and _ITEM_ID_RE.match(seg[2]):
        return ("item", seg[2])
    if len(seg) >= 3 and _LOCATION_RE.match(seg[1]) and seg[1].lower() not in _NOT_A_LOCATION:
        if seg[2] == "search" and dict(parse_qsl(parts.query)).get("query"):
            return ("search", seg[1])
        if seg[2] != "search" and _LOCATION_RE.match(seg[2]) and len(seg) == 3:
            return ("search", seg[1])
    return None


def normalize_input(text: str) -> Optional[str]:
    """A pasted Marketplace search, category or listing URL, as the one
    address to open (query kept, tracking parameters dropped), or None."""
    kind = classify(text)
    if kind is None:
        return None
    if kind[0] == "item":
        return item_url(kind[1])
    raw = text.strip()
    parts = urlparse(raw if "://" in raw else "https://" + raw)
    keep = [(k, v) for k, v in parse_qsl(parts.query) if k in ("query", "minPrice", "maxPrice", "sortBy",
                                                             "daysSinceListed", "itemCondition", "exact", "radius")]
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return "https://www.facebook.com" + path + ("?" + urlencode(keep) if keep else "")


def refusal_reason(text: str) -> str:
    raw = (text or "").strip()
    parts = urlparse(raw if "://" in raw else "https://" + raw)
    seg = [s for s in parts.path.split("/") if s]
    if seg[:1] == ["marketplace"] and len(seg) <= 2:
        return "a Marketplace page with no search, category or listing in it (the location page embeds no listings)"
    if seg[:1] == ["marketplace"]:
        return "not a Marketplace search, category or listing address"
    return "not a facebook.com/marketplace address"


# --------------------------------------------------------------------------- #
# Reading a page
# --------------------------------------------------------------------------- #
def _find(obj: Any, key: str) -> Iterator[dict]:
    """Every dict (depth-first, document order) that has `key`."""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if key in node:
                yield node
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))


def json_blocks(html: str) -> List[Any]:
    out = []
    for script in BeautifulSoup(html or "", "html.parser").find_all("script", type="application/json"):
        raw = script.string or script.get_text()
        if not any(k in raw for k in ('"marketplace_listing_title"', '"canonicalRouteName"', '"listing_photos"',
                                      '"redacted_description"')):
            continue  # ~100 blocks per page; a handful carry listings
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


_ROUTE_RE = re.compile(r'"canonicalRouteName":"([^"]+)"')
_TITLE_RE = re.compile(r"<title[^>]*>([^<]{0,120})</title>", re.I)


def page_route(html: str) -> Optional[str]:
    m = _ROUTE_RE.search(html or "")
    return m.group(1) if m else None


def page_title(html: str) -> Optional[str]:
    m = _TITLE_RE.search(html or "")
    return m.group(1).strip() if m else None


def listings(blocks: List[Any]) -> List[dict]:
    """Search-result listings, each once, in the site's order. A listing
    node is the dict that carries `marketplace_listing_title` and a
    `listing_price`; the item page's related listings are excluded by the
    caller (see detail())."""
    out, seen = [], set()
    for block in blocks:
        for node in _find(block, "marketplace_listing_title"):
            lid = node.get("id")
            # A malformed id is still passed on: listing_row() refuses it and
            # the run counts it as rejected, rather than it vanishing here.
            if not isinstance(lid, str) or "listing_price" not in node or lid in seen:
                continue
            seen.add(lid)
            out.append(node)
    return out


# A login page announces itself in its canonical link even when the
# address it was fetched under does not change — measured 2026-10-06: the
# Scraper API's own pool was served the login page (canonical
# https://fi-fi.facebook.com/login) for a Marketplace search, and with no
# final URL to go by it was misread as an unknown page rather than a block.
_LOGIN_CANONICAL_RE = re.compile(r'<link[^>]+rel="canonical"[^>]+href="https?://[a-z-]*\.?facebook\.com/login', re.I)


def is_login_page(html: str) -> bool:
    return bool(_LOGIN_CANONICAL_RE.search(html or ""))


def page_state(html: str, *, final_url: Optional[str] = None) -> str:
    """`content` (listings embedded), `empty` (a Marketplace page with none),
    `not_found` (the error route), `login`, `loading` or `unknown`."""
    route = page_route(html) or ""
    if route == ROUTE_ERROR or (not route and NOT_FOUND_TEXT in (html or "")):
        return "not_found"
    path = urlparse(final_url or "").path
    if any(path.startswith(p) for p in LOGIN_PATHS) or route.endswith("LoginRoute") or is_login_page(html):
        return "login"
    if '"marketplace_listing_title"' in (html or "") and listings(json_blocks(html)):
        return "content"
    if route.startswith("comet.fbweb.CometMarketplace"):
        # The site says "no results" in so many words (a feed unit of type
        # MarketplaceSearchFeedNoResults, "No listings found" — measured
        # 2026-10-06); a Marketplace route without it and without
        # listings is still painting.
        return "empty" if NO_RESULTS_MARKER in (html or "") else "loading"
    return "unknown"


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #
def _price(value: Any) -> Optional[float]:
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None
    return None


def currency_from_symbol(formatted: Optional[str]) -> Optional[str]:
    """ISO code for an UNAMBIGUOUS symbol in a displayed price, else None."""
    if not formatted:
        return None
    prefix = re.match(r"^\s*([^\d\s.,-]+)", formatted) or re.search(r"([^\d\s.,-]+)\s*$", formatted)
    if not prefix:
        return None
    return _SYMBOL_CURRENCY.get(prefix.group(1).strip())


def _uri(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        image = value.get("image")
        value = value.get("uri") or (image.get("uri") if isinstance(image, dict) else None)
    return value if isinstance(value, str) and value.startswith("http") else None


def _json_list(values: List[Any]) -> Optional[str]:
    return json.dumps(values, ensure_ascii=False) if values else None


def _iso(ts: Any) -> Optional[str]:
    if not isinstance(ts, int) or isinstance(ts, bool) or ts <= 0:
        return None
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_sku(listing_id: str) -> str:
    return f"facebook-listing-{listing_id}"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def listing_row(node: dict, *, search: Optional[str], position: int, scraped_at: Optional[str] = None) -> Optional[Product]:
    """One search-result listing as a row, or None without an id."""
    lid = node.get("id")
    if not isinstance(lid, str) or not lid.isdigit():
        return None
    price = node.get("listing_price") if isinstance(node.get("listing_price"), dict) else {}
    struck = node.get("strikethrough_price") if isinstance(node.get("strikethrough_price"), dict) else {}
    geo = ((node.get("location") or {}).get("reverse_geocode") or {}) if isinstance(node.get("location"), dict) else {}
    formatted = price.get("formatted_amount") or price.get("formatted_amount_zeros_stripped")
    subtitles = [s.get("subtitle") for s in node.get("custom_sub_titles_with_rendering_flags") or []
                 if isinstance(s, dict) and isinstance(s.get("subtitle"), str)]
    city_page = geo.get("city_page") if isinstance(geo.get("city_page"), dict) else {}
    return Product(
        sku=make_sku(lid),
        source=SOURCE,
        category="listing",
        title=node.get("marketplace_listing_title") if isinstance(node.get("marketplace_listing_title"), str) else None,
        brand=None,
        price=_price(price.get("amount")),
        currency=currency_from_symbol(formatted),
        price_source="search" if _price(price.get("amount")) is not None else None,
        product_url=item_url(lid),
        image_url=_uri(node.get("primary_listing_photo")),
        scraped_at=scraped_at or now_iso(),
        listing_id=lid,
        price_text=formatted if isinstance(formatted, str) else None,
        original_price=_price(struck.get("amount")),
        city=geo.get("city") or None,
        state=geo.get("state") or None,
        location_text=city_page.get("display_name") or None,
        listed_at=_iso(node.get("creation_time")),
        is_sold=node.get("is_sold") if isinstance(node.get("is_sold"), bool) else None,
        is_pending=node.get("is_pending") if isinstance(node.get("is_pending"), bool) else None,
        category_id=node.get("marketplace_listing_category_id") if isinstance(node.get("marketplace_listing_category_id"), str) else None,
        subtitle=" · ".join(subtitles) or None,
        delivery_types_json=_json_list([d for d in node.get("delivery_types") or [] if isinstance(d, str)]),
        search_url=search,
        position=position,
    )


def detail(html: str, listing_id: str) -> Optional[dict]:
    """The listing's OWN object on its item page (the page also embeds
    related listings — matched on the id, never the first one found)."""
    for block in json_blocks(html):
        for node in _find(block, "redacted_description"):
            if node.get("id") == listing_id:
                return node
    return None


def listing_photos(html: str, listing_id: str) -> List[dict]:
    """The listing's photos: on its page they sit on a separate object with
    the same id, not on the one carrying the description."""
    for block in json_blocks(html):
        for node in _find(block, "listing_photos"):
            if node.get("id") == listing_id and isinstance(node.get("listing_photos"), list):
                return [p for p in node["listing_photos"] if isinstance(p, dict)]
    return []


def apply_detail(row: Product, node: dict, photo_items: Optional[List[dict]] = None) -> Product:
    """Fill a row from the listing's own page: description, currency,
    category, condition, location text, photos, shipping."""
    price = node.get("listing_price") if isinstance(node.get("listing_price"), dict) else {}
    desc = node.get("redacted_description")
    lo = node.get("marketplaceListingRenderableIfLoggedOut") if isinstance(node.get("marketplaceListingRenderableIfLoggedOut"), dict) else {}
    cat = lo.get("marketplace_listing_category") if isinstance(lo.get("marketplace_listing_category"), dict) else {}
    attrs = [a for a in node.get("attribute_data") or [] if isinstance(a, dict)]
    condition = next((a.get("label") for a in attrs if a.get("attribute_name") == "Condition"), None)
    photos = []
    for item in photo_items or node.get("listing_photos") or []:
        uri = _uri(item.get("image")) if isinstance(item, dict) else None
        if uri and uri not in photos:
            photos.append(uri)
    if isinstance(price.get("currency"), str):
        row.currency = price["currency"]
    if row.price is None and _price(price.get("amount")) is not None:
        row.price = _price(price.get("amount"))
    if row.price is not None:
        row.price_source = "item_page"
    if not row.title and isinstance(node.get("marketplace_listing_title"), str):
        row.title = node["marketplace_listing_title"]
    row.description = (desc.get("text") or "").strip() or None if isinstance(desc, dict) else None
    row.category_name = lo.get("marketplace_listing_category_name") if isinstance(lo.get("marketplace_listing_category_name"), str) else row.category_name
    row.category_slug = cat.get("slug") if isinstance(cat.get("slug"), str) else row.category_slug
    row.condition = condition
    row.attributes_json = _json_list([{"name": a.get("attribute_name"), "value": a.get("label") or a.get("value")} for a in attrs
                                      if a.get("attribute_name")])
    text = node.get("location_text")
    row.location_text = (text.get("text") if isinstance(text, dict) else None) or row.location_text
    row.photo_urls_json = _json_list(photos)
    if photos and not row.image_url:
        row.image_url = photos[0]
    row.shipping_offered = node.get("is_shipping_offered") if isinstance(node.get("is_shipping_offered"), bool) else None
    row.status = node.get("renderable_listing_status") if isinstance(node.get("renderable_listing_status"), str) else None
    if isinstance(node.get("is_sold"), bool):
        row.is_sold = node["is_sold"]
    if isinstance(node.get("is_pending"), bool):
        row.is_pending = node["is_pending"]
    if row.listed_at is None:
        row.listed_at = _iso(node.get("creation_time"))
    return row


def item_row(html: str, listing_id: str, *, scraped_at: Optional[str] = None) -> Optional[Product]:
    """A listing opened by its own URL: the row from its page alone."""
    node = detail(html, listing_id)
    if node is None:
        return None
    base = dict(node)
    base.setdefault("id", listing_id)
    row = listing_row(base, search=None, position=1, scraped_at=scraped_at)
    return apply_detail(row, node, listing_photos(html, listing_id)) if row is not None else None
