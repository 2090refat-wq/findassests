# Task: Build a database of unclaimed dividends in Bangladesh from public CMSF and company lists

## Who I am and why

I run a law firm in Bangladesh. We help shareholders, and the legal heirs of deceased shareholders, recover unclaimed cash dividends, stock (bonus) dividends, fractional dividends, right shares and IPO refund money. This money is held by listed companies or has been transferred to the Capital Market Stabilization Fund (CMSF) under the BSEC (CMSF) Rules, 2021.

Listed companies and CMSF publish lists of unclaimed items as PDFs. I need every one of those lists downloaded, the tables extracted, and the result turned into one clean database. The database should show, for each shareholder (BO account or folio), every unclaimed item they have across all companies.

## Files attached to this session

| File | What it is |
|---|---|
| `PROMPT.md` | These instructions |
| `download_list.csv` | 287 documents to fetch. Columns: `id, company, type, title, url, kind, role, source, note` |
| `download_documents.py` | Ready-made, resumable downloader (writes `downloads/`, `manifest.csv`, `discovered_links.csv`) |
| `schema.sql` | Target SQLite schema. The output CSVs and Sheets use the same column names |
| `cmsf_master_links.csv` | The full research list (301 rows), including gaps, for reference |
| `manual_gaps.csv` | 16 sites that could not be read automatically (JavaScript-only, broken SSL, dead domains) |
| `allowed_domains.txt` | The domains this session needs to be allowed to reach |

In `download_list.csv`:
- `kind`: `direct_file` (URL is the file), `gdrive` (Google Drive share link), `wp_download_page` (WordPress download page that links to the file), `html_page` (web page containing the file links)
- `role`: `list` (parse it), `summary` (company or year totals only; parse into the documents table, not dividends), `settled_claims` (CMSF lists of already-paid claims; parse into `settled_claims`), `reference_only` (forms, notices, guides; download only), `check_relevance` (open it and decide whether it is an unclaimed list)

## Working rules

- Set up a task list first and keep it updated. I may not be watching.
- Work in batches of about 20 documents. Save progress to disk after each batch so the job can resume if the session restarts. Never re-download or re-parse a document already marked done.
- Do all heavy work in code: Python, pandas, pdfplumber/camelot, SQLite. Do not paste large tables into the chat.
- Keep every value traceable to its source: `doc_id`, page and row for every record. Never invent, guess or fill in missing names, numbers or amounts. If a cell is unreadable, leave it empty and log it.
- Use only what is printed in these public lists. Do not look up, enrich or add phone numbers, emails, NID numbers or any other personal data from other sources. Treat the output as confidential client-intake data.
- If something blocks you (network, a broken file, an ambiguous layout), log it and move on. Report it at the end rather than stopping.

## Step 0: Check the environment

1. Install what you need: `pip install requests beautifulsoup4 pypdf pdfplumber camelot-py[base] opencv-python-headless rapidfuzz pandas openpyxl unidecode` (add `--break-system-packages` if pip asks). For scanned PDFs: `apt-get install -y tesseract-ocr tesseract-ocr-ben ghostscript poppler-utils` if apt is available, then `pip install pytesseract pdf2image`.
2. Test network access: `python download_documents.py --only D001,D046,D120`.
   - If these fail with a proxy/403/CONNECT error, the session's network is restricted. **Stop and tell me.** I will either set this environment's network access to Custom with the domains in `allowed_domains.txt`, or run `download_documents.py` on my own computer and upload the `downloads/` folder plus `manifest.csv` as a zip.
   - If I upload a zip instead, unzip it in place and continue from Step 2.

## Step 1: Download

