"""Compare production crawler engines and save a local, reviewable HTML report."""

import argparse
import asyncio
import html
import json
import os
from pathlib import Path
from time import perf_counter

from playwright.async_api import async_playwright

from llms_txt_gen.crawling.browser import route_public_request
from llms_txt_gen.crawling.fetcher import fetch_page
from llms_txt_gen.generation.generator import build_document, render_document


async def compare(args):
    output = args.output
    records = []
    for engine in ("beautifulsoup", "playwright"):
        started = perf_counter()
        result = await fetch_page(
            args.url,
            json.dumps(
                {
                    "crawler": engine,
                    "max_pages": args.max_pages,
                    "fetch_timeout_seconds": args.timeout,
                    "render_wait_seconds": args.render_wait,
                }
            ),
        )
        elapsed = perf_counter() - started
        (output / f"{engine}.json").write_text(result.model_dump_json(indent=2) + "\n")
        (output / f"{engine}-llms.txt").write_text(render_document(build_document(result)))
        records.append((engine, result, elapsed))
        print(f"{engine}: {len(result.results)} pages in {elapsed:.2f}s", flush=True)

    # Visualize the same URL with scripts disabled/enabled in fresh browser contexts.
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(os.environ["PLAYWRIGHT_CDP_URL"])
        try:
            for engine, enabled in (("beautifulsoup", False), ("playwright", True)):
                context = await browser.new_context(
                    java_script_enabled=enabled,
                    service_workers="block",
                    viewport={"width": 1280, "height": 900},
                )
                try:
                    await context.route("**/*", route_public_request)
                    page = await context.new_page()
                    await page.goto(args.url, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(args.render_wait * 1000)
                    await page.screenshot(path=str(output / f"{engine}.png"))
                finally:
                    await context.close()
        finally:
            await browser.close()

    cards = []
    for engine, result, elapsed in records:
        rows = "".join(
            f"<tr><td>{html.escape(page.url)}</td><td>{html.escape(page.title or '—')}</td></tr>"
            for page in result.results
        )
        page_label = "page" if len(result.results) == 1 else "pages"
        cards.append(f"""<section><h2>{engine}</h2>
            <p class="metric">{len(result.results)} {page_label} <small>· {elapsed:.2f}s</small></p>
            <p><a href="{engine}.json">Crawl JSON</a> ·
            <a href="{engine}-llms.txt">Generated llms.txt</a></p>
            <img src="{engine}.png"
                 alt="Page with JavaScript {"enabled" if engine == "playwright" else "disabled"}">
            <table><tr><th>URL</th><th>Extracted title</th></tr>{rows}</table></section>""")
    report = f"""<!doctype html><html lang="en"><meta charset="utf-8">
        <title>BeautifulSoup vs Playwright — crawler demo</title><style>
        *{{box-sizing:border-box}}body{{font:16px system-ui;margin:36px;
        background:#f4f5f7;color:#182333}}
        h1{{font-size:32px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}
        section{{background:white;padding:24px;border-radius:14px;min-width:0}}
        .metric{{font-size:30px;font-weight:650}}small{{font-size:16px;font-weight:400}}
        img{{width:100%;border:1px solid #ddd;border-radius:8px}}a{{color:#245cba}}
        table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:20px}}
        td,th{{text-align:left;border-bottom:1px solid #ddd;padding:10px;overflow-wrap:anywhere}}
        @media(max-width:800px){{.grid{{grid-template-columns:1fr}}}}
        </style><h1>BeautifulSoup vs Playwright</h1><p>{html.escape(args.url)}</p>
        <p>Live production crawler comparison. Limit: {args.max_pages} pages per engine;
        render wait: {args.render_wait:g}s. Browser screenshots show scripts disabled/enabled;
        page counts and titles come from each crawler's saved results.</p>
        <div class="grid">{"".join(cards)}</div></html>"""
    (output / "index.html").write_text(report)
    print(f"Report: {output / 'index.html'}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="https://audio-tour.pages.dev/")
    parser.add_argument("--output", default="demo-output")
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--render-wait", type=float, default=1)
    args = parser.parse_args()
    if not os.environ.get("PLAYWRIGHT_CDP_URL"):
        parser.error("Set PLAYWRIGHT_CDP_URL to the Chromium sidecar endpoint first")
    if args.max_pages <= 0 or args.timeout <= 0 or not 0 <= args.render_wait <= 60:
        parser.error("Page limit and timeout must be positive; render wait must be in [0, 60]")
    args.output = Path(args.output).resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    asyncio.run(compare(args))


if __name__ == "__main__":
    main()
