import os
import re
import shutil
import sys
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.alfardanexchange.com.qa"
SITEMAP_INDEX = f"{BASE}/sitemap.xml"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AlfardanKBScraper/1.0)"}

# The server omits its intermediate cert (GlobalSign GCC R3 EV TLS CA 2025),
# so default CA bundles fail verification. Point REQUESTS_CA_BUNDLE at a
# bundle that includes the intermediate before running.
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Sub-sitemaps that only list taxonomy pages with no useful text
SKIP_SITEMAPS = ("blog-categories", "blog-tags")


def get_locs(xml_url: str) -> list[str]:
    resp = SESSION.get(xml_url, timeout=30)
    resp.raise_for_status()
    return re.findall(r"<loc>([^<]+)</loc>", resp.text)


def collect_urls() -> list[str]:
    urls = []
    for sitemap in get_locs(SITEMAP_INDEX):
        name = os.path.basename(urlparse(sitemap).path)
        if any(name.startswith(s) for s in SKIP_SITEMAPS):
            continue
        try:
            urls.extend(get_locs(sitemap))
        except Exception as e:
            print(f"WARN: could not read sitemap {sitemap}: {e}")
    seen = set()
    unique = []
    for u in urls:
        u = u.rstrip("/")
        if u and u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def url_to_filename(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return "index.txt"
    slug = path.replace("/", "_")
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", slug)
    return f"{slug[:120]}.txt"


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    text_lines = []
    for line in lines:
        if line:
            text_lines.append(line)
        elif text_lines and text_lines[-1] != "":
            text_lines.append("")
    return "\n".join(text_lines).strip()


def scrape(out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    urls = collect_urls()
    print(f"Found {len(urls)} URLs to scrape")

    ok, failed = 0, []
    for i, url in enumerate(urls, 1):
        try:
            resp = SESSION.get(url, timeout=30)
            resp.raise_for_status()
            text = extract_text(resp.text)
            if not text:
                print(f"[{i}/{len(urls)}] EMPTY {url}")
                failed.append(url)
                continue
            filename = url_to_filename(url)
            with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
                f.write(f"URL: {url}\n")
                f.write("=" * 80 + "\n\n")
                f.write(text + "\n")
            ok += 1
            print(f"[{i}/{len(urls)}] OK {url} -> {filename}")
        except Exception as e:
            print(f"[{i}/{len(urls)}] FAIL {url}: {e}")
            failed.append(url)
        time.sleep(0.5)

    print(f"\nDone: {ok} saved, {len(failed)} failed")
    for url in failed:
        print(f"  failed: {url}")
    return ok, failed


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "pages"
    scrape(out)