1. Run `python download_documents.py`. It skips `reference_only` by default, writes `manifest.csv` after every document, and is safe to re-run.
2. Run `python download_documents.py --retry-failed` once more for transient failures.
3. For `html_page` and `wp_download_page` rows, the script follows the links it finds inside the page (child ids like `D088-01`). Check `discovered_links.csv`. If a page gave unrelated files (annual reports and so on), mark those children `skipped` and don't parse them.
4. De-duplicate by `sha256`. The same PDF is often on both CMSF and the company site. Keep one, and record the duplicate ids in `issues`.
5. Report: files ok, failed (with reasons), duplicates, total pages.

## Step 2: Triage each file

For every downloaded file, record in `documents`:
- `pdf_kind`: `text` if pdfplumber finds real characters on most pages, `scanned` if pages are images, `mixed` if both, otherwise `spreadsheet` or `other`.
- Whether the text is English, Bangla or both. Bangla digits (০১২৩৪৫৬৭৮৯) must be converted to 0–9.
- What it contains: a name-level list (one row per shareholder) or only a summary (year totals). Summaries go in `documents` only.
- The printed grand total, if there is one. Put it in `stated_total`.

Open 2–3 sample pages of each new layout before writing an extractor for it. Many companies reuse the same registrar format, so group files by layout and write one extractor per layout rather than one per file.

## Step 3: Extract the tables

Use this order of methods per file and record which one worked in `extraction_method`:
1. `pdfplumber` table extraction (try both lattice-style and text-alignment settings).
2. `camelot` (lattice, then stream) for ruled tables pdfplumber misses.
3. Line-by-line text parsing with regular expressions for the layout. Typical row shape: `SL | Warrant | Folio/BO | Name | Father/Husband | Address | Shares | Gross | Tax | Net`.
4. OCR (`tesseract -l eng+ben`, 300 dpi) only for scanned pages. Flag every OCR row with `issues = "ocr"` because OCR names and numbers need human checking.
5. `.xlsx` files: read directly with pandas.

Handle these known problems:
- Header rows repeated on every page; page footers; "Total", "Sub-total" and "Grand Total" rows. Exclude all of them from `dividends`, but use the totals for checking.
- Rows split across two lines (long names and addresses). Merge them.
- Multi-year files where the year is a section heading rather than a column. Carry the heading year down to each row.
- Amounts like `1,234.50`, `Tk. 500`, `(500)`, or Bangla digits. Convert to numbers.
- BO IDs: valid only when exactly 16 digits after removing spaces and dashes. Otherwise keep the raw value in `folio_or_bo_raw` and put it in `folio_no` if it looks like a folio.
- Stock and bonus lists: put the share count in `shares` and leave `net_amount` empty.
- Set `dividend_type` from the document title or the section (cash, stock, fraction, right, ipo_refund).

Save one raw CSV per document in `extracted/raw/<doc_id>.csv` before normalising, so it can be re-checked later.

## Step 4: Validate each document

- Compare the sum of `net_amount` (or shares) with `stated_total`. Within 0.5% → `match`. Otherwise → `mismatch`, with the difference noted in `issues`.
- Check that row counts are plausible against the page count.
- Flag rows with no name, no amount, a non-numeric amount, or a duplicated (company, year, folio/BO, amount).
- A document is `parsed` only if the totals match (or no total is printed) and fewer than 2% of rows are flagged. Otherwise mark it `partial` and list why.

## Step 5: Normalise names (for matching only; keep the raw name)

`holder_name_norm`:
- Uppercase; strip punctuation and extra spaces; transliterate with unidecode.
- Remove titles: MR, MRS, MS, MISS, DR, PROF, ALHAJ, ALHAJJ, HAJI, HAJEE, ENGR, ADV, LATE.
- Standardise common forms: MD / MD. / MOHD / MOHAMMAD / MOHAMMED / MUHAMMAD → MOHAMMAD; MST / MOST / MOSAMMAT / MOSAMMOT → MOSAMMAT; ABDUL / ABDL → ABDUL; UDDIN / UDDEEN → UDDIN; CHOWDHURY / CHOUDHURY / CHAUDHURY → CHOWDHURY; BEGUM / BEGAM → BEGUM.
- Leave Bangla-script names as they are, and add a transliterated copy for comparison.
- Mark institutional holders (LTD, LIMITED, PLC, BANK, FUND, TRUST, SECURITIES, INVESTMENT) with `issues = "institution"`. Keep them, but they are low priority for us.

