# Testing with real credentials and the live site

**Live status, 2026-10-06.** From one residential IP, logged out, no key
and no proxy, Playwright unless noted:

| Run | Result |
|---|---|
| `--urls-file` (New York bike, London sofa, Berlin Fahrrad, LA vehicles, Chicago desk $50–200) | 100 listings (24+24+24+24, then capped at `--max-results 100`), exit 0, 25s; GBP and EUR filled from the symbol, "$" left empty |
| `--query bike --location nyc --details --max-results 10` | 10/10 with description, condition, category, photos and USD, exit 0, 102s — `sample_output.*` is cut from this run |
| `--category vehicles --location chicago` | 24, each with a mileage subtitle, exit 0 |
| a listing URL | 1 row from its own page, exit 0 |
| `--query zqxwvjkpqzzz --location nyc` | the site's own "No listings found": exit 4, nothing written |
| `--location "new york"`; the bare `/marketplace/nyc/` page | exit 2, nothing requested |
| Selenium, `--query fahrrad --location berlin` | 24, all EUR, exit 0 |
| Puppeteer, `--query sofa --location london`; with `--details --max-results 2` in New York | 24, all GBP; 2/2 with details; exit 0 |
| Playwright + a 2Captcha residential proxy, `--details --max-results 2` | 2/2 with details, exit 0 (an earlier attempt hit an exit that closed every connection: 3 attempts, exit 5, reported) |
| Puppeteer + the residential proxy, `--query tv --location la` | 24, exit 0 |
| Scraping Browser API (`FACEBOOK_CDP_ENDPOINT`, a `country-us` profile): Playwright `--details`, Puppeteer, Selenium | 2/2 with details; **14** listings per search (not 24) on both searches tried; Selenium exit 2 up front |
| `--scraper-api` through that profile | 14 listings; `--details` 1/1 |
| `--scraper-api` on its own pool | **refused**: the login page (a Finnish exit) — exit 3; once an empty answer — exit 5 |
| `--scraper-api` with an invalid key | exit 5 after one call |

The filters, measured one at a time and together on the same day:
`minPrice`/`maxPrice` alone, `daysSinceListed` alone, `sortBy` alone,
price range + price sort, price range + days, and newest-first or
distance + days all return listings; **a price sort together with
`daysSinceListed` returned "No listings found" every time** (New York and
London, both price sorts). The tool warns before running such a search.

Seller: `marketplace_listing_seller` was `null` on 353 of 370 captured
listings and named a person on 17; no row of any run above has a seller
field.

Bugs found by these runs and fixed the same day:

- **pyppeteer's browser cleanup could hang forever** after a proxy closed
  the connection (20+ minutes, after the page had already been reported
  failed). Every cleanup step is now bounded at 10 seconds and a local
  Chromium that will not close is killed. (Shared engine code: fixed in
  facebook-ads-scraper and facebook-pages-scraper too.)
- **A login page served under the asked-for address** (the Scraper API's
  pool) was reported as an unreadable page. It is now a block (exit 3),
  recognised by its canonical link. (All three repos.)
- **An empty Scraper API answer** was not retried. It is now an API
  error, retried, exit 5 when it persists. (All three repos.)
- **A listing with a malformed id vanished silently**; it is now counted
  in `rejected_rows`.

**Pagination, 2026-10-06 (later):** Playwright through the residential
proxy, `--query sofa --location chicago --max-results 150`: 150 listings,
7 pagination answers, no duplicates, complete, 35s. Puppeteer through the
proxy, `--query desk --location la --max-results 80`: 80, complete.
Selenium from this machine's own address: the page sent its pagination
requests (8 answers) but the address was already rate-limited by the
probing below, so the run stopped `rate_limited` with the 24 it had —
reported, not hidden. Probing: 25 replays at 3s gave 573 distinct
listings, then "Rate limit exceeded", still in force 3.5 minutes later;
at 1.5s the limit came after 15. A 1280×720 window and a programmatic
scroll sent no request at all; 1366×900 with a wheel scroll did.
Found on the way: Selenium's `--window-size=1366,900` left a 1366×757
page, too short — the page size is now set over CDP.

**After the pre-release security review, 2026-10-06:** the pyppeteer
engine through the password-protected residential proxy still read
normally (the proxy's own auth challenge answered, nothing else), with no
password in any log; a CSV run records `csv_text_encoding` and
`diff_runs.py` reads it back to the original text.

Not yet run live: `--proxy-file` with several exits, the Docker image,
and any run from a datacentre IP.

The quickest real check:

```bash
python3 playwright_scraper.py --query bike --location nyc --out /tmp/mp.json
cat /tmp/mp.json.meta.json        # status: complete, product_count: 100 (24 embedded, the rest paginated)
```

## 1. Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium
cp .env.example .env              # only for the paid paths below
python3 env_config.py             # what was picked up, secrets masked
```

## 2. One search, and what the rows should look like

```bash
python3 playwright_scraper.py --query bike --location nyc --details --max-results 5 --out /tmp/mp.json --dump-html
```

- exit 0, `status: complete`, `details_read: 5`;
- five rows with `price`, `city`, `listed_at`, `image_url`, and from
  `--details` a `description`, `condition`, `category_name` and
  `currency: USD`; `position` 1–5; no seller anywhere.

Open a few `product_url`s in a browser and compare.

## 3. The failure answers

```bash
python3 playwright_scraper.py --query zqxwvjkpqzzz --location nyc --out /tmp/e.json      # exit 4, nothing written
python3 playwright_scraper.py --url https://www.facebook.com/marketplace/nyc/            # exit 2, nothing requested
python3 playwright_scraper.py --query x --location nyc --sort price_ascend --days-since-listed 7   # warns: expect no listings
```

## 4. Puppeteer and Selenium

```bash
pip install -r requirements-puppeteer.txt   # in its own venv
PYPPETEER_EXECUTABLE_PATH=/path/to/chromium python3 puppeteer_scraper.py --query sofa --location london

pip install -r requirements-selenium.txt    # in its own venv
python3 selenium_scraper.py --query fahrrad --location berlin
```

Same rows, same exit codes.

## 5. The residential proxy (`--proxy` / `FACEBOOK_PROXY`)

Put `FACEBOOK_PROXY=http://login:password@host:port` in `.env` and run
section 2 again. The log names the exit, never the password. An exit that
closes every connection is reported (exit 5 for that page) — rotate with
`--proxy-file`.

## 6. Scraper API mode (`--scraper-api`)

```bash
python3 playwright_scraper.py --scraper-api --query bike --location nyc --out /tmp/sapi.json   # with FACEBOOK_CDP_ENDPOINT set
```

On 2026-10-06 the Scraper API's own pool got the login page for
Marketplace (exit 3); routed through a Scraping Browser profile it read
the search.

## 7. The Scraping Browser API (`--cdp-endpoint`)

```bash
python3 playwright_scraper.py --query guitar --location nyc --details --max-results 2   # with FACEBOOK_CDP_ENDPOINT in .env
```

Selenium refuses a credentialled endpoint up front (exit 2).

## 8. Push to GitHub and let CI do the rest

`tests.yml` runs the offline suite on Python 3.9 and 3.12, builds the
wheel and the Docker image, and runs one `engine-smoke` job per engine.
`canary.yml` needs the `FACEBOOK_PROXY` repo secret; without it the job
skips with a notice. Dispatch it once by hand and check both branches.

## 9. What "done" looks like

Every row in the tables above has a date and a number. Add the open items
from the top of this file to the table as they are run, and update the
README's claims from the same runs.
