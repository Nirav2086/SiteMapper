# Technical Report — Sitemap Recon & Screenshot Tool

**File:** `sitemap_screenshot_tool.py`
**Version:** 1.0
**Language:** Python 3.10+ (uses `X | None` type hints)
**Core libraries:** `httpx` (async HTTP), `xml.etree.ElementTree` (XML
parsing), `playwright` (headless browser screenshots), `rich` (terminal UI,
optional)

---

## 1. Purpose

Given a website, the tool answers two questions completely automatically:

1. *What does this site's sitemap tree actually contain?* — Sitemaps are
   often nested: a root `sitemap.xml` is a `sitemapindex` pointing at dozens
   or hundreds of child sitemaps (per content type, per date range, per
   locale, etc.), and each child `urlset` lists the real page URLs. Doing
   this by hand for a large site is impractical.
2. *What do those pages actually look like?* — A full-page screenshot of
   every discovered URL, for fast visual triage (useful in bug bounty recon
   to quickly scan hundreds of endpoints for admin panels, staging pages,
   error pages, forms, etc. without opening each one manually).

## 2. High-level architecture

```
 main()
   └─ main_async()
        ├─ crawl_sitemaps()            [Phase 1: discovery + BFS crawl]
        │    ├─ discover_seed_sitemaps()
        │    │    └─ fetch_text()      (robots.txt + common paths)
        │    └─ loop: fetch_text() → parse_sitemap_xml() → queue children
        │
        ├─ write_report()              [checkpoint: save before screenshots]
        │
        ├─ screenshot_urls()           [Phase 2: optional, Playwright]
        │    └─ Chromium → goto() → screenshot() per URL, bounded by
        │       a semaphore for concurrency control
        │
        └─ write_report()              [final: full report with screenshots]
             └─ print_summary_table()
```

Everything after the banner runs inside a single `asyncio.run(main_async())`
call — the crawl and the screenshot phase are both async, but they run
sequentially (crawl fully finishes before any screenshot starts), which is
what makes the "save immediately after crawl" safety guarantee possible.

## 3. Function-by-function breakdown

### `strip_ns(tag)`
Sitemap XML is namespaced (`{http://www.sitemaps.org/schemas/sitemap/0.9}loc`).
`ElementTree` returns tags with the namespace prefix attached, which makes
string comparisons like `tag == "loc"` fail. This strips the `{...}` prefix
with a single regex substitution so the rest of the code can compare plain
tag names (`loc`, `sitemap`, `url`, `sitemapindex`, `urlset`) regardless of
which namespace URI the sitemap declares.

### `safe_filename(url, max_len=120)`
Screenshots need a filesystem-safe, unique filename derived from a URL that
may contain query strings, slashes, unicode, or be extremely long. The
approach:
1. Take `netloc + path` from the parsed URL (drops query string / fragment
   from the *visible* part of the name, since those can be huge or contain
   characters that break filenystems).
2. Replace `/` with `_`, then strip any remaining character that isn't
   alphanumeric, `.`, `_`, or `-`.
3. Truncate to `max_len` characters.
4. Append an 8-character MD5 hash of the **full original URL** (including
   query string).

The hash suffix is the important part: two different URLs that only differ
by query string (`?id=1` vs `?id=2`) would otherwise collapse to the same
truncated filename and overwrite each other's screenshot. The hash
guarantees uniqueness even after truncation.

### `fetch_text(client, url)`
A thin async wrapper around `httpx.AsyncClient.get()`:
- 20-second timeout, follows redirects.
- Returns the response body only on `HTTP 200` with non-empty text;
  otherwise returns `None`.
- Any exception (DNS failure, connection reset, TLS error, etc.) is caught
  and logged as a warning rather than raised — a single broken sitemap URL
  should never crash the whole crawl.

### `parse_sitemap_xml(xml_text)`
Takes raw XML text, returns `(child_sitemaps, page_urls)`.

- Parses with `ElementTree.fromstring()`.
- Looks at the root tag (after stripping namespace): `sitemapindex` means
  every child's `<loc>` is another sitemap; `urlset` means every child's
  `<loc>` is a real page.
- **Fallback for malformed XML:** if `ElementTree` raises `ParseError` (some
  servers return sitemaps with encoding issues, stray characters, or
  slightly invalid XML), the function falls back to a regex scrape —
  `<loc>\s*(.*?)\s*</loc>` — and treats every match as a page URL. This is
  deliberately permissive: a slightly broken sitemap is still worth
  extracting URLs from, even if we can no longer tell child-sitemaps from
  pages in that fallback path.

### `discover_seed_sitemaps(client, base_url)`
Turns a bare domain into a starting list of sitemap URLs:
1. If the input already ends in `.xml`, it's used as-is — no discovery
   needed.
