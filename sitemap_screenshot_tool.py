#!/usr/bin/env python3
"""
Sitemap Recon Tool
==================
Recursively discovers every sitemap (sitemap index -> child sitemaps -> page URLs)
starting from a domain or a direct sitemap.xml URL, then optionally screenshots
every final page URL it finds.

Usage:
    python3 sitemap_screenshot_tool.py -u https://example.com
    python3 sitemap_screenshot_tool.py -u https://example.com/sitemap.xml --no-screenshots
    python3 sitemap_screenshot_tool.py -u https://example.com -o results --concurrency 8 --limit 200

Install deps first:
    pip install httpx playwright rich --break-system-packages
    python3 -m playwright install chromium
    (rich is optional — the tool falls back to plain text output without it,
     but installing it gives you the banner/colors/progress bars)

Output layout (default: ./sitemap_recon_<domain>/):
    sitemaps_found.txt     -> every sitemap URL discovered (index + child)
    urls.txt                -> every final page URL discovered
    urls.json                -> structured data: {sitemap_source, url}
    screenshots/             -> one PNG per page URL (if enabled)
    report.html               -> simple clickable report linking urls to screenshots
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import urllib.parse as urlparse
import xml.etree.ElementTree as ET

try:
    import httpx
except ImportError:
    sys.exit("Missing dependency: pip install httpx --break-system-packages")

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.progress import (
        Progress, SpinnerColumn, BarColumn, TextColumn,
        MofNCompleteColumn, TimeElapsedColumn,
    )
    from rich import box
    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False
    console = None

VERSION = "1.0"

BANNER = r"""
 ____  _ _       __  __
