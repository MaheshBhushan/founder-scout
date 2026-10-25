"""Self-hosted search, scraping, and extraction that replace Exa and Firecrawl.

Search uses the open-source ``ddgs`` metasearch library (DuckDuckGo, Bing, Brave and
others, no API key). Set ``SEARXNG_URL`` to query a self-hosted SearXNG instance instead.
Scraping fetches pages with httpx and converts the HTML to Markdown-like text with
BeautifulSoup. ``mailto:`` links and Cloudflare-obfuscated addresses are decoded into
the text, so the literal-email check sees them. Pages that render with JavaScript
fall back to a headless Chromium when Playwright is installed.
Extraction asks ``claude -p`` for the structured profile, using the claude.ai login.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urljoin

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
HEADERS = {"user-agent": USER_AGENT, "accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8", "accept-language": "en,de;q=0.8"}
MAX_BYTES = 3_000_000
# Below this much visible text the page is probably rendered client-side.
MIN_TEXT = 400

CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "claude-haiku-5-5")
EXTRACT_TIMEOUT = 180
EXTRACT_CHARS = 24000

DROP_TAGS = ("script", "style", "noscript", "svg", "canvas", "iframe", "template", "form", "button", "select")
SOCIAL = re.compile(r"^https?://([a-z]+\.)?(linkedin\.com|twitter\.com|x\.com|github\.com)/", re.I)
BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "ul", "ol", "table", "tr", "blockquote", "figure", "address", "dl", "dd", "dt"}


# ---------- search ----------

async def search(http: httpx.AsyncClient, query: str, limit: int = 5) -> list[str]:
    searx = os.environ.get("SEARXNG_URL", "").rstrip("/")
    if searx:
        r = await http.get(f"{searx}/search", params={"q": query, "format": "json"}, headers=HEADERS)
        r.raise_for_status()
        return [x["url"] for x in r.json().get("results", [])[:limit] if x.get("url")]
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException

    def run() -> list[dict]:
        try:
            return DDGS().text(query, max_results=limit) or []
        except DDGSException as e:
            if "No results" in str(e):
                return []
            raise
    return [x["href"] for x in await asyncio.to_thread(run) if x.get("href")][:limit]


# ---------- fetching ----------

def decode_cfemail(hexstr: str) -> str:
    """Decode Cloudflare's email protection (``data-cfemail``): XOR with the first byte."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def html_to_markdown(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    extra: list[str] = []
    # JSON-LD often names founders and staff (schema.org Person) that the visible page hides.
    for s in soup.find_all("script", type="application/ld+json"):
        if s.string and ("Person" in s.string or "founder" in s.string.lower()):
            extra.append(s.string.strip()[:4000])
    for el in soup.select("[data-cfemail]"):
        el.replace_with(decode_cfemail(el["data-cfemail"]))
    for a in soup.select('a[href*="/cdn-cgi/l/email-protection#"]'):
        a["href"] = "mailto:" + decode_cfemail(a["href"].split("#", 1)[1])
    for t in soup(DROP_TAGS):
        t.decompose()
    out: list[str] = []

    def walk(node: Tag) -> None:
        for c in node.children:
            if isinstance(c, NavigableString):
                out.append(re.sub(r"\s+", " ", str(c)))
                continue
            if not isinstance(c, Tag):
                continue
            name = c.name
            if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                out.append(f"\n\n{'#' * int(name[1])} {c.get_text(' ', strip=True)}\n\n")
            elif name == "a":
                href = (c.get("href") or "").strip()
                text = c.get_text(" ", strip=True) or c.get("aria-label") or c.get("title") or ""
                if not text and SOCIAL.search(href):  # icon-only profile links
                    text = href
                if href.lower().startswith("mailto:"):
                    addr = unquote(href[7:].split("?")[0])
                    out.append(f" {text} ({addr}) " if text and text != addr else f" {addr} ")
                elif href and not href.startswith(("#", "javascript:")) and text:
                    out.append(f" [{text}]({urljoin(base_url, href)}) ")
                elif text:
                    out.append(f" {text} ")
            elif name == "br":
                out.append("\n")
            elif name == "li":
                out.append("\n- ")
                walk(c)
            elif name == "img":
                if c.get("alt"):
                    out.append(f" {c['alt']} ")
            else:
                block = name in BLOCK_TAGS
                if block:
                    out.append("\n")
                walk(c)
                if block:
                    out.append("\n")

    walk(soup.body or soup)
    text = "".join(out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if extra:
        text += "\n\n## Structured data\n\n" + "\n\n".join(extra)
    return text


async def fetch_html(http: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Return (final_url, html). Raises httpx.HTTPError on failure or non-HTML content."""
    async with http.stream("GET", url, headers=HEADERS, follow_redirects=True, timeout=30) as r:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if ctype and "html" not in ctype and "xml" not in ctype:
            raise httpx.HTTPError(f"not an HTML page ({ctype.split(';')[0]})")
        body = bytearray()
        async for chunk in r.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                break
        return str(r.url), body.decode(r.encoding or "utf-8", errors="replace")


_browser_lock = asyncio.Lock()


async def render_html(url: str) -> str | None:
    """Render a page in headless Chromium. Returns None when Playwright is not installed."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    async with _browser_lock:  # one browser at a time keeps memory bounded
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page(user_agent=USER_AGENT)
                await page.goto(url, wait_until="networkidle", timeout=45000)
                return await page.content()
            finally:
                await browser.close()


async def scrape_markdown(http: httpx.AsyncClient, url: str) -> str:
    try:
        final, html = await fetch_html(http, url)
    except httpx.HTTPStatusError as e:
        # Bot walls answer plain HTTP clients with 403; a real browser often gets through.
        rendered = await render_html(url) if e.response.status_code == 403 else None
        if not rendered:
            raise
        return html_to_markdown(rendered, url)
    text = html_to_markdown(html, final)
    if len(text) < MIN_TEXT:
        try:
            rendered = await render_html(final)
        except Exception:  # a browser failure leaves the static text in place
            rendered = None
        if rendered:
            text = max(text, html_to_markdown(rendered, final), key=len)
    return text


# ---------- extraction ----------

async def extract_json(markdown: str, url: str, prompt: str, schema: dict) -> dict:
    """Ask Claude for structured data from page text. Returns {} when extraction fails."""
    if not markdown.strip():
        return {}
    task = (
        f"{prompt}\n\nReturn only data stated on the page. Use empty strings for unknown fields.\n\n"
        f"=== PAGE {url} ===\n{markdown[:EXTRACT_CHARS]}"
    )
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
        "--tools", "", "--model", EXTRACT_MODEL, "--no-session-persistence",
        # Drop any API key so claude uses the claude.ai login instead.
        env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(task.encode()), EXTRACT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("claude extraction timed out")
    res = json.loads(out) if out.strip() else {}
    res = res[-1] if isinstance(res, list) and res else res
    if proc.returncode or res.get("is_error"):
        raise RuntimeError(f"claude exited {proc.returncode}: {res.get('result') or err.decode(errors='replace')[-300:]}")
    return res.get("structured_output") or {}
