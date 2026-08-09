# Sitemap Recon & Screenshot Tool

Recursively crawl a website's sitemap tree — including nested `sitemapindex`
files — pull out every page URL, and (optionally) capture a full-page
screenshot of each one. Built for recon work during bug bounty hunting and
general site auditing.

```
 ____  _ _       __  __
/ ___|(_) |_ ___|  \/  | __ _ _ __
\___ \| | __/ _ \ |\/| |/ _` | '_ \
 ___) | | ||  __/ |  | | (_| | |_) |
|____/|_|\__\___|_|  |_|\__,_| .__/
                              |_|
              R E C O N   T O O L
```

## Features

- **Auto-discovery** — point it at a bare domain and it checks `robots.txt`
  for `Sitemap:` entries plus common paths (`/sitemap.xml`,
  `/sitemap_index.xml`, `/wp-sitemap.xml`, etc). Or give it a direct sitemap
  URL if you already have one.
- **Recursive crawl** — walks `sitemapindex` → child sitemaps → `urlset` page
  URLs, however deep the nesting goes, with de-duplication and a configurable
  depth cap.
- **Concurrent, resilient fetching** — async HTTP with per-request timeouts;
  a malformed or unreachable sitemap doesn't stop the crawl.
- **Screenshot capture** — headless Chromium (via Playwright) grabs a
  full-page PNG of every discovered page URL.
- **Crash-safe output** — sitemap/URL results are written to disk immediately
  after the crawl, before screenshotting even starts, and the report is
  re-saved periodically during screenshotting. A browser crash never costs
  you the crawl data.
- **Pretty terminal UI** — banner, colored status lines, and live progress
  bars via [`rich`](https://github.com/Textualize/rich) (falls back to plain
  text if `rich` isn't installed).
- **Clickable HTML report** — every page URL linked next to its screenshot.

## Installation

```bash
git clone https://github.com/<your-username>/sitemap-recon-tool.git
cd sitemap-recon-tool
pip install -r requirements.txt --break-system-packages
python3 -m playwright install chromium
```

> **Note (Kali / Debian users):** if screenshots fail with `Connection closed
> while reading from the driver`, it's usually the system `apt` Playwright
> package colliding with the pip one. See [Troubleshooting](#troubleshooting)
> below.

## Usage

```bash
# Full run: discover sitemaps, extract URLs, screenshot the first 50 pages
python3 sitemap_screenshot_tool.py -u example.com

# Give it a direct sitemap URL instead of a bare domain
python3 sitemap_screenshot_tool.py -u https://example.com/sitemap.xml

# Just extract URLs, skip screenshots entirely (fast recon pass)
python3 sitemap_screenshot_tool.py -u example.com --no-screenshots

# Custom output dir, higher concurrency, screenshot up to 200 pages
python3 sitemap_screenshot_tool.py -u example.com -o results --concurrency 10 --limit 200

# Attempt to screenshot every single URL found (use with caution on large sites)
python3 sitemap_screenshot_tool.py -u example.com --all-screenshots
```

### CLI options

| Flag | Default | Description |
|---|---|---|
| `-u, --url` | *(required)* | Domain or direct sitemap URL |
| `-o, --output` | `sitemap_recon_<domain>` | Output directory |
| `--max-depth` | `6` | Max nested sitemap depth |
| `--concurrency` | `8` | Concurrent sitemap fetches |
| `--shot-concurrency` | `4` | Concurrent browser tabs for screenshots |
| `--limit` | auto (50 if >200 URLs) | Max pages to screenshot |
| `--all-screenshots` | off | Override the auto-cap, screenshot everything found |
| `--no-screenshots` | off | Skip screenshotting entirely |

## Output

```
sitemap_recon_example.com/
├── sitemaps_found.txt     # every sitemap URL discovered (index + child)
├── urls.txt                # every final page URL discovered
├── urls.json                # structured: [{url, source_sitemap}, ...]
├── screenshots/              # one PNG per screenshotted page
└── report.html                 # clickable report: URL <-> screenshot
```

## How it works (short version)

1. **Seed discovery** — check `robots.txt` and common sitemap paths to find
   the starting sitemap(s).
2. **BFS crawl** — fetch each sitemap, parse it as XML (`<sitemapindex>` or
   `<urlset>`), queue any child sitemaps, and collect any page `<loc>` URLs.
   Repeat until no new sitemaps are found or `--max-depth` is hit.
3. **Save immediately** — write `sitemaps_found.txt`, `urls.txt`, `urls.json`
   before touching the browser, so the crawl is never lost to a later
   failure.
4. **Screenshot** — launch headless Chromium, visit each page URL (bounded
   by `--limit`), and save a full-page PNG.
5. **Report** — build `report.html` linking every URL to its screenshot (or
   "no screenshot" if it wasn't captured).

See [`TECHNICAL_REPORT.md`](./TECHNICAL_REPORT.md) for the full breakdown of
every function, the data flow, and the design decisions behind it.

## Troubleshooting

**`Connection closed while reading from the driver`** (common on Kali) — the
system-wide `apt` Playwright/Node install conflicts with the pip-installed
one. Fix:

```bash
pip install --upgrade --force-reinstall playwright --break-system-packages
python3 -m playwright install --force chromium
```

If it still fails, isolate it in a clean venv:

```bash
python3 -m venv ~/.sitemap-venv
source ~/.sitemap-venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium
```

## Disclaimer

This tool only follows publicly listed sitemap links over normal HTTP(S) —
it does not bypass authentication, rate limits, or access controls. When
using it for bug bounty or security research, stay within the target
program's scope and rules of engagement.

## License

MIT — see [LICENSE](./LICENSE).
