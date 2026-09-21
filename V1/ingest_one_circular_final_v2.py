"""
ingest_.py — Extract RBI circular text and save to database + .md files

Single-file ingestion pipeline:
  - Crawls RBI index for circular metadata
  - Downloads PDFs (skips if present)
  - Extracts text + tables (HTML-first, PDF fallback)
  - Merges text correctly (tables inline, linked references)
  - Saves to ingest_.db (SQLite) and writes .md per circular to output/

Usage:
    python ingest_.py            # process up to 50 circulars (default)
    python ingest_.py --limit 5  # process only first 5
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

import fitz  # PyMuPDF (raw text + render pages for Gemini fallback)
import pdfplumber
import requests
from bs4 import BeautifulSoup

# Fix Windows console encoding for Unicode characters
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PDF_DIR = os.path.join(BASE_DIR, "pdfs_ingest_1_09_final")
os.makedirs(PDF_DIR, exist_ok=True)
OUTPUT_DIR = os.path.join(BASE_DIR, "output_1_09_final")
os.makedirs(OUTPUT_DIR, exist_ok=True)

DB_PATH = os.path.join(BASE_DIR, "ingest_1_09_final.db")
INDEX_URL = "https://www.rbi.org.in/scripts/BS_CircularIndexDisplay.aspx"
DETAIL_BASE = "https://www.rbi.org.in/scripts/BS_CircularIndexDisplay.aspx?Id="
MAX_CIRCULARS = 26
START_YEAR = 2026
END_YEAR = 2020
REQUEST_DELAY = 0.9
REQUEST_TIMEOUT = 40
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*",
}

DATE_PATTERN = (r"(January|February|March|April|May|June|July|August|September/"
                r"October|November|December)\s+\d{1,2},\s*\d{4}")
REF_PATTERN = r"RBI/\d{4}-\d{2}/\d+"
CLOSINGS = ["yours faithfully", "yours sincerely", "yours truly"]

# ----------------------------------------------------------------------------
# Signatory detection
# ----------------------------------------------------------------------------
SIGNATORY_REGEX = re.compile(
    r"(Governor|"
    r"Deputy\s+Governor|"
    r"Executive\s+Director(?:\s*\(ED\))?|"
    r"Principal\s+Chief\s+General\s+Manager(?:-in-Charge)?(?:\s*\(PCGM(?:-i-C)?\))?|"
    r"Chief\s+General\s+Manager(?:-in-Charge)?(?:\s*\(CGM(?:-i-C)?\))?|"
    r"General\s+Manager(?:-in-Charge)?(?:\s*\(GM(?:-i-C)?\))?|"
    r"Deputy\s+General\s+Manager(?:\s*\(DGM\))?|"
    r"Assistant\s+General\s+Manager(?:\s*\(AGM\))?|"
    r"Chief\s+Manager|"
    r"Deputy\s+Chief\s+Manager|"
    r"Regional\s+Director|"
    r"Principal\s+Regional\s+Director|"
    r"Secretary|"
    r"Officer[- ]in[- ]Charge)",
    re.IGNORECASE
)

def _strip_frontmatter(text):
    """Remove a leading YAML '--- ... ---' block we prepend (so it isn't
       mistaken for body content when scanning for the signatory)."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def _looks_like_name(s):
    """Heuristic: does this line look like a person's name?"""
    s = s.strip().strip("()")
    if not s or len(s) > 60:
        return False
    if any(ch.isdigit() for ch in s):
        return False
    words = s.split()
    if len(words) < 2:
        # allow initials form like "S. K. Jain" or "J.K."
        if words and all(re.match(r"[A-Z]\.?", w.rstrip(".")) for w in words):
            return True
        return False
    if s.isupper():
        return False
    return sum(1 for w in words if w[:1].isupper()) >= 2


