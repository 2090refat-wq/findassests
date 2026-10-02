-- Target database for the unclaimed-dividend project (SQLite).
-- The CSV / Google Sheet outputs use exactly these column names.

-- One row per source document (filled from manifest.csv + extraction results)
CREATE TABLE IF NOT EXISTS documents (
  doc_id            TEXT PRIMARY KEY,   -- id from download_list / manifest, e.g. D014 or D014-02
  company           TEXT NOT NULL,
  title             TEXT,
  source_url        TEXT NOT NULL,
  local_path        TEXT,
  sha256            TEXT,
  pages             INTEGER,
  role              TEXT,               -- list | summary | settled_claims | reference_only | check_relevance
  pdf_kind          TEXT,               -- text | scanned | mixed | spreadsheet | other
  extraction_method TEXT,               -- pdfplumber | camelot | text_regex | ocr | xlsx | manual
  rows_extracted    INTEGER,
  stated_total      REAL,               -- grand total printed in the document, if any
  extracted_total   REAL,               -- sum of net_amount we extracted
  total_check       TEXT,               -- match | mismatch | no_total_printed
  status            TEXT,               -- parsed | partial | failed | skipped
  issues            TEXT
);

-- One row per unclaimed item (the core table)
CREATE TABLE IF NOT EXISTS dividends (
  dividend_id       TEXT PRIMARY KEY,   -- <doc_id>-p<page>-r<row>, stable and traceable
  doc_id            TEXT NOT NULL REFERENCES documents(doc_id),
  company           TEXT NOT NULL,
  page              INTEGER,
  row_on_page       INTEGER,
  dividend_year     TEXT,               -- as printed: "2018", "2018-19", "FY2019-20", "13th AGM-2007"
  year_start        INTEGER,            -- normalised first year, e.g. 2018
  dividend_type     TEXT,               -- cash | stock | fraction | right | ipo_refund | other
  warrant_no        TEXT,
  folio_no          TEXT,
  bo_id             TEXT,               -- exactly 16 digits when valid, else NULL (raw kept in folio_or_bo_raw)
  folio_or_bo_raw   TEXT,
  holder_name_raw   TEXT,               -- exactly as printed
  holder_name_norm  TEXT,               -- normalised for matching (see PROMPT.md)
  father_or_spouse  TEXT,               -- only if printed in the public list
  address           TEXT,               -- only if printed in the public list
  shares            REAL,               -- number of shares (stock lists / holding)
  gross_amount      REAL,
  tax_amount        REAL,
  net_amount        REAL,               -- BDT; for stock lists leave NULL and use shares
  currency          TEXT DEFAULT 'BDT',
  holder_id         TEXT,               -- filled by the matching step
  match_method      TEXT,               -- bo_exact | folio_company | name_fuzzy | unmatched
  match_score       REAL,
  settled_flag      INTEGER DEFAULT 0,  -- 1 if found in a CMSF settled-claims list
  raw_row_json      TEXT                -- the original extracted cells, for audit
);

-- One row per person/account after matching
CREATE TABLE IF NOT EXISTS holders (
  holder_id         TEXT PRIMARY KEY,   -- H000001 ...
  bo_id             TEXT,
  folio_numbers     TEXT,               -- "; " separated, prefixed with company, e.g. "AB Bank:B02-001"
  name_canonical    TEXT,
  name_variants     TEXT,               -- "; " separated
  father_or_spouse  TEXT,
  companies_count   INTEGER,
  items_count       INTEGER,
  total_cash_bdt    REAL,
  total_shares      REAL,
  first_year        INTEGER,
  last_year         INTEGER,
  status            TEXT DEFAULT 'new'  -- new | contacted | engaged | deceased | closed (for the firm's later use)
);

-- CMSF already-settled claims, used to flag items that are no longer claimable
CREATE TABLE IF NOT EXISTS settled_claims (
  settled_id        TEXT PRIMARY KEY,
  doc_id            TEXT,
  company           TEXT,
  bo_id             TEXT,
  folio_no          TEXT,
  holder_name_raw   TEXT,
  amount_or_shares  REAL,
  settlement_date   TEXT,
  raw_row_json      TEXT
);

-- Pairs a human must decide (never auto-merged)
CREATE TABLE IF NOT EXISTS review_queue (
  review_id         TEXT PRIMARY KEY,
  dividend_id_a     TEXT,
  dividend_id_b     TEXT,
  holder_id_a       TEXT,
  holder_id_b       TEXT,
  name_a            TEXT,
  name_b            TEXT,
  evidence          TEXT,               -- e.g. "name 93; father name 88; different companies"
  score             REAL,
  decision          TEXT                -- blank | same | different
);

CREATE INDEX IF NOT EXISTS ix_div_bo     ON dividends(bo_id);
CREATE INDEX IF NOT EXISTS ix_div_folio  ON dividends(company, folio_no);
CREATE INDEX IF NOT EXISTS ix_div_name   ON dividends(holder_name_norm);
CREATE INDEX IF NOT EXISTS ix_div_holder ON dividends(holder_id);
