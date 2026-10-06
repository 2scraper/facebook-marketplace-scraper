# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org/) as closely as a CLI toolkit can: a patch
release means "fixes", not that every flag and default is frozen — a fix
that changes a default is called out at the top of its entry.

## [Unreleased]

## [0.1.1] - 2026-10-06

Text only — no change to what is read or written.

### Fixed

- `--max-solves` help: the local solver is disabled, so it caps nothing
  today (the README already said so); the landing pages no longer promise
  a browser-specific fingerprint (`--fp-tags` filters by OS only).
- The exit-code table lists every `stop_reason` a partial run can carry.
- --max-results help no longer says a search gives at most 24 (the tool paginates); README states the measured pagination limit instead of 'we have not found where the limit is'; the headline numbers say which were measured before pagination; TESTING and README tell the same story about sellers.

## [0.1.0] - 2026-10-06

First release: Facebook Marketplace, read logged out. Run live on
2026-10-06 with all three engines and the Scraper API mode (see
`TESTING.md`).

### Added

- Searches built from flags — `--query` or `--category` in a
  `--location`, with `--min-price`, `--max-price`, `--sort` and
  `--days-since-listed`, the site's own URL filters — or Marketplace
  search, category and listing URLs pasted with `--url` / `--urls-file`.
- One row per listing: title, price (number and as displayed), struck-out
  earlier price, city, state, listed time, main photo, sold / pending,
  category id, delivery types and, on vehicles, the mileage subtitle. The
  currency code only where the symbol names one currency.
- Pagination past the 24 a search embeds: the page's own next-page
  request, captured after a wheel scroll and sent again with each cursor
  (all three browser engines); stops at `--max-results`, the end, or
  Facebook's rate limit (partial, rows kept).
- `--details`: each listing's own page for its description, currency
  code, category, condition, photos, location text, shipping and status;
  a listing gone by then is kept from the search, marked `REMOVED`.
- Every page is opened with `locale=en_US`, so the site's own words are
  read in English whatever the machine's language.
- Nothing about sellers is ever written (a seller's name is sometimes
  sent; the suite checks it never reaches a row).
- `--scraper-api`: one 2Captcha Scraper API call per page, no browser.
- `diff_runs.py`: price drops, listings sold or changed; a listing that
  left the window a search shows is `left_selection`, one whose page is gone
  is `removed`.
- Offline suite on fixtures cut from real captures, a failure and
  recovery suite, CI on Python 3.9 and 3.12 with a wheel, Docker and
  per-engine job, and a daily canary that needs a `FACEBOOK_PROXY` secret
  and skips without one.
- Hardening found by live runs on 2026-10-06, shared by all three
  facebook-* repos: pyppeteer's browser cleanup is bounded (it could hang
  for 20+ minutes after a proxy closed the connection), a login page
  served under the asked-for address is a block (exit 3) recognised by
  its canonical link, and an empty Scraper API answer is retried as an
  API error.
- Hardening from a security review before release: an unparseable proxy
  line is reported without echoing it (it can carry a password); the
  pyppeteer engine answers only its OWN proxy's auth challenge, once per
  request, and cancels any other (a site's HTTP auth never sees the
  proxy's credentials); CSV cells that a spreadsheet would run as a
  formula are escaped (`csv_text_encoding: apostrophe-v1`, undone by
  `diff_runs.py`); output files and sidecars are replaced atomically.