## Step 6: Match the same holder across companies

Build `holders` and assign `holder_id`, in this order of confidence:
1. **`bo_exact`**: same valid 16-digit BO ID → same holder (score 1.0).
2. **`folio_company`**: same company + same folio number → same holder (score 1.0). Folio numbers are only unique within one company.
3. **`name_fuzzy`**: only for records with no BO ID. Same normalised name (rapidfuzz `token_sort_ratio` ≥ 95) **and** a second field that agrees: father/spouse name ≥ 90, or address similarity ≥ 85. Auto-merge only if both conditions hold.
4. Name ≥ 90 but no second field to confirm, or a conflicting second field → do not merge. Add the pair to `review_queue` with the evidence.
5. Everything else → its own `holder_id`, `match_method = unmatched`.

A wrong merge is worse than a missed one, because we would attribute money to the wrong person. Never merge two different valid BO IDs.

Then:
- Fill each holder's totals: companies, items, cash total, share total, first and last year.
- Parse `settled_claims` and set `settled_flag = 1` on any dividend matching on BO ID, or on company + folio, and amount. Don't delete them.

## Step 7: Outputs

Write these to `output/`:

| File | Content |
|---|---|
| `unclaimed.sqlite` | All tables from `schema.sql` |
| `dividends_all.csv` | Every item, all columns |
| `holders.csv` | One row per holder, sorted by `total_cash_bdt` descending |
| `holder_items.csv` | Holder → item list (holder columns + dividend columns), for working case by case |
| `top_holders.csv` | Holders with `total_cash_bdt` ≥ 50,000 or ≥ 3 companies, not settled, not institutions |
| `review_queue.csv` | Pairs for me to decide |
| `documents_report.csv` | Per document: method, rows, totals check, status, issues |
| `failed_or_partial.csv` | Anything that needs my attention, with the reason |
| `summary.md` | Totals: documents parsed, rows, holders, BDT by company and year, match statistics, known gaps |

**Google Sheets:** if a Google Drive connector is available in this session, create one Google Sheet named "Unclaimed Dividends BD – <today's date>" with tabs `holders`, `top_holders`, `dividends_all`, `review_queue` and `documents_report`. Freeze the header row on each tab and keep the sharing private (do not share it). Google Sheets has a 10-million-cell limit; if `dividends_all` would exceed it, keep it as CSV only and say so. If no Drive connector is available, also produce `output/unclaimed_dividends.xlsx` with the same tabs (bold header, frozen first row, filters on).

## Step 8: Check before you report

- Re-open 10 random records against the source PDF page and confirm the name, BO/folio and amount match. Report the result.
- Re-run the totals check across all documents.
- Spot-check 10 auto-merged holders from `name_fuzzy` and confirm they are plausible.
- Confirm no row exists without `doc_id`, `page` and `company`.

## Final report to me (short)

- How many documents were downloaded, parsed, partial or failed, and why.
- Total rows, unique holders, total unclaimed cash (BDT) and shares, top 10 companies by amount.
- How many holders appear in 2 or more companies.
- What still needs manual work: the items in `manual_gaps.csv`, failed downloads, partial parses and the review queue size.
- Links to the Google Sheet (if created) and the output files.

## Manual gaps (do not try to break through these)

`manual_gaps.csv` lists sites that need a normal browser (JavaScript-only pages, broken SSL certificates, dead domains). Do not try to bypass blocks or certificate errors. I will download those files myself and upload them to a `manual_uploads/` folder. When they appear, give each one an id like `M001`, add it to `documents`, and run Steps 2–7 on it.

Note: the domain `afchem.com` (Active Fine Chemicals) now redirects to an unrelated shop site. Never fetch it.