def _find_signatory_tail(lines):
    """Fallback: scan the TAIL of the document for a signature block.

    Looks only at the last few non-empty lines so we never grab a name that
    merely appears earlier in the body. Returns (name, designation).
    """
    nonempty = [ln for ln in lines if ln.strip()][-12:]
    if not nonempty:
        return "", ""

    # Pattern A: a parenthesized name, e.g. "(J. P. Sharma)"
    paren = re.compile(r"\(([^)]+)\)")
    for i, ln in enumerate(nonempty):
        m = paren.search(ln)
        if m and _looks_like_name(m.group(1)):
            name = m.group(1).strip()
            rest = ln[ln.rfind(")") + 1:].strip()
            desig = rest if rest else (nonempty[i + 1].strip() if i + 1 < len(nonempty) else "")
            return name, desig

    # Pattern B: a name line immediately above a designation line
    for i in range(len(nonempty) - 1, 0, -1):
        cur, nxt = nonempty[i - 1], nonempty[i]
        if _looks_like_name(cur) and any(w in nxt.lower() for w in
                                           ("governor", "general manager", "manager", "director", "secretary",
                                            "chief", "deputy", "executive", "officer", "chairman", "principal",
                                            "commissioner", "rbi", "reserve bank")):
            return cur.strip(), nxt.strip()

    return "", ""


def signatory_from_text(text):
    """Scan from the end for the signatory using bottom-to-top approach.
    
    Returns (name, designation) where:
    - name: the person's name (extracted from parentheses or line above designation)
    - designation: the person's designation (matched using SIGNATORY_REGEX)
    """
    text = _strip_frontmatter(text)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    
    # Scan from bottom up (last 20 lines) for signatory pattern
    nonempty = [ln for ln in lines if ln.strip()][-20:]
    
    # First, look for parenthesized name in the tail
    paren = re.compile(r"\(([^)]+)\)")
    for i in range(len(nonempty) - 1, -1, -1):
        ln = nonempty[i]
        m = paren.search(ln)
        if m and _looks_like_name(m.group(1)):
            name = m.group(1).strip()
            # Check if there's a designation on the same line after the closing paren
            rest = ln[ln.rfind(")") + 1:].strip()
            if rest:
                # Check if rest matches designation pattern
                if SIGNATORY_REGEX.search(rest):
                    return name, rest
            # Check next line for designation
            if i + 1 < len(nonempty):
                next_line = nonempty[i + 1].strip()
                if SIGNATORY_REGEX.search(next_line):
                    return name, next_line
                # Also check if next line looks like a name (for multi-line signatures)
                if _looks_like_name(next_line) and i + 2 < len(nonempty):
                    desig_line = nonempty[i + 2].strip()
                    if SIGNATORY_REGEX.search(desig_line):
                        return name, desig_line
    
    # Second, look for name followed by designation (without parentheses)
    for i in range(len(nonempty) - 1, 0, -1):
        cur = nonempty[i - 1]
        nxt = nonempty[i]
        # Current line should look like a name
        if _looks_like_name(cur):
            # Next line should match the designation pattern
            if SIGNATORY_REGEX.search(nxt):
                return cur.strip(), nxt.strip()
    
    # Third, look for designation followed by name (reverse order)
    for i in range(len(nonempty) - 1, 0, -1):
        cur = nonempty[i - 1]
        nxt = nonempty[i]
        # Current line should match designation pattern
        if SIGNATORY_REGEX.search(cur):
            # Next line should look like a name
            if _looks_like_name(nxt):
                return nxt.strip(), cur.strip()
    
    # If nothing found in the tail, try the closing-line approach as fallback
    for i, ln in enumerate(lines):
        if any(c in ln.lower() for c in CLOSINGS):
            after = [x for x in lines[i + 1:] if x.strip()]
            if after:
                name = re.sub(r"[()]", "", after[0]).strip()
                if _looks_like_name(name) and len(after) > 1:
                    # Look for designation in the lines after the name
                    for j in range(1, len(after)):
                        if SIGNATORY_REGEX.search(after[j]):
                            return name, after[j].strip()
            break
    
    return "", ""


def _in_bbox(ch, bboxes):
    for (x0, y0, x1, y1) in bboxes:
        if (x0 - 1 <= ch["x0"] and ch["x1"] <= x1 + 1 and
                y0 - 1 <= ch["top"] and ch["bottom"] <= y1 + 1):
            return True
    return False