2. Otherwise, fetch `/robots.txt` and extract every `Sitemap:` directive
   (the standard way sites advertise sitemap locations).
3. In parallel, probe a fixed list of common sitemap paths
   (`/sitemap.xml`, `/sitemap_index.xml`, `/sitemap-index.xml`,
   `/sitemap/sitemap.xml`, `/wp-sitemap.xml`) and keep any that return XML
   that looks like a sitemap (`<urlset`, `<sitemapindex`, or `<loc>`
   present).
4. If nothing is found by either method, falls back to guessing
   `/sitemap.xml` anyway (many sites have one even without a `robots.txt`
   reference).

### `crawl_sitemaps(start_url, max_depth=6, concurrency=8)`
The core BFS (breadth-first search) engine. State:
- `all_sitemaps: set` — every sitemap URL visited (dedup + final report).
- `url_records: list[dict]` — every page URL found, each tagged with
  `source_sitemap` (which sitemap it came from — useful for provenance).
- `seen_urls: set` — fast membership check to avoid duplicate page records.
- `queue: list[(url, depth)]` — sitemaps still to fetch, paired with their
  depth so `max_depth` can be enforced.

Algorithm:
1. Seed the queue via `discover_seed_sitemaps`.
2. Pop up to `concurrency` items at a time, fetch them **concurrently**
   using `asyncio.gather`, each fetch guarded by an `asyncio.Semaphore` (so
   even if the batch is large, no more than `concurrency` HTTP requests are
   in flight at once).
3. For each fetched sitemap: parse it, print/log a one-line summary, push
   any child sitemaps onto the queue (depth + 1), and record any page URLs.
4. Repeat until the queue is empty or every remaining item exceeds
   `max_depth`.

Depth exists to protect against pathological or intentionally malicious
sitemap structures (a sitemap that points to itself, or an extremely deep
chain) — without it, a misbehaving target could make the crawl loop
indefinitely. The `all_sitemaps` set additionally guarantees a sitemap is
never fetched twice even if multiple parents reference it.

**Live UI integration:** when `rich` is available, this function drives a
`Progress` spinner that updates its status text (`"N sitemaps, M pages"`)
after every worker completes, and prints each sitemap's result as a dim
sub-line so you can watch the tree being discovered in real time.

### `screenshot_urls(url_records, out_dir, concurrency=4, limit=None, save_progress_every=25, progress_cb=None)`
Handles Phase 2. Key design points:

- **Optional dependency, handled gracefully.** If `playwright` isn't
  installed, the function logs instructions and returns an empty dict
  rather than crashing — the sitemap/URL extraction (the more valuable,
  harder-to-reproduce part) still completed and was already saved.
- **`limit`** truncates the URL list *before* any browser work starts —
  important, because launching a browser and opening pages is by far the
  slowest part of the tool.
- **Concurrency via semaphore**, same pattern as the crawl phase, but
  applied to browser tabs (`context.new_page()`) instead of HTTP requests —
  each `shot()` coroutine acquires the semaphore, opens a tab, navigates
  (25s timeout, waits for the `load` event), screenshots full-page, closes
  the tab, and releases the semaphore.
- **Per-URL failure isolation** — a `try/except` inside `shot()` means one
  page timing out or erroring (dead link, SSL issue, infinite redirect)
  only logs a warning and moves on; it never aborts the batch.
- **Progress checkpointing** — every `save_progress_every` (default 25)
  successful screenshots, `progress_cb` is invoked with the results so far.
  In `main_async`, this callback is wired to `write_report()`, so the HTML
  report on disk is updated incrementally *during* a long screenshot run —
  if the process is killed or the browser crashes at screenshot #340 of
  1000, the first 325 (rounded down to the last checkpoint) are already on
  disk and linked in the report.
- **Outer `try/except` around the entire Playwright block.** This is the
  fix for a real failure mode encountered in testing: on some Linux setups
  (notably Kali, where a system-wide `apt` Playwright/Node install coexists
  with the pip-installed one), the Playwright driver process itself can fail
  to start (`Connection closed while reading from the driver`). Without
  this try/except, that exception would propagate all the way up through
  `main_async` and crash the script *before* `write_report()` ran again —
  which is exactly the bug this version fixes. Now, a driver-launch failure
  is caught, a diagnostic message with concrete fix commands is printed,
  and the function returns whatever screenshots (if any) were already
  captured.

### `write_report(out_dir, sitemaps, url_records, screenshots)`
Pure I/O, no network calls — writes four files:
- `sitemaps_found.txt` — sorted, newline-separated sitemap URLs.
- `urls.txt` — newline-separated page URLs (in discovery order).
- `urls.json` — the full `url_records` list (URL + its source sitemap) as
  JSON, for programmatic consumption (e.g. feeding into another recon tool).
