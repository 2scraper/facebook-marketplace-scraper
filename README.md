# facebook-marketplace-scraper

![release](https://img.shields.io/github/v/release/2scraper/facebook-marketplace-scraper?sort=semver)
![tests](https://github.com/2scraper/facebook-marketplace-scraper/actions/workflows/tests.yml/badge.svg)
![canary](https://github.com/2scraper/facebook-marketplace-scraper/actions/workflows/canary.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![no login](https://img.shields.io/badge/runs%20without-an%20account-success)

**Scrape Facebook Marketplace listings into clean JSON or CSV, without
logging in.** Search by keyword or browse a category in any Marketplace
location, filter by price and age, and get one row per listing: title,
price, location, when it was listed, its photo, sold / pending, delivery
options and, on vehicles, the mileage. With `--details`, also each
listing's description, condition, category, every photo and the currency
code.

- **No account, no cookies, no key.** It reads what Marketplace shows any
  logged-out visitor: the listings a search page embeds.
- **Measured live on 2026-10-06** from an ordinary residential IP with no
  key and no proxy: 5 searches in New York, London, Berlin, Los Angeles
  and Chicago — 100 listings in 25 seconds; and 10 listings with
  `--details` in 102 seconds, every one with its description, condition,
  photos and currency.
- **Nothing about sellers.** Sellers are private people; the tool never
  writes a seller field (see [Sellers](#sellers)).
- **Honest results.** A blocked, empty or partial run says so in its exit
  code and a `.meta.json` file next to the output. A run that finds
  nothing never overwrites your last good data.
- **Three browser engines** (Playwright, Puppeteer, Selenium) running one
  shared fetch loop, a browserless **Scraper API** mode, rotating proxies,
  2Captcha's **Scraping Browser API** over CDP, and a run-to-run diff tool
  that spots price drops and sold listings.

Companion repos: [facebook-ads-scraper](https://github.com/2scraper/facebook-ads-scraper)
(Meta's Ad Library) and [facebook-pages-scraper](https://github.com/2scraper/facebook-pages-scraper)
(public Pages).

## Quick start

```bash
git clone https://github.com/2scraper/facebook-marketplace-scraper.git
cd facebook-marketplace-scraper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium

python3 playwright_scraper.py --query bike --location nyc
```

Results land in `facebook_marketplace.json`, with
`facebook_marketplace.json.meta.json` beside it. No `.env` is needed for
this.

**When you need more than that:** for many searches, a specific exit
country, or no browser on your machine, put a 2Captcha residential proxy
or key in `.env` (`cp .env.example .env`, `FACEBOOK_PROXY=...`,
`TWOCAPTCHA_KEY=...`). `python3 env_config.py` shows what was picked up,
without printing secrets.

## Examples

```bash
# a keyword search in a location (the slug from facebook.com/marketplace/<location>/)
python3 playwright_scraper.py --query bike --location nyc

# with the site's own filters: a price range, newest first, listed in the last 7 days, as CSV
python3 playwright_scraper.py --query sofa --location london --min-price 50 --max-price 300 \
    --sort creation_time_descend --days-since-listed 7 --format csv

# browse a category instead of searching
python3 playwright_scraper.py --category vehicles --location la

# each listing's own page too: description, condition, category, photos, currency
python3 playwright_scraper.py --query "standing desk" --location chicago --details

# searches or listings copied from your browser, one per line (# comments allowed)
python3 playwright_scraper.py --urls-file searches.txt
python3 playwright_scraper.py --url "https://www.facebook.com/marketplace/item/1800446554437755/"

# the same with Puppeteer, or Selenium
python3 puppeteer_scraper.py --query bike --location nyc
python3 selenium_scraper.py --query bike --location nyc

# without a browser: 2Captcha's Scraper API (needs TWOCAPTCHA_KEY)
python3 playwright_scraper.py --scraper-api --query bike --location nyc

# what changed since last time: price drops, listings sold
python3 diff_runs.py monday.json tuesday.json
```

## Sample output

One row of `sample_output.json`, cut from a live run with `--details` on
2026-10-06 (media URLs shortened here):

```json
{
  "sku": "facebook-listing-1655081135947682",
  "source": "facebook.com",
  "category": "listing",
  "title": "Kugoo Pink Kids Bike with Training Wheels",
  "brand": null,
  "price": 50.0,
  "currency": "USD",
  "price_source": "item_page",
  "product_url": "https://www.facebook.com/marketplace/item/1655081135947682/",
  "image_url": "https://scontent.fakx4-2.fna.fbcdn.net/v/t39.84726-6/8299099…",
  "scraped_at": "2026-10-06T09:36:02Z",
  "listing_id": "1655081135947682",
  "price_text": "$50",
  "original_price": null,
  "city": "New York",
  "state": "NY",
  "location_text": "New York, NY",
  "listed_at": "2026-10-02T20:30:44Z",
  "is_sold": false,
  "is_pending": false,
  "status": "AVAILABLE",
  "category_id": "1658310421102081",
  "subtitle": null,
  "delivery_types_json": "[\"IN_PERSON\"]",
  "description": "Vibrant pink Kugoo kids bicycle has a white front basket and matching training wheels. …",
  "category_name": "Bicycles",
  "category_slug": "bicycles",
  "condition": "Used - like new",
  "attributes_json": "[{\"name\": \"Condition\", \"value\": \"Used - like new\"}]",
  "photo_urls_json": "[\"https://scontent.fakx4-2.fna.fbcdn.net/v/t39.84726-6/82990…\"]",
  "shipping_offered": false,
  "search_url": "https://www.facebook.com/marketplace/nyc/search/?query=bike",
  "position": 1
}
```

What the columns mean:

| Column | |
|---|---|
| `sku` / `listing_id` | `facebook-listing-{id}` / the listing's id. A listing two searches both found is one row |
| `price`, `price_text`, `original_price` | the asking price as a number (0.0 for a free item), as displayed ("$1,100"), and a struck-out earlier price |
| `currency` | an ISO code — from an unambiguous symbol in the displayed price (£ → GBP, € → EUR), or from the listing's own page with `--details`. **A bare "$" is left empty**: the search does not say which dollar |
| `price_source` | `search` or `item_page` |
| `city`, `state`, `location_text` | the listing's location as Marketplace shows it (city level) |
| `listed_at` | when it was listed (UTC) |
| `is_sold`, `is_pending`, `status` | as shown; `status` is the listing page's own status (`AVAILABLE` on every one seen), or `REMOVED` when `--details` found it gone |
| `category_id`, `subtitle`, `delivery_types_json` | Marketplace's category id; on vehicles the mileage ("73K miles"); IN_PERSON, DOOR_PICKUP, DOOR_DROPOFF, PUBLIC_MEETUP (the values seen) |
| `description`, `category_name`, `category_slug`, `condition`, `attributes_json`, `photo_urls_json`, `shipping_offered` | with `--details` only. `category_name` is the listing's own Marketplace category ("Household"); the breadcrumb a browser shows comes from another taxonomy ("Home Goods › Table Lamps" for the same lamp, checked 2026-10-06) |
| `search_url`, `position` | the search the row came from, and its place in it (from 1, across the search's pagination) |
| `brand` | not shown by Marketplace, always empty |

Media URLs are signed CDN links and stop working after some days.

## How a search is read

1. Open the search (or category) logged out, with `locale=en_US` added
   to the address so the page is in English whatever the machine's
   language is.
2. Read the 24 listings the page embeds.
3. **Paginate.** A logged-out search embeds 24, and the page asks for more
   only when it is really scrolled: the tool closes the login dialog,
   scrolls with the mouse wheel in a 1366×900 window, keeps the request
   the page sends (`CometMarketplaceSearchContentPaginationQuery`), and
   sends it again from the page with each next cursor, 3 seconds apart.
   Measured 2026-10-06: 150 listings of a Chicago search in 35 seconds;
   25 pages in a row (573 listings) before Facebook answered "Rate limit
   exceeded" — which 3.5 minutes of waiting did not lift, so the tool
   stops that search there (`stop_reason: rate_limited`, partial, every
   row kept) instead of waiting. Three things that do NOT work, found on
   the way: a programmatic `window.scrollTo` (no request at all), a
   1280×720 window (no request), and scrolling past the second page
   without the replay (the page shows a "Log in or sign up" banner).
4. With `--details`, open each listing's own page, in order, and read its
   description, currency code, category, condition, location text,
   photos and shipping. That is one more page load per listing (about 10
   seconds each at the default pace).

The site's filters in the URL are honoured: `minPrice` / `maxPrice` (a
$100–$300 search returned 17 listings, all in range, cheapest first),
`sortBy` and `daysSinceListed`. A search with no results says so ("No
listings found") and is reported as empty — exit 4, not a failure. The
bare location page (`/marketplace/nyc/`) has no search in it and is
refused.

## Sellers

Sellers on Marketplace are private people. A logged-out visitor is
sometimes sent the seller's name: `marketplace_listing_seller` was
`null` on 353 of 370 listings captured from a Kazakh address on
2026-10-05/06, and named a person on every listing of a New York search
fetched through an EU proxy later the same day. **This tool never writes a seller field**, whether
the page sends one or not, and the test suite checks that a listing
carrying one gives a row without a trace of it. Listing descriptions are
written as the seller published them.

## Captchas

None, so far. No page in the live runs of 2026-10-06 (Playwright,
Puppeteer and Selenium, direct and through a residential proxy, and the
Scraper API) carried a captcha widget, iframe or challenge. The generic
captcha detection stays on, so a real one would be reported as blocked
rather than read as data.

## Volume and blocks

From one residential IP on 2026-10-05 and 2026-10-06, more than 30
search and listing pages read normally, 11 of them back to back at the
default 2-second pace. We have not found where the limit is. If Facebook starts
refusing an address, the tool reports it as **blocked** (a login page,
or a page that is not facebook.com's under 401/403/429), never as an
empty search. Without a proxy pool it stops after 3 blocked answers in a
row — also during `--details` — and reports the rest as `not_attempted`,
keeping every row already read. To read more: rotate residential exits
with `--proxy-file`, and keep `--delay-between-pages` at 2 seconds or
more.

**A datacentre address has not been measured yet.** The daily canary
runs from a GitHub runner only when a proxy secret is configured.

## Scraper API mode

With `--scraper-api` no browser is driven: each search, and each listing
with `--details`, is one 2Captcha Scraper API call (`TWOCAPTCHA_KEY` in
`.env`), routed through the Scraping Browser profile in
`FACEBOOK_CDP_ENDPOINT` when one is set. Everything this tool reads is in
the page itself, so the rows are the same as a browser engine's.
**Measured 2026-10-06: on the Scraper API's own pool, Marketplace
answered with its login page** (reported as blocked, exit 3); routed
through a Scraping Browser profile, it read the search — 14 listings
rather than 24, the same as a browser on that profile. Set
`FACEBOOK_CDP_ENDPOINT` for this mode. There is no live page to paginate
in this mode: a search gives the listings its page embeds, and the run is
marked `capped` when more were announced. A refused key, an empty balance or
an empty answer ends the run with exit 5.

## Run results and exit codes

Every run writes `<out>` and `<out>.meta.json`: status, `stop_reason`,
per input address what happened and how many rows it gave, how many
listing pages `--details` read, `not_found_urls`, failed addresses with
reasons, whether `--max-results` capped the run, solves spent, and a
hash of the output file. A run that collects nothing writes neither, so
it never replaces your previous good file.

| Exit | Meaning |
|---|---|
| `0` | complete |
| `6` | partial: rows were written, but the run did not finish cleanly — `stop_reason` says why (`blocked`, `not_painted`, `failed_pages`, `rejected_rows`, `parse_error`, `remote_api_error`, `proxy_pool_exhausted`) |
| `3` | blocked, no rows |
| `4` | no rows: no listings for the search, or the listing is gone |
| `5` | nothing could be read (fetch, parse or Scraper API failure); no rows |
| `2` | bad usage, including input where every line was skipped |
| `1` | crash (a bug; please report it) |

**CSV and safe writes.** In a CSV export, text that a spreadsheet would
run as a formula (starting with `=`, `+`, `-` or `@`, or with a literal
leading apostrophe) gets a leading `'`; the sidecar records this
(`csv_text_encoding: apostrophe-v1`) and `diff_runs.py` restores the
original text before comparing. JSON is unchanged. Every output file and
sidecar is written to a temporary file first and swapped in only once
complete, so a failed write never leaves a half-written file over the
previous good one.

## Monitoring changes

```bash
# price drops, listings sold, as JSON
python3 diff_runs.py monday.json tuesday.json --json

# exit 1 if anything changed (for cron or CI)
python3 diff_runs.py monday.json tuesday.json --fail-on-change
```

`diff_runs.py` matches rows by listing id. A different price is a price
change (and a different currency is reported apart, never as a price
change); a listing marked sold or pending, re-titled, re-described or
moved is a field change. A listing missing from the new run has usually
just left the window a search shows, so it is `left_selection`; it is
`removed` only when the new run opened its page and found it gone. The
diff refuses runs of different searches, `--details` or `--max-results`,
a run that did not complete, and a `.meta.json` that does not match its
file.

## Options

Same flags for all three engines. Credentials go in `.env`
(`TWOCAPTCHA_KEY`, `FACEBOOK_PROXY`, `FACEBOOK_CDP_ENDPOINT`), never on
the command line.

| Option | Default | |
|---|---|---|
| `--query` / `--category` | | a keyword search, or a category slug (vehicles, electronics, …) to browse — with `--location` |
| `--location` | | the Marketplace location slug or id from `facebook.com/marketplace/<location>/`: nyc, la, london, berlin, … |
| `--min-price` / `--max-price` | | a price range, in the location's currency |
| `--sort` | best match | `price_ascend`, `price_descend`, `creation_time_descend`, `distance_ascend` |
| `--days-since-listed` | | 1, 7 or 30 |
| `--url` / `--urls-file` | | instead of the flags above: a Marketplace search, category or listing URL as copied from a browser, or a file of them |
| `--details` | off | also open each listing's own page |
| `--max-results` | 100 | listings to write in total; past the first 24 of a search the tool paginates (3s per 24) |
| `--delay-between-pages` | 2s | pause between page loads |
| `--format` / `--out` | json / `facebook_marketplace.<format>` | output format and path |
| `--proxy` / `--proxy-file` / `--proxy-shuffle` | `FACEBOOK_PROXY` | one proxy or a rotating pool, for a local browser |
| `--proxy-block-retries` | 3 | blocked answers before that proxy is dropped from the pool |
| `--scraper-api` | off | no browser: fetch each page through 2Captcha's Scraper API (needs `TWOCAPTCHA_KEY`) |
| `--cdp-endpoint` | `FACEBOOK_CDP_ENDPOINT` | connect to a Scraping Browser API profile instead of launching a browser |
| `--solve-captcha` | when-blocked | `off` disables the Browser API's own captcha auto-solve; there is no local solver |
| `--max-solves` / `--min-score` | 8 / 0.3 | kept for the local solver, which is disabled; they change nothing today |
| `--retries` / `--retry-delay` | 2 / 3s | navigation retries per page |
| `--fingerprint` / `--fp-tags` / `--fp-country` | off | apply a 2Captcha Fingerprint API user agent (local browsers only) |
| `--dump-html` | off | save each page's HTML next to the output, for debugging |
| `--allow-empty` | off | write an output file even when nothing was found |
| `--headless` / `--headful` | headless | show the browser window |

## Engines

- **Playwright** (`playwright_scraper.py`) is the recommended engine.
- **Puppeteer** (`puppeteer_scraper.py`, via pyppeteer) supports the same
  modes. pyppeteer itself is no longer maintained, and its own proxy login
  no longer works on current Chromium, so this engine answers the proxy's
  password prompt itself (over CDP `Fetch`).
- **Selenium** (`selenium_scraper.py`) runs a local Chrome only.
  chromedriver cannot authenticate a Scraping Browser endpoint (the run
  exits 2 before fetching; use `--scraper-api` instead), and its
  `--proxy-server` cannot use a proxy password (the credentials are
  stripped, with a warning; a proxy that needs its password then answers
  HTTP 407 and the run says so — allow your IP in the proxy's settings
  instead).

Install one engine per virtualenv (`requirements-playwright.txt`,
`requirements-puppeteer.txt`, `requirements-selenium.txt`). Their
dependencies conflict with each other.

All three engines share one fetch loop (`page_flow.py`), so they agree on
results, exit codes and when money is spent. Docker:
`docker build -t facebook-marketplace-scraper .` gives an image with
Playwright and Chromium.

## Known limitations

- **About 600 listings per search from one exit**, then Facebook's rate
  limit (measured 2026-10-06). Split a search (price ranges, a category)
  or rotate exits (`--proxy-file`) for more. A browser on the Scraping
  Browser profile tried got 14 per page instead of 24.
- **A price sort with `--days-since-listed` comes back empty.** Measured
  2026-10-06 in New York and London: `price_ascend` or `price_descend`
  together with `daysSinceListed` answered "No listings found", while each
  alone, and `creation_time_descend` or `distance_ascend` with the days
  filter, returned listings. The tool warns before running such a search.
- **No seller information**, by design (see above).
- **"$" has no currency code** without `--details`.
- **Locations are slugs.** Use the one in the address bar when you open
  Marketplace for a city (`nyc`, `la`, `chicago`, `london`, `berlin` were tested); a
  numeric location id works too.

## Is this allowed?

Facebook's `robots.txt` disallows crawlers, and Meta's terms forbid
automated collection without its permission. This tool reads only what
Marketplace shows any logged-out visitor, does not log in, and never
collects anything about sellers. Whether your use is permitted depends
on your jurisdiction and purpose — check before you run it.

## Development

```bash
python3 smoke_test.py            # offline checks, no network, no engine needed
python3 .github/ci_checks.py     # credential scan
python3 -m unittest discover -s tests -p 'test_*.py'  # failure and recovery scenarios
```

Parser and flow checks run on fixtures cut from real captures in
`tests/fixtures/`. CI runs the offline suite on Python 3.9 and 3.12,
installs the built wheel outside the checkout, builds the Docker image
and launches Chromium in it, and runs each engine in its own virtualenv.
`TESTING.md` describes live testing; `CHANGELOG.md` has the history.

## Licence

MIT, see `LICENSE`.
