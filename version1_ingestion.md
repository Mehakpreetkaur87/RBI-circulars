# RBI Circulars Ingestion & Scraping Engine — Version 1 Guide

> **Target Script**: [`V1/ingest_one_circular_final_v2.py`](file:///d:/RBI_circulars_project/V1/ingest_one_circular_final_v2.py)  
> **Pipeline Phase**: Step 1 — Web Crawling, Content Extraction, Table Reconstruction, Validation, and Storage

---

## 📌 Executive Summary

The **Version 1 Ingestion Engine** (`ingest_one_circular_final_v2.py`) is an automated regulatory scraping and ingestion pipeline designed specifically for the **Reserve Bank of India (RBI)** portal (`rbi.org.in`).

Regulatory circulars issued by the RBI feature complex layouts:
1. Nested HTML tables and frames.
2. Inconsistent document styling across decades.
3. Multi-page tables spanning page breaks.
4. Redundant circular repetitions within HTML wrappers.
5. Critical metadata (circular reference numbers, issue dates, issuing departments, signatories) scattered across headers and footers.

The V1 engine addresses these challenges using a **multi-tiered, fault-tolerant, HTML-first extraction strategy with PDF and multimodal Vision fallbacks**, followed by automated **Tier-0 data validation** before persisting records into a SQLite database and clean Markdown documents.

---

## 🏗️ Architecture & Ingestion Flow

The following diagram illustrates the complete end-to-end flow of the V1 scraping pipeline:

```mermaid
flowchart TD
    A["RBI Index Page<br/>(BS_CircularIndexDisplay.aspx)"] -->|1. Crawl Index Table| B["Index Metadata Collector<br/>(Ref No, Date, Dept, Subject, URL)"]
    
    B -->|2. HTTP Request with Delay| C["Fetch Circular Detail Page<br/>(Detail HTML)"]
    
    C -->|3. Scrape Detailed Metadata| D["Metadata Normalizer<br/>(Status, Department, Meant For)"]
    
    C -->|4. Primary Extraction Route| E{"Is HTML Body Valid?<br/>(len >= 200 chars)"}
    
    E -->|Yes (HTML First)| F["extract_body_from_html()<br/>- Decompose scripts/styles<br/>- Strip Portal Navigation<br/>- Parse HTML Tables to MD"]
    
    E -->|No (PDF Fallback)| G["Locate PDF Link on Detail Page"]
    G -->|Download & Cache| H["pdfs_ingest_1_09_final/*.pdf"]
    H --> I["extract_from_pdf()<br/>(pdfplumber + char bbox filtering)"]
    I --> J{"Rule-based Table Poor?<br/>(pipes < 4 or spaces)"}
    J -->|Yes| K["Gemini 2.5 Flash Vision Fallback<br/>(Render Page to PNG -> LLM Table MD)"]
    J -->|No| L["Markdown Inline Tables"]
    K --> L
    L --> M["Stitch Multi-page Tables"]
    
    F --> N["Content De-nesting & Deduplication<br/>keep_last_complete_circular()"]
    M --> N
    
    N --> O["Signatory Extraction Engine<br/>signatory_from_text()<br/>(Bottom-up regex scanning)"]
    
    N --> P["Regulatory Reference Linker<br/>(Detect RBI/YYYY-YY/NNN pattern)"]
    
    O & P & D --> Q["Build Final Markdown with YAML Frontmatter"]
    
    Q --> R["Compute SHA-256 Content Hash"]
    
    R --> S["Tier-0 Quality Assurance Engine<br/>run_tier0_checks()"]
    S -->|Required Fields Check| S1["ref_no, date, subject, signatory"]
    S -->|Regex Format Check| S2["^RBI/\\d{4}-\\d{2}/\\d+$"]
    S -->|Designation Check| S3["Known RBI Official Ranks"]
    S -->|Table Consistency Check| S4["Matching Column Counts"]
    
    S -->|Status: verified / needs_review| T["SQLite Database<br/>ingest_1_09_final.db"]
    Q --> U["Markdown File Output<br/>output_1_09_final/{ref_no}.md"]
```

---

## 🔍 Detailed Scraping Mechanism Breakdown

### 1. Index Discovery & Seed Crawling (`crawl_index`)
* **Target Endpoint**: `https://www.rbi.org.in/scripts/BS_CircularIndexDisplay.aspx`
* **Mechanism**:
  - Sends an HTTP `GET` request using custom browser headers (`User-Agent: Mozilla/5.0 ... Chrome/120.0.0.0`).
  - Parses the HTML DOM using `BeautifulSoup(html, "html.parser")`.
  - Targets the master index table: `<table class="tablebg">`.
  - Iterates over table rows (`<tr>`), extracting:
    - **Circular Link & ID**: Anchor tag `<a class="link2">` containing `href` with query parameter `Id=...`. Resolves relative URLs to absolute links (`https://www.rbi.org.in/scripts/...`).
    - **Notification Reference Number**: Extracted using regex `RBI/\d{4}-\d{2}/\d+` from the link text.
    - **Date Issued**: 2nd column (`<td>[1]`).
    - **Department**: 3rd column (`<td>[2]`).
    - **Subject**: 4th column (`<td>[3]`).
    - **Target Audience (`meant_for`)**: 5th column (`<td>[4]`).
  - Respects the configurable `--limit` CLI argument (defaults to 26).

---

### 2. Deep Metadata Extraction & Page Classification (`scrape_web_metadata`)
* For every circular discovered in the index, the script fetches its individual detail URL (`BS_CircularIndexDisplay.aspx?Id=<id>`).
* Extracts and normalizes:
  - **Reference Number Validation**: Re-scans detail text for `RBI/\d{4}-\d{2}/\d+`.
  - **Issue Date Normalization**: Matches full dates via `DATE_PATTERN` (`(January|...|December)\s+\d{1,2},\s*\d{4}`).
  - **Regulatory Status**: Scans lines following the `"status"` label. If marked `"withdrawn"` or `"superseded"`, updates the circular status from default `"active"`.
  - **Target Department & Addressee**: Scans key-value indicator lines (`department`, `subject`, `meant for`).

---

### 3. Content Extraction Strategy: HTML-First Architecture (`extract_body_from_html`)

RBI publishes circulars directly as HTML web pages as well as downloadable PDFs. V1 applies an **HTML-First** extraction principle because HTML retains the canonical structure, native table DOM, and paragraph tags.

#### A. DOM Sanitation & Region Selection
1. **Decomposes non-content elements**: Strips `<script>` and `<style>` tags.
2. **Identifies content boundary**:
   - Searches for specific RBI content wrappers in order of priority:
     1. `<div id="leftnavigation">`
     2. `<div class="content">`
     3. `<td class="tablecontent">`
     4. `<table class="tablebg">`
   - Excludes extraneous wrapper tables where possible.

#### B. Noise & Navigation Removal (`_is_nav_text`)
RBI pages embed global portal navigation, social media links, and footer disclaimers. The scraper explicitly filters out these noise tokens:
* `"rbi kehta hai"`, `"tenders"`, `"right to information"`, `"follow rbi"`, `"rss"`
* `"twitter"`, `"youtube"`, `"instagram"`, `"facebook"`, `"linkedin"`
* `"rbi clarifications"`, `"rbi's vision"`, `"what's new"`, `"press releases"`
* `"notifications"`, `"speeches"`, `"rbi website"`, `"sitemap"`, `"contact us"`, `"careers"`, `"opportunities"`

#### C. HTML Table Conversion (`html_table_to_md`)
* Traverses `<table>` elements and iterates over rows (`<tr>`) and cells (`<td>`, `<th>`).
* Formats rows into GitHub Flavored Markdown (GFM) pipe-table syntax:
  ```markdown
  | Header 1 | Header 2 | Header 3 |
  | --- | --- | --- |
  | Val 1    | Val 2    | Val 3    |
  ```

#### D. Extraction Gate
* If `len(html_body.strip()) >= 200`, the engine accepts the HTML extraction (`source_method = "html"`).
* If HTML is absent, empty, or truncated (< 200 characters), it triggers the **PDF Fallback Pipeline**.

---

### 4. PDF Fallback Pipeline (`extract_from_pdf`)

If the HTML detail page is insufficient:
1. **PDF Link Discovery**: Scans detail page HTML for anchor tags referencing `.pdf` files (`<a href="...pdf">`).
2. **Local Caching**: Downloads the PDF into `pdfs_ingest_1_09_final/`. Skips downloading if the file already exists locally.
3. **PDF Validation**: Verifies magic bytes (`pdf_resp.content[:4] == b"%PDF"`).
4. **Coordinate-Aware Text & Table Separation**:
   - Standard PDF text extraction scrambles tabular rows and running prose.
   - V1 uses `pdfplumber` to detect table bounding boxes (`page.find_tables()`).
   - `_chars_to_prose(page.chars, bboxes)`:
     - Rejects any character falling within a table bounding box (`_in_bbox`).
     - Groups remaining characters into horizontal text lines (grouping characters within 3 units vertically) and sorts by horizontal `x0` coordinates.
     - Preserves clean paragraphs without interleaved table text.
   - Converts detected table cells into GFM markdown tables (`_table_to_md`).
5. **Multimodal Gemini Vision Table Fallback (`gemini_table_markdown`)**:
   - If a table has corrupted columns, fewer than 4 pipes, or irregular cell alignment:
     - Renders the specific PDF page to a 150 DPI PNG byte stream using PyMuPDF (`fitz`).
     - Calls **Google Gemini** (`gemini-2.5-flash` via `google.genai`):
       > *"Extract ONLY the table(s) in this image into a single markdown table. Where a row has sub-rows nested under it, repeat the parent row's label in each sub-row so every row is fully self-contained. Return only the markdown table, no commentary."*
     - Replaces the corrupted table with the AI-reconstructed clean Markdown table.
6. **Multi-Page Table Stitching (`stitch_multipage_tables`)**:
   - Uses regex to detect split tables separated by page breaks or markdown boundary gaps.
   - If the continuation table repeats the header row, drops the duplicate header and joins the body rows.

---

### 5. Document De-nesting & Deduplication (`keep_last_complete_circular`)

A notable quirk of RBI's content management system is that circular web pages frequently embed the circular text **two to three times in nested wrapper containers**:
1. Outer summary/wrapper table
2. Intermediate container table
3. Final complete official circular with formal salutation, body, and signature

The V1 scraper solves this cleanly:
* Finds all occurrences of the circular's reference number (`REF_PATTERN`) in the extracted text.
* Scans backward from the last match to find the block containing standard formal sign-offs:
  - `"Yours faithfully"`, `"Yours sincerely"`, `"Yours truly"`
  - Official designations: `"Chief General Manager"`, `"General Manager"`, `"Governor"`
* Keeps the **last complete instance**, discarding previous repetitive HTML wrapper text while maintaining document integrity.

---

### 6. Signatory & Designation Extraction Engine (`signatory_from_text`)

To enable compliance auditing and citation attribution, the scraper extracts the issuing officer's name and designation using a **bottom-up scanning heuristic**:

1. **Frontmatter Stripping**: Strips leading YAML frontmatter so metadata headers aren't mistaken for document body content.
2. **Reverse Scanning Window**: Examines the last 20 non-empty lines of the circular.
3. **Pattern Matching Hierarchy**:
   - **Pattern A (Parenthesized Name)**: Detects names enclosed in parentheses (e.g., `(R. K. Moolchandani)`), followed or preceded by an official designation.
   - **Pattern B (Name Above Designation)**: Detects a name line directly preceding a designation line matching `SIGNATORY_REGEX`.
   - **Pattern C (Designation Above Name)**: Detects designation preceding the officer's name.
   - **Pattern D (Closing Salutation Fallback)**: Scans forward from formal closing phrases (`"yours faithfully"`).
4. **Heuristic Name Validation (`_looks_like_name`)**:
   - Length between 1 and 60 characters.
   - Contains no numerical digits.
   - Filters out all-uppercase header blocks.
   - Requires at least two title-cased words or standard initial patterns (e.g., `"S. K. Jain"`).
5. **Designation Regex (`SIGNATORY_REGEX`)**: Recognizes 14 official RBI designations:
   - Governor, Deputy Governor, Executive Director (ED)
   - Principal Chief General Manager (PCGM / PCGM-i-C)
   - Chief General Manager (CGM / CGM-i-C)
   - General Manager (GM / GM-i-C)
   - Deputy General Manager (DGM)
   - Assistant General Manager (AGM)
   - Chief Manager, Deputy Chief Manager
   - Regional Director, Principal Regional Director
   - Secretary, Officer-in-Charge

---

### 7. Regulatory Citation & Reference Extraction (`inject_reference_links`)
* Regulators frequently cite earlier circulars (e.g., *"Attention is invited to circular RBI/2022-23/12..."*).
* V1 extracts all referenced circular numbers matching `RBI/\d{4}-\d{2}/\d+`.
* Deduplicates references and persists them into the relational `circular_references` table.
* Injects clickable Markdown hyperlinks (`[RBI/YYYY-YY/NNN](url)`) into the circular text where target URLs are known.

---

### 8. Tier-0 Quality Assurance Engine (`run_tier0_checks`)

Before writing to the database, every circular is passed through an automated 4-stage validation gate:

| Check Name | Target Evaluated | Pass Condition | Action on Failure |
| :--- | :--- | :--- | :--- |
| **`required_field_check`** | `ref_no`, `date_issued`, `subject`, `signatory_name` | All 4 fields must be non-empty strings. | Marks `extraction_status = 'needs_review'`. Logs missing fields. |
| **`ref_no_format_check`** | `ref_no` | Must strictly match `^RBI/\d{4}-\d{2}/\d+$`. | Marks `extraction_status = 'needs_review'`. |
| **`signatory_designation_check`**| `signatory_designation` | Must match recognized titles in `SIGNATORY_REGEX`. | Warnings logged; status flagged if invalid. |
| **`table_column_consistency_check`** | Markdown tables in `body_markdown` | All data rows must have the exact column count as header row. | Catches malformed tables; logs inconsistent row indices. |

---

## 🗄️ Database Architecture (`ingest_1_09_final.db`)

The scraper automatically creates and maintains 4 normalized SQLite tables:

### 1. `circulars` Table
Primary repository for circular metadata, body text, and processing state.
```sql
CREATE TABLE IF NOT EXISTS circulars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_no TEXT UNIQUE NOT NULL,             -- e.g., 'RBI/2026-27/236'
    rbi_page_id TEXT,                         -- Internal RBI page ID
    rbi_page_url TEXT NOT NULL,              -- Full URL to circular detail page
    title TEXT,                              -- Circular title/subject
    date_issued TEXT,                        -- Formal date (e.g., 'March 15, 2026')
    department TEXT,                         -- Issuing department (e.g., 'DoR')
    subject TEXT,                            -- Official subject line
    meant_for TEXT,                          -- Target banking entities
    status TEXT DEFAULT 'active',            -- 'active', 'withdrawn', 'superseded'
    signatory_name TEXT,                     -- Name of signing authority
    signatory_designation TEXT,              -- Designation of signing authority
    body_markdown TEXT,                      -- Clean extracted Markdown content
    footnote_text TEXT,                      -- Footnotes and endnotes
    source_method TEXT,                      -- 'html' or 'pdf_pdfplumber'
    pdf_url TEXT,                            -- Download URL of original PDF
    content_hash TEXT NOT NULL,              -- SHA-256 hash of header + body
    extraction_status TEXT DEFAULT 'pending',-- 'verified' or 'needs_review'
    scraped_at TEXT DEFAULT (datetime('now'))
);
```

### 2. `circular_references` Table
Captures cross-circular regulatory citations for compliance dependency graphs.
```sql
CREATE TABLE IF NOT EXISTS circular_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ref_no TEXT NOT NULL,             -- Circular making the reference
    referenced_ref_no TEXT NOT NULL,         -- Cited circular number
    reference_type TEXT NOT NULL,            -- e.g., 'related', 'supersedes'
    url TEXT                                 -- Link to referenced circular
);
```

### 3. `validation_results` Table
Audit log of all automated Tier-0 QA test results.
```sql
CREATE TABLE IF NOT EXISTS validation_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    circular_ref_no TEXT NOT NULL,
    tier INTEGER NOT NULL,                   -- 0 for Tier-0
    check_name TEXT NOT NULL,                -- e.g., 'required_field_check'
    result TEXT NOT NULL,                    -- 'pass', 'warning', 'fail'
    details TEXT,                            -- Specific diagnostics/errors
    checked_at TEXT DEFAULT (datetime('now'))
);
```

### 4. `ingestion_runs` Table
Execution history tracking batch runs, volumes, and runtime completion states.
```sql
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT DEFAULT (datetime('now')),
    new_count INTEGER,
    skipped_count INTEGER,
    status TEXT                              -- 'running', 'done'
);
```

---

## 📄 Output Markdown Structure

Each processed circular generates a `.md` file in `V1/output_1_09_final/` named using a sanitized circular reference (e.g., `RBI_2026-27_236.md`).

### Structure Specification:
```markdown
---
circular_number: "RBI/2026-27/236"
date_issued: "March 15, 2026"
department: "Department of Regulation"
subject: "Master Direction – Reserve Bank of India (Prudential Norms on Income Recognition...)"
status: "active"
meant_for: "All Commercial Banks (excluding RRBs)"
---

# Master Direction – Reserve Bank of India (Prudential Norms on Income Recognition...)

1. Please refer to our circular [RBI/2024-25/12](https://www.rbi.org.in/...) dated April 1, 2024...

| Category | Asset Classification Norm | Provisioning Requirement |
| --- | --- | --- |
| Standard | Performing loan | 0.40% |
| Sub-standard | Non-performing <= 12 months | 15.00% |

Yours faithfully,

(Veena Srivastava)
Chief General Manager
```

---

## ⚙️ Configuration Parameters & Environment Variables

| Variable / Setting | Default Value | Description |
| :--- | :--- | :--- |
| `INDEX_URL` | `https://www.rbi.org.in/scripts/BS_CircularIndexDisplay.aspx` | Seed index URL for circular listings. |
| `REQUEST_DELAY` | `0.9` seconds | Politeness delay between successive HTTP requests to avoid 429/rate-limiting. |
| `REQUEST_TIMEOUT` | `40` seconds | Socket read/connect timeout for resilient network fetching. |
| `MAX_CIRCULARS` | `26` | Default batch size when `--limit` is not specified. |
| `GEMINI_API_KEY` | `os.getenv("GEMINI_API_KEY", "")` | API key for Gemini multimodal table vision fallback. |
| `GEMINI_MODEL` | `os.getenv("GEMINI_MODEL", "gemini-2.5-flash")` | Model used for vision table reconstruction. |
| `PDF_DIR` | `V1/pdfs_ingest_1_09_final/` | Local directory storing raw downloaded PDF files. |
| `OUTPUT_DIR` | `V1/output_1_09_final/` | Local directory storing generated clean `.md` files. |
| `DB_PATH` | `V1/ingest_1_09_final.db` | Local SQLite database file path. |

---

## 💻 CLI Execution Guide

### 1. Basic Execution (Default Limit of 26 circulars)
```powershell
cd d:\RBI_circulars_project\V1
python ingest_one_circular_final_v2.py
```

### 2. Fast Test Ingestion (First 5 Circulars)
```powershell
python ingest_one_circular_final_v2.py --limit 5
```

### 3. Verification & Database Inspection
You can verify the database contents via SQLite CLI or Python:
```powershell
python -c "import sqlite3; conn = sqlite3.connect('ingest_1_09_final.db'); print('Total Circulars:', conn.execute('SELECT count(*) FROM circulars').fetchone()[0]); print('Verified:', conn.execute('SELECT count(*) FROM circulars WHERE extraction_status=\"verified\"').fetchone()[0])"
```

---

## 🛡️ Robustness & Edge-Case Defenses

1. **Anti-Duplication**: Computes SHA-256 hash of header block + body markdown to avoid re-inserting unchanged circulars.
2. **Rate Limiting & Politeness**: 0.9s delay between requests avoids triggering RBI firewall IP blocks.
3. **Encoding Resiliency**: On Windows systems, explicitly runs `sys.stdout.reconfigure(encoding='utf-8')` to prevent crashes when printing Unicode symbols or rupee (`₹`) currency characters.
4. **Table Collision Prevention**: Replaces pipe characters (`|`) with slashes (`/`) inside text cells to preserve Markdown table alignment.
5. **Atomic Run Tracking**: Ingestion run states (`running` -> `done`) and counts (`new_count`, `skipped_count`) are tracked in `ingestion_runs` even if a run is aborted prematurely.