def _line_text(chars):
    chars = sorted(chars, key=lambda c: c["x0"])
    s, prev = "", None
    for c in chars:
        if prev and (c["x0"] - prev["x1"]) > 1.2:
            s += " "
        s += c["text"]
        prev = c
    return s.strip()


def _chars_to_prose(chars, bboxes):
    chars = [c for c in chars if not _in_bbox(c, bboxes)]
    if not chars:
        return ""
    chars = sorted(chars, key=lambda c: (round(c["top"]), c["x0"]))
    lines, cur, last = [], [], None
    for c in chars:
        if last is None or abs(c["top"] - last) <= 3:
            cur.append(c)
        else:
            lines.append(_line_text(cur))
            cur = [c]
        last = c["top"]
    if cur:
        lines.append(_line_text(cur))
    return "\n".join(lines)


def _table_to_md(cells):
    rows = [r for r in cells if any((c or "").strip() for c in r)]
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)

    def clean(x):
        return (x or "").replace("|", "/").replace("\n", " ").strip()

    out = []
    header = [clean(rows[0][i]) if i < len(rows[0]) else "" for i in range(ncol)]
    out.append("| " + " | ".join(header) + " |")
    out.append("|" + " --- |" * ncol)
    for r in rows[1:]:
        row = [clean(r[i]) if i < len(r) else "" for i in range(ncol)]
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def _extract_tables(page):
    """Try pdfplumber line strategy, then text strategy. Return list of table dicts."""
    tables = []
    try:
        found = page.find_tables()
    except Exception:
        found = []
    if not found:
        try:
            found = page.find_tables(table_settings={
                "vertical_strategy": "text", "horizontal_strategy": "text"})
        except Exception:
            found = []
    for t in found:
        cells = t.extract()
        if not cells:
            continue
        tables.append({"bbox": t.bbox, "cells": cells})
    return tables


def gemini_table_markdown(pdf_path, page_index):
    """Fallback: render one page to PNG and ask Gemini for a markdown table."""
    if not GEMINI_API_KEY:
        return None
    try:
        from google import genai
        from google.genai import types as genai_types
        doc = fitz.open(pdf_path)
        png = doc[page_index].get_pixmap(dpi=150).tobytes("png")
        doc.close()
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = ("Extract ONLY the table(s) in this image into a single markdown table. "
                  "Where a row has sub-rows nested under it, repeat the parent row's label in "
                  "each sub-row so every row is fully self-contained. Return only the markdown "
                  "table, no commentary.")
        part = genai_types.Part.from_bytes(data=png, mime_type="image/png")
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=[prompt, part])
        return resp.text.strip() if resp.text else None
    except Exception as e:
        print("  ! gemini table fallback failed:", e)
        return None


def extract_from_pdf(pdf_path):
    """Step 2 (+Step 3 stitching): returns markdown with inline tables from PDF."""
    pages_md = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            tables = _extract_tables(page)
            bboxes = [t["bbox"] for t in tables]
            prose = _chars_to_prose(page.chars, bboxes)
            page_md = prose
            for t in tables:
                md = _table_to_md(t["cells"])
                # try Gemini cleanup only if the rule-based table looks poor
                if md.count("|") < 4 or any("  " in r for r in md.splitlines()):
                    fb = gemini_table_markdown(pdf_path, i - 1)
                    if fb:
                        md = fb
                page_md += "\n\n" + md + "\n"
            pages_md.append(page_md)
    return "\n\n".join(pages_md)


