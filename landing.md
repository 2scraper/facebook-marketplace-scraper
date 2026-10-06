# Facebook Marketplace Scraper by 2scraper

**Open-source scraper for Facebook Marketplace listings — any location, keyword or category, price filters, no login, three engines, JSON or CSV.**

Search Marketplace by keyword or browse a category in any location, with the site's own price, sort and age filters, and get one row per listing: title, price, location, listed time, photo, sold / pending, delivery options and vehicle mileage — plus, if you want them, each listing's description, condition, category, every photo and its currency.

[**View source on GitHub →**](https://github.com/2scraper/facebook-marketplace-scraper)

---

## Before you scrape

Facebook's `robots.txt` disallows crawlers, and Meta's terms forbid automated collection without its permission. This tool reads only what Marketplace shows any logged-out visitor, never logs in, never contacts sellers and never collects anything about them. Whether your use is allowed depends on your jurisdiction and purpose: check first.

## What to expect

Every row comes from the data a Marketplace page embeds and from its own pagination. Measured live on 2026-10-06: 150 listings of one Chicago search in 35 seconds; with no key and no proxy, 5 searches in New York, London, Berlin, Los Angeles and Chicago — the 24 each embeds — in 25 seconds; 10 listings with their own pages in 102 seconds. Past the 24 a search page embeds, the tool follows the page's own pagination — 150 listings of one search in 35 seconds, about 600 per search from one address before Facebook's rate limit. Details in the [README](https://github.com/2scraper/facebook-marketplace-scraper#readme).

## What you get

- Free, open-source scraper, one script per engine — **Playwright** (recommended), **Puppeteer** (via pyppeteer) and **Selenium**, all producing the identical output schema and exit codes
- Keyword searches and category browsing in any Marketplace location, with price range, sort and "listed in the last N days"; or search, category and listing URLs pasted from a browser
- Listing fields: title, price and struck-out price, city and state, listed time, main photo, sold / pending, category, delivery types, vehicle mileage
- `--details`: description, condition, category name, every photo, currency code, shipping
- JSON and CSV export, with a documented `Product` schema and a `.meta.json` sidecar
- Change monitoring: `diff_runs.py` shows price drops and listings sold between two runs
- A browserless mode (`--scraper-api`) that needs no browser driver installed

## 2Captcha products, when you want them

| Product | What it's for |
|---|---|
| **Proxies — 2captcha.com/proxy** (2prx.com is the same product, different name) | Many searches from several addresses: residential exits in `.env` or `--proxy-file`, rotated per page with per-exit failure tracking |
| **Scraper API — 2captcha.com** | No browser at all: `--scraper-api` fetches each page from 2Captcha's side, one HTTP call each |
| **Scraping Browser API — 2captcha.com** | A remote browser session over CDP with its own proxy, fingerprint and captcha auto-solve bundled — `--cdp-endpoint` |
| **Browser fingerprints — 2captcha Fingerprint API** | Pick a Fingerprint API profile by OS and country for a locally-launched browser (applied as its user agent) |

## Who this is for

Resellers and price researchers tracking what things sell for locally, dealers watching vehicle listings, and anyone who wants Marketplace results in a spreadsheet rather than a browser tab. Seller data, messaging and anything behind a login are out of scope.

## Get started

```bash
git clone https://github.com/2scraper/facebook-marketplace-scraper.git
cd facebook-marketplace-scraper
pip install -r requirements-playwright.txt && playwright install chromium

python3 playwright_scraper.py --query bike --location nyc --max-price 300 --format csv --out bikes.csv
```

Full setup, CLI reference, and configuration details in the [repository README](https://github.com/2scraper/facebook-marketplace-scraper#readme).

---

**Need it running at scale, with proxies, fingerprints, and captcha solving already configured?**
[Talk to us →](https://2captcha.com/contact) · Proxies by [2captcha.com/proxy](https://2captcha.com/proxy) · Scraping Browser API & captcha solving by [2captcha.com](https://2captcha.com)