- `report.html` — a self-contained dark-themed HTML table: one row per page
  URL, with a link to the source sitemap and a thumbnail linking to the
  full screenshot (or "no screenshot" text if that URL wasn't captured).

This function is intentionally **idempotent and cheap** — it fully
overwrites all four files every time it's called, which is what makes the
checkpoint pattern (call it after crawl, then again periodically during
screenshotting, then once more at the end) safe and simple: there's no
partial-write or append logic to get wrong.

### Terminal UI helpers (`print_banner`, `log`, `log_ok`, `log_warn`,
`log_err`, `log_info`, `print_summary_table`)
All UI output is routed through these instead of raw `print()`, with a
single `RICH_AVAILABLE` flag deciding whether to use `rich`'s `Console` (for
color, the banner panel, and a bordered summary `Table`) or plain-text
fallbacks. This keeps the tool fully functional in minimal environments
(e.g. CI, or a machine where `pip install rich` isn't possible) while giving
a polished experience everywhere else.

### `main_async(args)`
Orchestrates the whole run in order:
1. Resolve the output directory name from the target domain (or use
   `--output` if given).
2. Run `crawl_sitemaps()`.
3. **Immediately** call `write_report()` with an empty screenshots dict —
   this is the crash-safety fix: sitemap/URL data hits disk before the
   browser is ever launched.
4. If screenshots weren't disabled: compute an *effective* limit (explicit
   `--limit`, or auto-capped at 50 if more than 200 URLs were found and
   `--all-screenshots` wasn't passed), then run `screenshot_urls()` with a
   progress-save callback.
5. Call `write_report()` one final time with the complete screenshot map,
   then print the summary table.

### `main()`
Standard `argparse` CLI wiring, then `print_banner()` followed by
`asyncio.run(main_async(args))`.

## 4. Design decisions & trade-offs

| Decision | Reasoning |
|---|---|
| BFS with a shared `queue` list instead of recursion | Recursive fetching would need one coroutine per sitemap with no natural concurrency cap; the queue + semaphore approach lets you fetch exactly `--concurrency` sitemaps at a time regardless of tree shape. |
| Save results before screenshotting, not after | Screenshotting is the more failure-prone phase (external browser process, network to many different hosts, long-running). The crawl data is valuable and comparatively cheap to obtain — it should never be at risk from a later phase's failure. |
| Auto-cap screenshots at 50 for large sites | Real-world sitemaps can easily contain 6-7 figures of URLs (this was observed directly: 1,146,983 URLs from one target). Screenshotting all of them by default would make the tool appear "stuck" for hours/days. Requiring an explicit `--limit` or `--all-screenshots` makes the cost visible and opt-in. |
| MD5-hash suffix on screenshot filenames | Prevents silent screenshot overwrites when multiple URLs truncate to the same base filename (very common with query-string-heavy URLs). |
| Regex fallback in `parse_sitemap_xml` | Some real-world sitemaps are not strictly valid XML. Failing closed (0 URLs extracted) on a parse error would silently under-report; a permissive regex fallback trades some precision (can't distinguish sitemap-index entries from page entries) for not losing data. |
| `rich` as an optional, not required, dependency | The tool should degrade gracefully rather than hard-fail in restricted environments where an extra terminal-UI library can't be installed. |

## 5. Known limitations

- **No JavaScript-rendered sitemap support** — if a site generates its
  sitemap.xml dynamically behind logic `httpx` can't execute, discovery may
  miss it (rare in practice; sitemaps are almost always static XML).
- **No robots.txt disallow enforcement** — the tool does not check
  `Disallow` rules before visiting/screenshotting a URL. It should only be
  pointed at targets you're authorized to scan.
- **Memory usage scales with URL count** — `url_records` is held fully in
  memory as a Python list of dicts; for sites with millions of URLs this can
  use a non-trivial amount of RAM (though far less than screenshotting would
  cost in time).
- **No retry logic** — a single failed fetch (sitemap or screenshot) is
  logged and skipped, not retried. Transient network blips can therefore
  under-count results on a single run.
- **Screenshot phase is the bottleneck** — even at reasonable concurrency,
  screenshotting thousands of real pages will take a long time; this is a
  fundamental cost of rendering full pages in a real browser, not something
  the tool's architecture can avoid.

## 6. Possible future improvements

- Retry-with-backoff for transient fetch failures.
- `--include`/`--exclude` regex filters on discovered URLs before
  screenshotting.
- Optional response-header capture per URL (status code, server header,
  security headers) alongside the screenshot, for faster bug-bounty triage.
- Resume support: read an existing `urls.json` and only screenshot URLs not
  already present in `screenshots/`.
- Streaming `urls.txt`/`urls.json` writes during the crawl itself (not just
  at checkpoints) for extremely large sites.