/ ___|(_) |_ ___|  \/  | __ _ _ __
\___ \| | __/ _ \ |\/| |/ _` | '_ \
 ___) | | ||  __/ |  | | (_| | |_) |
|____/|_|\__\___|_|  |_|\__,_| .__/
                              |_|
              R E C O N   T O O L
"""


def print_banner():
    if RICH_AVAILABLE:
        console.print(BANNER, style="bold cyan", highlight=False)
        console.print(
            Panel.fit(
                f"[bold]Sitemap Recon & Screenshot Tool[/bold]  [dim]v{VERSION}[/dim]\n"
                f"[dim]Recursive sitemap crawler -> page extractor -> screenshot capture[/dim]",
                border_style="cyan",
            )
        )
    else:
        print(BANNER)
        print(f"Sitemap Recon & Screenshot Tool v{VERSION}")
        print("(tip: pip install rich --break-system-packages for a nicer interface)\n")


def log(msg, style="white", prefix="*"):
    if RICH_AVAILABLE:
        console.print(f"[dim][{prefix}][/dim] {msg}", style=style)
    else:
        print(f"[{prefix}] {msg}")


def log_ok(msg):
    log(msg, style="green", prefix="+")


def log_warn(msg):
    log(msg, style="yellow", prefix="!")


def log_err(msg):
    log(msg, style="bold red", prefix="!")


def log_info(msg):
    log(msg, style="cyan", prefix="*")


def print_summary_table(out_dir, sitemaps, url_records, screenshots):
    if not RICH_AVAILABLE:
        print(f"\nSitemaps found : {len(sitemaps)}")
        print(f"Pages found    : {len(url_records)}")
        print(f"Screenshots    : {len(screenshots)}")
        print(f"Output dir     : {out_dir}")
        return
    table = Table(title="Run Summary", box=box.ROUNDED, show_header=False, border_style="cyan")
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Sitemaps found", str(len(sitemaps)))
    table.add_row("Pages found", str(len(url_records)))
    table.add_row("Screenshots taken", str(len(screenshots)))
    table.add_row("Output directory", out_dir)
    table.add_row("Report", os.path.join(out_dir, "report.html"))
    console.print(table)


SITEMAP_NS_RE = re.compile(r"\{.*\}")
COMMON_SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap/sitemap.xml",
    "/wp-sitemap.xml",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SitemapReconTool/1.0; +https://example.local)"
}


def strip_ns(tag: str) -> str:
    return SITEMAP_NS_RE.sub("", tag)


def safe_filename(url: str, max_len: int = 120) -> str:
    """Turn a URL into a filesystem-safe filename, deduped with a short hash."""
    parsed = urlparse.urlparse(url)
    base = (parsed.netloc + parsed.path).strip("/").replace("/", "_") or "root"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    return f"{base[:max_len]}_{h}.png"


async def fetch_text(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        if resp.status_code == 200 and resp.text.strip():
            return resp.text
    except Exception as e:
        log_warn(f"fetch failed: {url} ({e})")
    return None


def parse_sitemap_xml(xml_text: str):
    """Returns (child_sitemaps, page_urls). Handles both <sitemapindex> and <urlset>."""
    child_sitemaps, page_urls = [], []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", errors="ignore"))
    except ET.ParseError:
        # Fall back to a crude regex scrape if XML is malformed
        for m in re.finditer(r"<loc>\s*(.*?)\s*</loc>", xml_text, re.I | re.S):
            page_urls.append(m.group(1).strip())
        return child_sitemaps, page_urls

    root_tag = strip_ns(root.tag)
    for child in root:
        if strip_ns(child.tag) != "sitemap" and strip_ns(child.tag) != "url":
            continue
        loc = None
        for sub in child:
            if strip_ns(sub.tag) == "loc":
                loc = (sub.text or "").strip()
                break
        if not loc:
            continue
        if root_tag == "sitemapindex" or strip_ns(child.tag) == "sitemap":
            child_sitemaps.append(loc)
        else:
            page_urls.append(loc)
    return child_sitemaps, page_urls


async def discover_seed_sitemaps(client: httpx.AsyncClient, base_url: str):
    """If given a bare domain, check robots.txt and common sitemap paths."""
    parsed = urlparse.urlparse(base_url)
    if parsed.path and parsed.path.lower().endswith(".xml"):
        return [base_url]

    origin = f"{parsed.scheme}://{parsed.netloc}"
    seeds = set()

    robots_text = await fetch_text(client, origin + "/robots.txt")
    if robots_text:
        for line in robots_text.splitlines():
            if line.lower().startswith("sitemap:"):
                seeds.add(line.split(":", 1)[1].strip())

    for path in COMMON_SITEMAP_PATHS:
        candidate = origin + path
        text = await fetch_text(client, candidate)
        if text and ("<urlset" in text or "<sitemapindex" in text or "<loc>" in text):
            seeds.add(candidate)

    return list(seeds) if seeds else [origin + "/sitemap.xml"]


async def crawl_sitemaps(start_url: str, max_depth: int = 6, concurrency: int = 8):
    """BFS through the sitemap tree. Returns (all_sitemaps, url_records)."""
    all_sitemaps = set()
    url_records = []  # list of {"url": ..., "source_sitemap": ...}
    seen_urls = set()

    sem = asyncio.Semaphore(concurrency)

    progress_ctx = None
    task_id = None
    if RICH_AVAILABLE:
        progress_ctx = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[cyan]crawling sitemaps[/cyan]"),
            TextColumn("[dim]{task.fields[status]}[/dim]"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        progress_ctx.start()
        task_id = progress_ctx.add_task("crawl", status="starting...")

    def update_status():
        if progress_ctx:
            progress_ctx.update(
                task_id,
                status=f"{len(all_sitemaps)} sitemaps, {len(url_records)} pages",
            )

    async with httpx.AsyncClient(http2=True) as client:
        seeds = await discover_seed_sitemaps(client, start_url)
        queue = [(s, 0) for s in seeds]

        while queue:
            batch, queue = queue[:concurrency], queue[concurrency:]

            async def worker(sm_url, depth):
                if sm_url in all_sitemaps or depth > max_depth:
                    return
                all_sitemaps.add(sm_url)
                async with sem:
                    text = await fetch_text(client, sm_url)
                if not text:
                    return
                children, pages = parse_sitemap_xml(text)
                if progress_ctx:
                    progress_ctx.console.print(
                        f"[dim]  ->[/dim] {sm_url} [dim]({len(children)} child, {len(pages)} pages)[/dim]"
                    )
                else:
                    print(f"  [+] {sm_url} -> {len(children)} child sitemap(s), {len(pages)} page(s)")
                for c in children:
                    if c not in all_sitemaps:
                        queue.append((c, depth + 1))
                for p in pages:
                    if p not in seen_urls:
                        seen_urls.add(p)
                        url_records.append({"url": p, "source_sitemap": sm_url})
                update_status()

            await asyncio.gather(*(worker(u, d) for u, d in batch))

    if progress_ctx:
        progress_ctx.stop()

    return all_sitemaps, url_records


async def screenshot_urls(url_records, out_dir: str, concurrency: int = 4, limit: int | None = None,
                           save_progress_every: int = 25, progress_cb=None):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log_warn("Playwright (Python) not installed — skipping screenshots.")
        log_warn("Install with: pip install playwright --break-system-packages && python3 -m playwright install chromium")
        return {}

    shots_dir = os.path.join(out_dir, "screenshots")
    os.makedirs(shots_dir, exist_ok=True)
    records = url_records[:limit] if limit else url_records
    results = {}
    failed = 0
    sem = asyncio.Semaphore(concurrency)

    bar = None
    task_id = None
    if RICH_AVAILABLE:
        bar = Progress(
            SpinnerColumn(style="magenta"),
            TextColumn("[magenta]screenshotting[/magenta]"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            TextColumn("[dim]{task.fields[status]}[/dim]"),
            TimeElapsedColumn(),
            console=console,
        )
        bar.start()
        task_id = bar.add_task("shots", total=len(records), status="")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            context = await browser.new_context(viewport={"width": 1366, "height": 900})

            async def shot(rec):
                nonlocal failed
                url = rec["url"]
                fname = safe_filename(url)
                fpath = os.path.join(shots_dir, fname)
                async with sem:
                    page = await context.new_page()
                    try:
                        await page.goto(url, timeout=25000, wait_until="load")
                        await page.screenshot(path=fpath, full_page=True)
                        results[url] = fname
                    except Exception as e:
                        failed += 1
                        if bar:
                            bar.console.print(f"[red]  x[/red] {url} [dim]({e})[/dim]")
                        else:
                            print(f"  [!] screenshot failed: {url} ({e})")
                    finally:
                        await page.close()
                    if bar:
                        bar.update(task_id, advance=1, status=f"{failed} failed")
                    if progress_cb and len(results) % save_progress_every == 0:
                        progress_cb(results)

            await asyncio.gather(*(shot(r) for r in records))
            await browser.close()

    except Exception as e:
        # Playwright driver itself failed to launch (env/version mismatch, etc).
        # Don't let this wipe out whatever we already captured.
        if bar:
            bar.stop()
        log_err(f"Playwright failed to launch: {e}")
        log_warn("This is usually a Node/Playwright driver conflict (common on Kali where")
        log_warn("apt's system playwright and pip's python-playwright collide). Try:")
        log_warn("  pip install --upgrade --force-reinstall playwright --break-system-packages")
        log_warn("  python3 -m playwright install --force chromium")
        log_warn("If that still fails, run screenshots from a clean venv:")
        log_warn("  python3 -m venv ~/.sitemap-venv && source ~/.sitemap-venv/bin/activate")
        log_warn("  pip install httpx playwright && python3 -m playwright install chromium")
    finally:
        if bar:
            bar.stop()

    return results


def write_report(out_dir, sitemaps, url_records, screenshots):
    with open(os.path.join(out_dir, "sitemaps_found.txt"), "w") as f:
        f.write("\n".join(sorted(sitemaps)))

    with open(os.path.join(out_dir, "urls.txt"), "w") as f:
        f.write("\n".join(r["url"] for r in url_records))

    with open(os.path.join(out_dir, "urls.json"), "w") as f:
        json.dump(url_records, f, indent=2)

    rows = []
    for r in url_records:
        shot = screenshots.get(r["url"])
        img_tag = f'<a href="screenshots/{shot}" target="_blank"><img src="screenshots/{shot}" width="220"></a>' if shot else "no screenshot"
        rows.append(
            f"<tr><td><a href='{r['url']}' target='_blank'>{r['url']}</a></td>"
            f"<td>{r['source_sitemap']}</td><td>{img_tag}</td></tr>"
        )
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Sitemap Recon Report</title>
<style>
body{{font-family:sans-serif;background:#111;color:#eee;padding:20px}}
table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #333;padding:8px;font-size:13px;vertical-align:top}}
th{{background:#222}}
a{{color:#6cf}}
</style></head><body>
<h2>Sitemap Recon Report</h2>
<p>{len(sitemaps)} sitemap(s) crawled, {len(url_records)} page URL(s) found, {len(screenshots)} screenshot(s) captured.</p>
<table><tr><th>Page URL</th><th>Source sitemap</th><th>Screenshot</th></tr>
{''.join(rows)}
</table></body></html>"""
    with open(os.path.join(out_dir, "report.html"), "w") as f:
        f.write(html)


async def main_async(args):
    parsed = urlparse.urlparse(args.url if "://" in args.url else "https://" + args.url)
    domain = parsed.netloc or args.url
    out_dir = args.output or f"sitemap_recon_{domain.replace(':', '_')}"
    os.makedirs(out_dir, exist_ok=True)

    log_info(f"Target: [bold]{args.url}[/bold]" if RICH_AVAILABLE else f"Target: {args.url}")
    log_info(f"Output: {out_dir}")
    if RICH_AVAILABLE:
        console.rule(style="dim")

    sitemaps, url_records = await crawl_sitemaps(
        args.url if "://" in args.url else "https://" + args.url,
        max_depth=args.max_depth,
        concurrency=args.concurrency,
    )
    log_ok(f"Total sitemaps found: {len(sitemaps)}")
    log_ok(f"Total page URLs found: {len(url_records)}")

    # Save crawl results to disk IMMEDIATELY — before screenshots run — so a
    # Playwright crash never costs you the (often much more valuable / slower
    # to reproduce) sitemap crawl data.
    write_report(out_dir, sitemaps, url_records, {})
    log_ok(f"Sitemap + URL results saved to {out_dir}/ (sitemaps_found.txt, urls.txt, urls.json)")

    screenshots = {}
    if not args.no_screenshots and url_records:
        effective_limit = args.limit
        if effective_limit is None and len(url_records) > 200 and not args.all_screenshots:
            effective_limit = 50
            log_warn(f"{len(url_records)} page URLs found — too many to screenshot in one run.")
            log_warn(f"Defaulting to the first {effective_limit}. Use --limit N to change this,")
            log_warn(f"or --all-screenshots to (attempt to) screenshot every single one.")

        if RICH_AVAILABLE:
            console.rule(style="dim")

        def _save_progress(partial_shots):
            write_report(out_dir, sitemaps, url_records, partial_shots)

        screenshots = await screenshot_urls(
            url_records, out_dir, concurrency=args.shot_concurrency,
            limit=effective_limit, progress_cb=_save_progress,
        )

    write_report(out_dir, sitemaps, url_records, screenshots)
    if RICH_AVAILABLE:
        console.rule(style="dim")
    print_summary_table(out_dir, sitemaps, url_records, screenshots)


def main():
    ap = argparse.ArgumentParser(description="Recursively crawl sitemaps and screenshot final pages.")
    ap.add_argument("-u", "--url", required=True, help="Domain or direct sitemap.xml URL, e.g. example.com or https://example.com/sitemap.xml")
    ap.add_argument("-o", "--output", help="Output directory (default: sitemap_recon_<domain>)")
    ap.add_argument("--max-depth", type=int, default=6, help="Max nested sitemap depth (default 6)")
    ap.add_argument("--concurrency", type=int, default=8, help="Concurrent sitemap fetches (default 8)")
    ap.add_argument("--shot-concurrency", type=int, default=4, help="Concurrent browser tabs for screenshots (default 4)")
    ap.add_argument("--limit", type=int, default=None, help="Max number of pages to screenshot (default: auto-capped at 50 if >200 URLs found)")
    ap.add_argument("--all-screenshots", action="store_true", help="Override the auto-cap and attempt to screenshot every URL found (can be extremely slow on large sites)")
    ap.add_argument("--no-screenshots", action="store_true", help="Skip screenshotting, only extract sitemap URLs")
    args = ap.parse_args()
    print_banner()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