def extract_body_from_html(html):
    """Pull prose + inline <table> markdown from the RBI display page.

    Only the circular's own content region is used — website chrome (footer nav,
    sidebars) is excluded so the signatory/table scan stays on real document text.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    # Prefer the circular container if detectable; else the main page table.
    container = (soup.find("div", id="leftnavigation") or
                 soup.find("div", class_="content") or
                 soup.find("td", class_="tablecontent") or
                 soup)
    # Fall back to the tablebg (index-style) or the first large table.
    if container is soup:
        container = soup.find("table", class_="tablebg") or soup

    parts = []
    for el in container.find_all(["p", "table", "li", "h1", "h2", "h3"]):
        if el.name == "table":
            md = html_table_to_md(el)
            if md:
                parts.append(md)
        else:
            txt = el.get_text(" ", strip=True)
            if txt and not _is_nav_text(txt):
                parts.append(txt)
    return "\n\n".join(parts)


def _is_nav_text(txt):
    low = txt.lower()
    return any(w in low for w in
               ("rbi kehta hai", "tenders", "right to information", "follow rbi", "rss",
                "twitter", "youtube", "instagram", "facebook", "linkedin", "rbi clarifications",
                "rbi's vision", "what's new", "press releases", "notifications", "speeches",
                "rbi website", "sitemap", "contact us", "careers", "opportunities"))


def html_table_to_md(table):
    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    out = ["| " + " | ".join(c if i < len(r) else "" for i, c in enumerate(r)) + " |" for r in rows]
    out.insert(1, "|" + " --- |" * ncol)
    return "\n".join(out)


def stitch_multipage_tables(markdown_text):
    """
    Merge a table that was split across a page break. If the continuation repeats the
    header row, drop the duplicate header.
    """
    pattern = re.compile(
        r"(\|[\^\n]+\|\n\|[\s:\-|\n]+\|\n(?:\|[^\n]+\|\n)+)"
        r"\s*(?:<!--\s*page_break\s*-->)?\s*"
        r"(\|[\s:\-|\n]+\|\n)?((?:\|[^\n]+\|\n)+)"
    )

    def merge(match):
        t1 = match.group(1).strip().split("\n")
        t2 = match.group(3).strip().split("\n")
        if t1 and t2 and t1[0].strip() == t2[0].strip():
            t2 = t2[1:]
        return "\n".join(t1 + t2)

    return pattern.sub(merge, markdown_text)



def keep_last_complete_circular(text, ref_no):
    """
    Keep only the LAST complete occurrence of this circular in the Markdown
    body.

    The existing extraction is left completely unchanged.

    Why this is used:
    The RBI page can expose the same circular several times:
      1. wrapper/combined table
      2. inner table
      3. actual structured circular

    The last occurrence is the desired structured version.

    We only use this function when preparing the .md file. The database still
    receives the original extracted body_markdown unchanged.
    """
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    ref_no = (ref_no or "").strip()

    if not ref_no:
        return text

    # Find every occurrence of THIS circular's reference number.
    # re.escape prevents characters such as /, -, or . from being treated
    # as regular-expression operators.
    matches = list(
        re.finditer(
            re.escape(ref_no),
            text,
            flags=re.IGNORECASE
        )
    )

    # Nothing is duplicated.
    if len(matches) <= 1:
        return text

    # Prefer the LAST occurrence that contains the closing/signatory section.
    # This identifies the last complete circular rather than a later
    # reference-number mention that may occur inside a partial block.
    for match in reversed(matches):
        candidate = text[match.start():].strip()

        if re.search(
            r"(Yours faithfully|Yours sincerely|Yours truly|"
            r"Chief General Manager|General Manager|Governor)",
            candidate,
            flags=re.IGNORECASE
        ):
            return candidate

    # Safe fallback: if no closing/signatory text can be found, use the
    # last occurrence exactly as requested.
    return text[matches[-1].start():].strip()

def build_header_block(meta):
    """
    Build a valid Markdown YAML front matter block and title.

    Metadata is stored once in YAML. The circular body follows below it.
    """
    return (
        "---\n"
        f'circular_number: "{meta["circular_number"]}"\n'
        f'date_issued: "{meta["date_issued"]}"\n'
        f'department: "{meta["department"]}"\n'
        f'subject: "{meta["subject"]}"\n'
        f'status: "{meta["status"]}"\n'
        f'meant_for: "{meta["meant_for"]}"\n'
        "---\n\n"
        f"# {meta['subject'] or meta['circular_number']}\n\n"
    )


def inject_reference_links(markdown_text, meta):
    """Turn reference numbers into Markdown links only when a URL exists."""
    for ref in meta["references"]:
        ref_no = (ref.get("ref_no") or "").strip()
        url = (ref.get("url") or "").strip()

        if not ref_no or not url:
            continue

        link = f"[{ref_no}]({url})"
        markdown_text = re.sub(re.escape(ref_no), link, markdown_text)

    return markdown_text


def _db_path():
    return DB_PATH


def init_db():
    conn = sqlite3.connect(_db_path())
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS circulars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref_no TEXT UNIQUE NOT NULL,
            rbi_page_id TEXT,
            rbi_page_url TEXT NOT NULL,
            title TEXT,
            date_issued TEXT,
            department TEXT,
            subject TEXT,
            meant_for TEXT,
            status TEXT DEFAULT 'active',
            signatory_name TEXT,
            signatory_designation TEXT,
            body_markdown TEXT,
            footnote_text TEXT,
            source_method TEXT,
            pdf_url TEXT,
            content_hash TEXT NOT NULL,
            extraction_status TEXT DEFAULT 'pending',
            scraped_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS circular_references (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_ref_no TEXT NOT NULL,
            referenced_ref_no TEXT NOT NULL,
            reference_type TEXT NOT NULL,
            url TEXT
        );
        CREATE TABLE IF NOT EXISTS validation_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            circular_ref_no TEXT NOT NULL,
            tier INTEGER NOT NULL,
            check_name TEXT NOT NULL,
            result TEXT NOT NULL,
            details TEXT,
            checked_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT DEFAULT (datetime('now')),
            new_count INTEGER,
            skipped_count INTEGER,
            status TEXT
        );
    """)
    conn.commit()
    return conn


