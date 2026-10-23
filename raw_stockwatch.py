import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

from lxml import html as lxml_html
import requests

# --------------------------------------------------------------------------- #
# Configuration & Constants
# --------------------------------------------------------------------------- #

BASE_URL = "https://www.stockwatch.pl/komunikaty-spolek/wszystkie.aspx"
SOURCE_ORIGIN = "https://www.stockwatch.pl"
DEFAULT_PROXY = "socks4://185.147.69.48:1080"
TARGET_RECORDS = 100
DEFAULT_MAX_PAGES = 6
REQUEST_TIMEOUT = 30
PDF_DOWNLOAD_TIMEOUT = 60
REQUEST_DELAY = 0.5
MAX_RETRIES = 2

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": f"{SOURCE_ORIGIN}/",
    "Upgrade-Insecure-Requests": "1",
}

PDF_HREF_PATTERNS = [re.compile(r"\.pdf($|\?)", re.I), re.compile(r"pliki\.aspx", re.I)]
PDF_TEXT_KEYWORDS = ("pdf", "załącznik", "zalacznik", "pobierz", "download", "skan", "dokument", "attachment")


# --------------------------------------------------------------------------- #
# HTML Parsing & Helpers
# --------------------------------------------------------------------------- #


def parse_listing_page(html: str) -> list[dict]:
    """Parse announcement records from listing page HTML table."""
    tree = lxml_html.fromstring(html)
    records = []
    for row in tree.xpath("//table//tr"):
        cells = row.xpath("./td")
        if len(cells) < 5:
            continue

        c_links, t_links = cells[0].xpath(".//a"), cells[4].xpath(".//a")
        if not (c_links and t_links):
            continue

        cat_links = cells[1].xpath(".//a")
        category = (cat_links[0] if cat_links else cells[1]).text_content().strip()
        href = (t_links[0].get("href") or "").strip()

        rec = {
            "company_name": c_links[0].text_content().strip(),
            "title": t_links[0].text_content().strip(),
            "category": category,
            "published_at": cells[3].text_content().strip(),
            "url": urljoin(f"{SOURCE_ORIGIN}/", href) if href else "",
        }
        if all(rec.values()):
            records.append(rec)
    return records


def find_pdf_links(html: str, base_url: str) -> list[dict]:
    """Find PDF attachment links on detail page."""
    tree = lxml_html.fromstring(html)
    anchors = tree.xpath("//article//a[@href]") or tree.xpath("//a[@href]")
    found, seen = [], set()

    for a in anchors:
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:")):
            continue

        text = a.text_content().strip()
        text_lower = text.lower()
        href_lower = href.lower()

        is_pdf = any(p.search(href) for p in PDF_HREF_PATTERNS) or (
            any(kw in text_lower for kw in PDF_TEXT_KEYWORDS)
            and ("pliki" in href_lower or re.search(r"\.(pdf|docx?|xlsx?)($|\?)", href, re.I))
        )
        if not is_pdf:
            continue

        full_url = urljoin(base_url, href)
        if full_url not in seen:
            seen.add(full_url)
            found.append({"url": full_url, "text": text})
    return found


def make_pdf_filename(record: dict, pdf_index: int, pdf_url: str = "") -> str:
    """Generate safe, descriptive filename: {ticker}_{date}_{url_id}_{N}.pdf"""
    ticker = re.sub(r"[^a-z0-9_-]", "", record.get("company_name", "").lower())[:20] or "unknown"
    date_part = record.get("published_at", "").split(" ")[0].replace("-", "")
    date = re.sub(r"[^0-9]", "", date_part) or "nodate"
    slug = urlparse(record.get("url", "")).path.rsplit(",", 1)[-1].rsplit("/", 1)[-1].replace(".aspx", "")
    url_id = re.sub(r"[^a-z0-9_-]", "", slug.lower())[:50] or "noid"
    return f"{ticker}_{date}_{url_id}_{pdf_index}.pdf"