# ----------------------------------------------------------------------------
# Crawling and processing
# ----------------------------------------------------------------------------
def crawl_index(limit=MAX_CIRCULARS):
    """Scrape the RBI index page to collect circular metadata and detail URLs."""
    print(f"== Crawling index (limit {limit}) ==")
    try:
        resp = requests.get(INDEX_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ! Failed to fetch index page: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="tablebg")
    if not table:
        print("  ! Could not find the expected table on the index page.")
        return []

    rows = []
    for tr in table.find_all("tr")[1:]:
        if len(rows) >= limit:
            break
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue

        link_tag = tds[0].find("a", class_="link2")
        if not link_tag:
            continue
        href = link_tag.get("href", "")
        if not href or "Id=" not in href:
            continue
        detail_url = href if href.startswith("http") else f"https://www.rbi.org.in/scripts/{href}"
        link_text = link_tag.get_text(" ", strip=True)
        ref_match = re.search(REF_PATTERN, link_text)
        ref_no = ref_match.group(0) if ref_match else link_text.split()[0] if link_text.split() else ""

        date_issued = tds[1].get_text(strip=True)
        department = tds[2].get_text(strip=True)
        subject = tds[3].get_text(strip=True)
        meant_for = tds[4].get_text(strip=True)
        status = "active"

        rows.append({
            "ref_no": ref_no,
            "date_issued": date_issued,
            "department": department,
            "subject": subject,
            "meant_for": meant_for,
            "detail_url": detail_url,
            "status": status,
        })

    print(f"   found {len(rows)} circulars")
    return rows


def scrape_web_metadata(detail_url, rid):
    """Scrape metadata from the detail page for a given circular."""
    try:
        resp = requests.get(detail_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ! Failed to fetch detail page for {rid}: {e}")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)

    ref_match = re.search(REF_PATTERN, text)
    ref_no = ref_match.group(0) if ref_match else ""

    date_match = re.search(DATE_PATTERN, text)
    date_issued = date_match.group(0) if date_match else ""

    department = subject = meant_for = ""
    status = "active"

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    for i, line in enumerate(lines):
        low = line.lower()
        if "department" in low and i + 1 < len(lines):
            department = lines[i + 1].strip()
        if "subject" in low and i + 1 < len(lines):
            subject = lines[i + 1].strip()
        if "meant for" in low and i + 1 < len(lines):
            meant_for = lines[i + 1].strip()
        if "status" in low and i + 1 < len(lines):
            status_val = lines[i + 1].strip().lower()
            if "withdrawn" in status_val:
                status = "withdrawn"
            elif "superseded" in status_val:
                status = "superseded"

    return {
        "ref_no": ref_no,
        "date_issued": date_issued,
        "department": department,
        "subject": subject,
        "meant_for": meant_for,
        "status": status,
    }


def process_one(row, conn, stats):
    """Process a single circular: fetch detail page, extract body, save to DB and .md file."""
    rid = row.get("ref_no", "unknown")
    detail_url = row.get("detail_url")
    if not detail_url:
        print(f"  ! {rid}: missing detail URL")
        stats["skipped"] += 1
        return

    print(f"  Processing {rid}...")

    # Step 1: get metadata from detail page (with fallback to index row)
    meta = scrape_web_metadata(detail_url, rid)
    for key in ["ref_no", "date_issued", "department", "subject", "meant_for", "status"]:
        if key in meta and meta[key]:
            row[key] = meta[key]

    if not row["ref_no"]:
        print(f"  ! {rid}: could not determine reference number")
        stats["skipped"] += 1
        return

    # Step 2: try to get body from detail page HTML
    html_body = ""
    try:
        resp = requests.get(detail_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        html_body = extract_body_from_html(resp.text)
    except Exception as e:
        print(f"  ! {rid}: failed to fetch/parse detail page HTML: {e}")

    use_html = bool(html_body and len(html_body.strip()) >= 200)
    body_markdown = ""
    source_method = ""
    pdf_url = None

    if use_html:
        body_markdown = html_body
        source_method = "html"
    else:
        # Step 3: fall back to PDF
        try:
            resp = requests.get(detail_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            pdf_tag = soup.find("a", href=lambda x: x and ".pdf" in x.lower())
            if pdf_tag:
                pdf_url = pdf_tag["href"]
                if not pdf_url.startswith("http"):
                    pdf_url = f"https://www.rbi.org.in/scripts/{pdf_url}"
            else:
                print(f"  ! {rid}: no PDF link found on detail page")
                pdf_url = None
        except Exception as e:
            print(f"  ! {rid}: failed to fetch detail page for PDF link: {e}")
            pdf_url = None

        if pdf_url:
            pdf_filename = os.path.join(PDF_DIR, os.path.basename(pdf_url))
            if not os.path.exists(pdf_filename):
                try:
                    print(f"    Downloading PDF: {os.path.basename(pdf_url)}")
                    pdf_resp = requests.get(pdf_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                    pdf_resp.raise_for_status()
                    if pdf_resp.content[:4] == b"%PDF":
                        with open(pdf_filename, "wb") as f:
                            f.write(pdf_resp.content)
                    else:
                        print(f"  ! {rid}: downloaded file is not a PDF")
                        pdf_url = None
                except Exception as e:
                    print(f"  ! {rid}: failed to download PDF: {e}")
                    pdf_url = None
            else:
                print(f"    PDF already exists: {os.path.basename(pdf_url)}")

            if pdf_url:
                try:
                    pdf_text = extract_from_pdf(pdf_filename)
                    if pdf_text and len(pdf_text.strip()) >= 50:
                        body_markdown = pdf_text
                        source_method = "pdf_pdfplumber"
                    else:
                        print(f"  ! {rid}: PDF text extraction yielded insufficient content")
                        pdf_url = None
                except Exception as e:
                    print(f"  ! {rid}: failed to extract text from PDF: {e}")
                    pdf_url = None

    if not body_markdown:
        print(f"  ! {rid}: failed to extract body from HTML or PDF")
        stats["skipped"] += 1
        return

    # Step 4: extract signatory from the body text
    signatory_name, signatory_designation = signatory_from_text(body_markdown)

    # Step 5: extract reference IDs from the body to create links
    refs = []
    for match in re.finditer(REF_PATTERN, body_markdown):
        ref_no = match.group(0)
        refs.append({"ref_no": ref_no, "url": ""})
    seen = set()
    unique_refs = []
    for r in refs:
        if r["ref_no"] not in seen:
            seen.add(r["ref_no"])
            unique_refs.append(r)

    # Step 6: build the final markdown with header and body
    meta_for_header = {
        "circular_number": row["ref_no"],
        "date_issued": row["date_issued"],
        "department": row["department"],
        "subject": row["subject"],
        "meant_for": row["meant_for"],
        "status": row["status"],
    }
    header_block = build_header_block(meta_for_header)

    # IMPORTANT:
    # Do not change the extraction pipeline or the database content.
    # For the .md file only, keep the LAST complete occurrence of the
    # circular. In the RBI output this is the final structured version.
    md_body = keep_last_complete_circular(
        body_markdown,
        row["ref_no"]
    )

    final_markdown = header_block + md_body

    # Only create links when an actual URL is available.
    if unique_refs:
        final_markdown = inject_reference_links(
            final_markdown,
            {"references": unique_refs}
        )

    # Step 7: compute hash for deduplication
    content_for_hash = header_block + body_markdown
    content_hash = hashlib.sha256(content_for_hash.encode("utf-8")).hexdigest()

    # Step 8: save to database
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO circulars
            (ref_no, rbi_page_id, rbi_page_url, title, date_issued, department, subject,
             meant_for, status, signatory_name, signatory_designation, body_markdown,
             footnote_text, source_method, pdf_url, content_hash, extraction_status, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                row["ref_no"],
                None,
                detail_url,
                row["subject"] or row["ref_no"],
                row["date_issued"],
                row["department"],
                row["subject"],
                row["meant_for"],
                row["status"],
                signatory_name,
                signatory_designation,
                body_markdown,
                "",
                source_method,
                pdf_url if pdf_url else "",
                content_hash,
                "pending",
            ),
        )
        conn.commit()
        stats["new"] += 1
    except Exception as e:
        print(f"  ! {rid}: failed to save to database: {e}")
        stats["skipped"] += 1
        return

    # Step 8b: Store reference links in database
    try:
        for ref in unique_refs:
            cursor.execute(
                "INSERT OR IGNORE INTO circular_references (source_ref_no, referenced_ref_no, reference_type, url) VALUES (?, ?, ?, ?)",
                (row["ref_no"], ref["ref_no"], "related", ref.get("url", ""))
            )
    except Exception as e:
        print(f"  ! {rid}: failed to save references: {e}")

    # Step 8c: Run Tier 0 validation checks and update extraction_status
    try:
        record_for_validation = {
            "ref_no": row["ref_no"],
            "date_issued": row["date_issued"],
            "subject": row["subject"],
            "signatory_name": signatory_name,
            "signatory_designation": signatory_designation,
            "body_markdown": body_markdown,
            "source_method": source_method,
        }
        new_status = run_tier0_checks(record_for_validation, conn, row["ref_no"])
        
        # Update extraction_status in circulars table
        cursor.execute(
            "UPDATE circulars SET extraction_status = ? WHERE ref_no = ?",
            (new_status, row["ref_no"])
        )
        conn.commit()
        
        if new_status == "needs_review":
            print(f"  [WARN] {rid}: needs review (validation issues found)")
    except Exception as e:
        print(f"  ! {rid}: validation failed: {e}")

    # Step 9: write the markdown file
    safe_ref_no = re.sub(r'[^\w\-_.]', '_', row["ref_no"])
    md_path = os.path.join(OUTPUT_DIR, f"{safe_ref_no}.md")
    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(final_markdown)
    except Exception as e:
        print(f"  ! {rid}: failed to write markdown file: {e}")

    print(f"  [OK] {rid}: saved (source: {source_method})")
    time.sleep(REQUEST_DELAY)




# ----------------------------------------------------------------------------
# Tier 0 Validation Functions
# ----------------------------------------------------------------------------
def required_field_check(record):
    """Check that all required fields are present and non-empty."""
    required = ["ref_no", "date_issued", "subject", "signatory_name"]
    missing = [field for field in required if not record.get(field)]
    if missing:
        return ("required_field_check", "fail", f"Missing: {', '.join(missing)}")
    return ("required_field_check", "pass", "All required fields present")


def ref_no_format_check(record):
    """Check that ref_no matches RBI's known pattern."""
    ref_no = record.get("ref_no", "")
    pattern = r"^RBI/\d{4}-\d{2}/\d+$"
    if re.match(pattern, ref_no):
        return ("ref_no_format_check", "pass", "Format valid")
    return ("ref_no_format_check", "fail", f"Malformed ref_no: {ref_no}")


def signatory_designation_check(record):
    """Check that signatory designation matches known RBI titles."""
    desig = record.get("signatory_designation", "")
    if not desig:
        return ("signatory_designation_check", "warning", "No designation extracted")
    
    if SIGNATORY_REGEX.search(desig):
        return ("signatory_designation_check", "pass", "Designation matches known pattern")
    return ("signatory_designation_check", "fail", f"Unknown designation: {desig}")


def table_column_consistency_check(record):
    """Check that all rows in each Markdown table have consistent column counts."""
    body = record.get("body_markdown", "")
    if not body:
        return ("table_column_consistency_check", "pass", "No tables present")
    
    # Extract all markdown tables from body
    table_pattern = re.compile(r'(\|.+\|(?:\n\|[-: |]+\|)?(?:\n\|.+\|)+)')
    tables = table_pattern.findall(body)
    
    if not tables:
        return ("table_column_consistency_check", "pass", "No tables present")
    
    issues = []
    for table in tables:
        rows = [line for line in table.split('\n') if line.strip().startswith('|')]
        if len(rows) < 2:
            continue
        
        # Get column count from header (first row)
        header_count = len([c for c in rows[0].split('|') if c.strip()])
        if header_count == 0:
            continue
            
        for i, row in enumerate(rows[2:], start=2):  # Skip header and separator
            col_count = len([c for c in row.split('|') if c.strip()])
            if col_count != header_count:
                issues.append(f"Row {i} has {col_count} cols (expected {header_count})")
            if len(issues) >= 3:
                break
    
    if issues:
        return ("table_column_consistency_check", "fail", "; ".join(issues))
    return ("table_column_consistency_check", "pass", "All tables consistent")


def run_tier0_checks(record, conn, ref_no):
    """Run all Tier 0 validation checks and store results."""
    checks = [
        required_field_check,
        ref_no_format_check,
        signatory_designation_check,
        table_column_consistency_check,
    ]
    
    extraction_status = "verified"
    
    for check_func in checks:
        try:
            check_name, result, details = check_func(record)
            
            # Store in validation_results
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO validation_results (circular_ref_no, tier, check_name, result, details) VALUES (?, ?, ?, ?, ?)",
                (ref_no, 0, check_name, result, details)
            )
            
            # Update extraction_status if any check fails
            if result == "fail":
                extraction_status = "needs_review"
            elif result == "warning" and extraction_status == "verified":
                extraction_status = "needs_review"
                
        except Exception as e:
            print(f"  ! Validation check {check_func.__name__} failed: {e}")
    
    return extraction_status

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=MAX_CIRCULARS)
    args = ap.parse_args()

    print(f"== Crawling index (limit {args.limit}) ==")
    rows = crawl_index(args.limit)
    print(f"   found {len(rows)} circulars")

    conn = init_db()
    run = conn.execute("INSERT INTO ingestion_runs (status) VALUES ('running')").lastrowid
    stats = {"new": 0, "skipped": 0}
    try:
        for row in rows:
            try:
                process_one(row, conn, stats)
            except Exception as e:
                print("  ! error:", e)
            time.sleep(REQUEST_DELAY)
    finally:
        conn.execute("UPDATE ingestion_runs SET new_count=?, skipped_count=?, status='done' WHERE id=?",
                     (stats["new"], stats["skipped"], run))
        conn.commit()
        conn.close()

    print("\n=================== DONE ===================")
    print(stats)
    print("DB :", DB_PATH)
    print("MD :", OUTPUT_DIR)


if __name__ == "__main__":
    main()