def dedupe(records: list[dict]) -> list[dict]:
    """Deduplicate records by (company_name, title, published_at)."""
    seen, unique = set(), []
    for r in records:
        key = (r["company_name"], r["title"], r["published_at"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def save_outputs(records: list[dict], json_path: Path, csv_path: Path) -> None:
    """Save records to JSON and CSV."""
    json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = ["company_name", "title", "category", "published_at", "url", "pdf_url", "pdf_file"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for r in records:
            pdf = (r.get("attachments") or [{}])[0]
            writer.writerow({
                "company_name": r.get("company_name", ""),
                "title": r.get("title", ""),
                "category": r.get("category", ""),
                "published_at": r.get("published_at", ""),
                "url": r.get("url", ""),
                "pdf_url": pdf.get("url", ""),
                "pdf_file": pdf.get("filename", "") if pdf.get("downloaded") else "",
            })


# --------------------------------------------------------------------------- #
# Network & Download Operations
# --------------------------------------------------------------------------- #


def build_session(proxy: str | None = None) -> requests.Session:
    """Create a configured requests session."""
    session = requests.Session()
    session.headers.update(HTTP_HEADERS)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def fetch_html(session: requests.Session, url: str) -> str:
    """Fetch HTML page; raises RuntimeError on 403."""
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 403:
        raise RuntimeError(f"HTTP 403 geo-blocked on {url}")
    resp.raise_for_status()
    return resp.text


def download_pdf(session: requests.Session, url: str, path: Path) -> tuple[bool, str]:
    """Download PDF file with retry and validation. Returns (success, error_msg)."""
    last_err = ""
    for attempt in range(MAX_RETRIES + 1):
        try:
            with session.get(url, timeout=PDF_DOWNLOAD_TIMEOUT, stream=True) as resp:
                if resp.status_code == 403:
                    return False, "HTTP 403 (geo-blocked)"
                resp.raise_for_status()

                content_type = resp.headers.get("Content-Type", "").lower()
                chunks = resp.iter_content(chunk_size=8192)
                first_chunk = next(chunks, b"")

                is_pdf = "pdf" in content_type or first_chunk[:5].startswith(b"%PDF") or ".pdf" in urlparse(url).path.lower()
                if not is_pdf:
                    return False, f"not a PDF (Content-Type: {content_type})"

                with open(path, "wb") as f:
                    f.write(first_chunk)
                    for chunk in chunks:
                        f.write(chunk)
                return True, ""
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < MAX_RETRIES:
                time.sleep(1.5 * (attempt + 1))
    return False, last_err


def process_announcement(session: requests.Session, record: dict, attachments_dir: Path) -> dict:
    """Fetch detail page, immediately find and download all PDFs as they are discovered."""
    url = record["url"]
    ticker = record["company_name"]
    print(f"[{ticker}] {record['published_at']} — fetching detail page...")

    try:
        html = fetch_html(session, url)
    except Exception as e:
        print(f"[{ticker}] ✗ Detail fetch failed: {e}", file=sys.stderr)
        record["attachments"] = []
        return record

    pdf_links = find_pdf_links(html, url)
    print(f"[{ticker}] Found {len(pdf_links)} PDF link(s)")
    attachments = []

    for idx, pdf_info in enumerate(pdf_links):
        pdf_url = pdf_info["url"]
        filename = make_pdf_filename(record, idx, pdf_url)
        path = attachments_dir / filename

        if path.exists() and path.stat().st_size > 0:
            print(f"  [{idx+1}/{len(pdf_links)}] Cached: {filename}")
            ok, err = True, ""
        else:
            print(f"  [{idx+1}/{len(pdf_links)}] Downloading: {filename}")
            ok, err = download_pdf(session, pdf_url, path)
            if ok:
                size = path.stat().st_size if path.exists() else 0
                print(f"    ✓ Saved {size} bytes")
            else:
                print(f"    ✗ Download failed: {err}", file=sys.stderr)

        att = {"url": pdf_url, "filename": filename if ok else "", "downloaded": ok, "text": pdf_info["text"]}
        if not ok:
            att["error"] = err
        attachments.append(att)

    record["attachments"] = attachments
    return record


# --------------------------------------------------------------------------- #
# Main Scraping Pipeline & CLI
# --------------------------------------------------------------------------- #


def scrape(args: argparse.Namespace) -> int:
    output_dir = args.output
    attachments_dir = output_dir / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = output_dir / "announcements.json", output_dir / "announcements.csv"

    session = build_session(args.proxy)
    results = []
    seen_urls = set()
    total_downloaded = 0
    t0 = time.time()

    print(f"Starting scraper (max {args.max_pages} pages, target: {args.target}, proxy: {args.proxy or 'disabled'})...")

    try:
        for page in range(args.max_pages):
            url = f"{BASE_URL}?{urlencode({'page': page, 'type': '', 'c': '', 't': 'Wszystkie'})}"
            print(f"\n{'='*60}\n[Listing Page {page}] Fetching...")
            try:
                html = fetch_html(session, url)
            except Exception as e:
                print(f"[Listing Page {page}] ✗ Fetch failed: {e}", file=sys.stderr)
                break

            records = parse_listing_page(html)
            print(f"[Listing Page {page}] Found {len(records)} announcement rows\n{'='*60}")

            for r in records:
                if r["url"] in seen_urls:
                    continue
                seen_urls.add(r["url"])

                # Immediately process announcement and scrape PDF as soon as link is found
                processed = process_announcement(session, r, attachments_dir)
                results.append(processed)

                total_downloaded += sum(1 for a in processed.get("attachments", []) if a.get("downloaded"))
                print(f"Progress: {len(results)}/{args.target} announcements processed\n")

                if len(results) >= args.target:
                    print(f"Reached target of {args.target} records!")
                    break

                if args.delay > 0:
                    time.sleep(args.delay)

            if len(results) >= args.target:
                break

    except KeyboardInterrupt:
        print("\nInterrupted by user. Saving collected records so far...", file=sys.stderr)

    # Sort, dedupe, and save outputs
    results.sort(key=lambda r: r["published_at"], reverse=True)
    final = dedupe(results)[:args.target]
    save_outputs(final, json_path, csv_path)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s | Saved {len(final)} unique records ({total_downloaded} PDFs downloaded).")
    print(f"Outputs: {json_path} and {csv_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronous unified scraper + PDF downloader for stockwatch.pl.")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="Max listing pages (default: 6).")
    parser.add_argument("--target", type=int, default=TARGET_RECORDS, help="Target unique records to collect (default: 100).")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY, help="Delay between requests in seconds (default: 0.5).")
    parser.add_argument("--proxy", default=DEFAULT_PROXY, help="Proxy URL (pass '' to disable).")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent, help="Output directory.")
    args = parser.parse_args()

    return scrape(args)


if __name__ == "__main__":
    sys.exit(main())
