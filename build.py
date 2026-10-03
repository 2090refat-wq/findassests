#!/usr/bin/env python3
"""Steps 4-7: validate, normalise, match, write outputs. Reads extracted/raw + extracted/stats + manifest.csv."""
import csv, json, re, sqlite3, itertools
from pathlib import Path
from collections import defaultdict, Counter
import pandas as pd
from totalcheck import score_raw, candidates, check, year_hits, year_err
from unidecode import unidecode
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"; OUT.mkdir(exist_ok=True)

man = {r["id"]: r for r in csv.DictReader(open(ROOT / "manifest.csv", encoding="utf-8-sig"))}
tri = {r["id"]: r for r in csv.DictReader(open(ROOT / "extracted/triage.csv", encoding="utf-8"))}
ocr_ids = {p.stem for p in (ROOT / "extracted/raw").glob("*.csv")}

# ---------- duplicates by sha256 ----------
first_by_sha, dup_of = {}, {}
for i, m in man.items():
    if m["status"] != "ok":
        continue
    s = m["sha256"]
    if s in first_by_sha:
        dup_of[i] = first_by_sha[s]
    else:
        first_by_sha[s] = i

# ---------- name normalisation ----------
TITLES = {"MR", "MRS", "MS", "MISS", "DR", "PROF", "ALHAJ", "ALHAJJ", "HAJI", "HAJEE", "ENGR", "ADV", "LATE"}
MAP = {"MD": "MOHAMMAD", "MOHD": "MOHAMMAD", "MOHAMMED": "MOHAMMAD", "MUHAMMAD": "MOHAMMAD",
       "MST": "MOSAMMAT", "MOST": "MOSAMMAT", "MOSAMMOT": "MOSAMMAT", "ABDL": "ABDUL", "UDDEEN": "UDDIN",
       "CHOUDHURY": "CHOWDHURY", "CHAUDHURY": "CHOWDHURY", "BEGAM": "BEGUM"}
INSTITUTION = {"LTD", "LIMITED", "PLC", "BANK", "FUND", "TRUST", "SECURITIES", "INVESTMENT"}


def norm_name(raw):
    s = unidecode(raw or "").upper()
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    toks = [MAP.get(t, t) for t in s.split() if t not in TITLES]
    return " ".join(toks), bool(INSTITUTION & set(toks))


def year_start(y):
    m = re.search(r"(19|20)\d\d", y or "")
    return int(m.group(0)) if m else None



NUMTOK = re.compile(r"^\(?-?\d[\d,]*(\.\d+)?\)?$")


def resolve_ambiguous(raw, st, extra_totals):
    """Stock rows with several numbers after the name: pick first/last number only if that makes the document total match the printed total."""
    amb = raw["issues"].str.contains("stock_ambiguous_numbers") & raw["shares"].isna()
    if not amb.any():
        return raw
    runs = []
    for idx in raw.index[amb]:
        line, name = raw.at[idx, "line"], raw.at[idx, "holder_name_raw"]
        p = line.find(name)
        tail = line[p + len(name):].split() if p >= 0 else []
        run = []
        for t in tail:
            if NUMTOK.match(t):
                run.append(float(t.replace(",", "").strip("()")))
            else:
                break
        runs.append((idx, run))
    known = raw.loc[~amb, "shares"].sum()
    cands = candidates(st, extra_totals)
    if not cands:
        return raw
    for name, pick in (("last", lambda r: r[-1]), ("first", lambda r: r[0])):
        vals = {i: pick(r) for i, r in runs if r}
        total = known + sum(vals.values())
        status, _ = check(total, cands)
        if status == "match":
            for i, v in vals.items():
                raw.at[i, "shares"] = v
                raw.at[i, "issues"] = raw.at[i, "issues"].replace("stock_ambiguous_numbers", f"shares_{name}_number_verified_by_total")
            return raw
    return raw

# ---------- documents + dividends ----------
docs, rows, settled = [], [], []
for doc_id, m in man.items():
    t = tri.get(doc_id, {})
    d = dict(doc_id=doc_id, company=m["company"], title=m["title"], source_url=m["url"], local_path=m["local_path"],
             sha256=m["sha256"], pages=int(m["pages"] or 0) if m["pages"] else None, role=m["role"],
             pdf_kind=t.get("pdf_kind", ""), extraction_method="", rows_extracted=0, stated_total=None,
             extracted_total=None, total_check="", status="", issues="")
    if m["status"] != "ok":
        d.update(status="failed", issues=m["error"][:200]); docs.append(d); continue
    if doc_id in dup_of:
        d.update(status="skipped", issues=f"duplicate of {dup_of[doc_id]} (same sha256)"); docs.append(d); continue
    if m["role"] in ("reference_only",):
        d.update(status="skipped", issues="reference only"); docs.append(d); continue
    if m["role"] == "summary":
        d.update(status="skipped", issues="summary document: totals only, not parsed into dividends"); docs.append(d); continue
    sp = ROOT / "extracted/stats" / f"{doc_id}.json"
    rp = ROOT / "extracted/raw" / f"{doc_id}.csv"
    if not sp.exists() or not rp.exists():
        d.update(status="failed" if t.get("pdf_kind") not in ("scanned",) else "failed",
                 issues="not extracted" + ("; scanned, needs OCR" if t.get("pdf_kind") == "scanned" else ""))
        docs.append(d); continue
    st = json.loads(sp.read_text())
    method = "text_regex+ocr" if st.get("ocr") and st.get("ocr_rows") else "text_regex"
    year_note = ""
    best_sc = score_raw(rp, st)
    ry = ROOT / "extracted" / "raw_years" / f"{doc_id}.csv"
    if ry.exists():
        sy = score_raw(ry, st)
        hits, nyears = year_hits(ry, st)
        med_err, frac6, _ = year_err(ry, st)
        approx = nyears >= 3 and frac6 >= 0.8 and med_err <= 0.06 and best_sc[0] != "match"
        if sy[3] > 0 and nyears >= 2 and ((hits == nyears) or (sy[0] == "match" and best_sc[0] != "match") or approx):
            rp, best_sc, method = ry, sy, "year_columns"
            if approx and hits != nyears:
                year_note = f"per-year column totals within {med_err:.1%} of printed (approximate; some rows missing)"
    for alt_dir, alt_name in (("raw_col", "column_position"), ("raw_pair", "paired_lines")):
        if method == "year_columns":
            break
        alt = ROOT / "extracted" / alt_dir / f"{doc_id}.csv"
        if not alt.exists():
            continue
        sa = score_raw(alt, st)
        if sa[3] > 0 and sa[2] <= 3 * max(best_sc[2], 1) and ((sa[0] == "match" and best_sc[0] != "match") or
                          (best_sc[0] != "match" and sa[0] != "n/a" and sa[1] < best_sc[1])):
            rp, best_sc, method = alt, sa, alt_name
    raw = pd.read_csv(rp, dtype=str, keep_default_na=False)
    d["extraction_method"] = method
    d["rows_extracted"] = len(raw)
    for c in ("shares", "gross_amount", "tax_amount", "net_amount"):
        raw[c] = pd.to_numeric(raw[c], errors="coerce")
    big = (raw["net_amount"].abs() >= 1e9) | (raw["shares"].abs() >= 1e9)
    raw.loc[big, ["net_amount", "shares"]] = None
    raw.loc[big, "issues"] = raw.loc[big, "issues"].apply(lambda x: (x + ";unreadable_amount").strip(";"))
    tot_mask = raw["holder_name_raw"].str.match(r"(?i)^\W*(grand\s+|sub\s*-?\s*)?total\b")
    extra_totals = [float(v) for v in raw.loc[tot_mask, "net_amount"].fillna(raw.loc[tot_mask, "shares"]).dropna()]
    raw = raw[~tot_mask].copy()
    _ia = raw["issues"].str.contains("int_amount")
    if _ia.any() and _ia.mean() < 0.5:       # mostly decimal amounts: whole-number "rows" are address/continuation noise
        raw = raw[~_ia].copy()
    raw = resolve_ambiguous(raw, st, extra_totals)
    addr = raw["holder_name_raw"].str.upper().str.contains(r"\b(?:FLOOR|HOUSE|ROAD|APT|FLAT|BUILDING|MANSION|TOWER|AVENUE)\b", regex=True)
    for mask, tag in ((raw["net_amount"] > 1_000_000, "implausible_amount"), (addr, "name_looks_like_address")):
        raw.loc[mask, "issues"] = raw.loc[mask, "issues"].apply(lambda x, t=tag: (x + ";" + t).strip(";"))
    d["rows_extracted"] = len(raw)
    is_stock = (raw["dividend_type"].isin(["stock", "right"])).mean() > 0.5 if len(raw) else False
    ext = float(raw["shares"].sum()) if is_stock else float(raw["net_amount"].sum())
    d["extracted_total"] = round(ext, 2)
    totals = [v for v in (st.get("totals") or []) + extra_totals if v >= 100]  # drop header noise such as "Total Balance 7"
    issues = []
    if totals:
        cands = candidates(st, extra_totals)
        best = min(cands, key=lambda c: abs(c - ext)) if cands else None
        d["stated_total"] = best
        if best and ext and abs(best - ext) <= 0.005 * best:
            d["total_check"] = "match"
        else:
            d["total_check"] = "mismatch"
            issues.append(f"extracted {ext:,.2f} vs nearest printed total {best}")
            vcol = "shares" if is_stock else "net_amount"
            ys = raw.groupby("dividend_year")[vcol].sum()
            hit = [y for y, v in ys.items() if v and any(abs(v - c) <= 0.005 * c for c in cands)]
            miss = [y for y in ys.index if y not in hit and ys[y]]
            if len(hit) >= 2 and len(miss) <= st.get("unreadable_totals", 0):
                d["total_check"] = "match"
                issues.pop()
                issues.append(f"per-year totals match for {len(hit)} years; {len(miss)} year total(s) unreadable ('#####') in the PDF")
    else:
        d["total_check"] = "no_total_printed"
    info = r"amount_repaired|year_column|col_amount|paired_lines|int_amount|shares_(?:first|last)_number_verified_by_total"
    bad = raw["issues"].str.replace(info, "", regex=True).str.replace(";", "", regex=False).str.strip().ne("")
    flagged = bad.mean() if len(raw) else 1
    if st.get("textless_pages"):
        issues.append(f"{st['textless_pages']} pages without text (need OCR)")
    if st.get("unparsed_idlike"):
        issues.append(f"{st['unparsed_idlike']} lines with an ID not parsed")
    if flagged:
        issues.append(f"{flagged:.1%} rows flagged")
    rep = (raw["issues"].str.contains("amount_repaired")).sum()
    if rep:
        issues.append(f"{rep} amounts repaired (gross-tax=net verified)")
    if st.get("error"):
        issues.append(st["error"])
    ok = (d["total_check"] in ("match", "no_total_printed") and flagged < 0.02 and not st.get("textless_pages")
          and st["unparsed_idlike"] <= max(2, 0.01 * len(raw)) and len(raw) > 0)
    if year_note:
        issues.append(year_note)
        ok = False
    d["status"] = "parsed" if ok else "partial"
    if st.get("ocr_rows"):
        d["status"] = "partial"
        issues.append(f"{st['ocr_rows']} rows from OCR: needs human check")
    d["issues"] = "; ".join(issues)
    docs.append(d)

    if m["role"] == "settled_claims":
        for i, r in raw.iterrows():
            settled.append(dict(settled_id=f"{doc_id}-S{i+1}", doc_id=doc_id, company=m["company"],
                                bo_id=r["bo_id"] if re.fullmatch(r"\d{16}", r["bo_id"] or "") else "",
                                folio_no=r["folio_no"], holder_name_raw=r["holder_name_raw"],
                                amount_or_shares=r["net_amount"] if pd.notna(r["net_amount"]) else r["shares"],
                                settlement_date="", raw_row_json=json.dumps(r.to_dict())))
        continue
    if m["role"] == "check_relevance" and d["status"] != "parsed":
        continue
    for i, r in raw.iterrows():
        bo = r["bo_id"] if re.fullmatch(r"\d{16}", r["bo_id"] or "") else ""
        nn, inst = norm_name(r["holder_name_raw"])
        iss = [x for x in r["issues"].split(";") if x]
        if inst:
            iss.append("institution")
        rows.append(dict(
            dividend_id=f"{doc_id}-p{r['page']}-r{r['row_on_page']}", doc_id=doc_id, company=m["company"],
            page=int(r["page"]), row_on_page=int(r["row_on_page"]), dividend_year=r["dividend_year"],
            year_start=year_start(r["dividend_year"]), dividend_type=r["dividend_type"], warrant_no=r["warrant_no"],
            folio_no=r["folio_no"], bo_id=bo, folio_or_bo_raw=r["folio_or_bo_raw"],
            holder_name_raw=r["holder_name_raw"], holder_name_norm=nn, father_or_spouse="", address="",
            shares=r["shares"], gross_amount=r["gross_amount"], tax_amount=r["tax_amount"], net_amount=r["net_amount"],
            currency="BDT", holder_id="", match_method="", match_score=None, settled_flag=0,
            issues=";".join(iss), raw_row_json=json.dumps({"line": r["line"]})))

docs_df0 = pd.DataFrame(docs)
import os
if os.environ.get("DOCS_ONLY"):
    docs_df0.to_csv(OUT / "documents_report.csv", index=False)
    print(docs_df0.status.value_counts().to_dict(), docs_df0.total_check.value_counts().to_dict())
    raise SystemExit
div = pd.DataFrame(rows)
print("dividend rows:", len(div))
_st = {r["doc_id"]: r["status"] for r in docs}
_tc = {r["doc_id"]: r["total_check"] for r in docs}
# whole-number amounts are ambiguous with share counts: trust them only when the document total matched
iam = div["issues"].str.contains("int_amount") & (div["doc_id"].map(_tc) != "match")
div.loc[iam, "issues"] = div.loc[iam, "issues"].apply(lambda x: (x + ";unverified_int_amount").strip(";"))

# duplicated (company, year, folio/bo, amount)
key = div["company"] + "|" + div["dividend_year"].astype(str) + "|" + div["bo_id"].where(div["bo_id"] != "", div["folio_no"]) \
    + "|" + div["net_amount"].fillna(div["shares"]).astype(str)
hasid = (div["bo_id"] != "") | (div["folio_no"] != "")
dupmask = key.duplicated(keep=False) & hasid
div.loc[dupmask, "issues"] = div.loc[dupmask, "issues"].apply(lambda x: (x + ";dup_row").strip(";"))

# ---------- matching ----------
parent = {}


def find(x):
    while parent.setdefault(x, x) != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[rb] = ra


method_of = {}
by_bo = defaultdict(list); by_folio = defaultdict(list)
for idx, r in div.iterrows():
    if r["bo_id"]:
        by_bo[r["bo_id"]].append(idx)
    elif r["folio_no"]:
        by_folio[(r["company"], r["folio_no"].replace(" ", ""))].append(idx)
for ids in by_bo.values():
    for j in ids[1:]:
        union(ids[0], j); method_of[j] = "bo_exact"
    if len(ids) > 1:
        method_of[ids[0]] = "bo_exact"
for ids in by_folio.values():
    for j in ids[1:]:
        union(ids[0], j); method_of[j] = "folio_company"
    if len(ids) > 1:
        method_of[ids[0]] = "folio_company"
div["_root"] = [find(i) for i in div.index]
roots = {r: f"H{n:06d}" for n, r in enumerate(sorted(set(div["_root"])), 1)}
div["holder_id"] = div["_root"].map(roots)
div["match_method"] = [method_of.get(i, "unmatched") for i in div.index]
div["match_score"] = [1.0 if m in ("bo_exact", "folio_company") else None for m in div["match_method"]]

# ---------- review queue: same normalised name, different holders, no second field to confirm ----------
review = []
blocks = defaultdict(list)
for idx, r in div[div["holder_name_norm"].str.split().str.len() >= 3].iterrows():
    blocks[" ".join(sorted(r["holder_name_norm"].split()))].append(idx)
rn = 0
for k, ids in blocks.items():
    hs = {}
    for i in ids:
        hs.setdefault(div.at[i, "holder_id"], i)
    if len(hs) < 2:
        continue
    for (ha, ia), (hb, ib) in itertools.combinations(list(hs.items())[:6], 2):
        ba, bb = div.at[ia, "bo_id"], div.at[ib, "bo_id"]
        if ba and bb and ba != bb:
            continue  # never merge two different valid BO IDs
        rn += 1
        review.append(dict(review_id=f"R{rn:06d}", dividend_id_a=div.at[ia, "dividend_id"],
                           dividend_id_b=div.at[ib, "dividend_id"], holder_id_a=ha, holder_id_b=hb,
                           name_a=div.at[ia, "holder_name_raw"], name_b=div.at[ib, "holder_name_raw"],
                           evidence="same normalised name; no father/address in list to confirm; "
                                    + ("different companies" if div.at[ia, "company"] != div.at[ib, "company"] else "same company"),
                           score=100.0, decision=""))
review_df = pd.DataFrame(review)
REVIEW_TOTAL = len(review_df)
if REVIEW_TOTAL > 5000:
    amt = div.set_index("dividend_id")["net_amount"].fillna(0)
    review_df["_p"] = review_df["dividend_id_a"].map(amt) + review_df["dividend_id_b"].map(amt)
    review_df = review_df.sort_values("_p", ascending=False).head(5000).drop(columns="_p")

# ---------- settled flag ----------
sd = pd.DataFrame(settled)
if len(sd):
    s_bo = {(r.bo_id, round(float(r.amount_or_shares or 0), 2)) for r in sd.itertuples() if r.bo_id}
    s_fo = {(r.company, str(r.folio_no).replace(" ", ""), round(float(r.amount_or_shares or 0), 2)) for r in sd.itertuples() if r.folio_no}
    def is_settled(r):
        a = round(float(r["net_amount"] if pd.notna(r["net_amount"]) else (r["shares"] if pd.notna(r["shares"]) else 0)), 2)
        return int((r["bo_id"], a) in s_bo or (r["company"], r["folio_no"].replace(" ", ""), a) in s_fo)
    div["settled_flag"] = div.apply(is_settled, axis=1)

# ---------- holders ----------
def agg(g):
    unver = g["issues"].str.contains("implausible_amount|name_looks_like_address|unverified_int_amount")
    cash = g.loc[(g["dividend_type"] != "stock") & ~unver, "net_amount"].sum()
    cash_unver = g.loc[(g["dividend_type"] != "stock") & unver, "net_amount"].sum()
    return pd.Series(dict(
        bo_id=next((b for b in g["bo_id"] if b), ""),
        folio_numbers="; ".join(sorted({f"{c}:{f}" for c, f in zip(g["company"], g["folio_no"]) if f})),
        name_canonical=g["holder_name_raw"].mode().iat[0],
        name_variants="; ".join(sorted(set(g["holder_name_raw"]))[:8]),
        father_or_spouse="", companies_count=g["company"].nunique(), items_count=len(g),
        total_cash_bdt=round(cash, 2), total_cash_unverified_bdt=round(cash_unver, 2), total_shares=g["shares"].sum(),
        first_year=g["year_start"].min(), last_year=g["year_start"].max(), status="new",
        settled_all=int(g["settled_flag"].all()), institution=int(g["issues"].str.contains("institution").any())))
holders = div.groupby("holder_id").apply(agg, include_groups=False).reset_index()
holders = holders.sort_values("total_cash_bdt", ascending=False)

# ---------- outputs ----------
div_out = div.drop(columns=["_root"])
div_out.to_csv(OUT / "dividends_all.csv", index=False)
hold_csv = holders.drop(columns=["settled_all", "institution"])
hold_csv.to_csv(OUT / "holders.csv", index=False)
hi = holders[["holder_id", "bo_id", "name_canonical", "companies_count", "total_cash_bdt"]].merge(
    div_out.drop(columns=["bo_id", "raw_row_json"]), on="holder_id")
hi.to_csv(OUT / "holder_items.csv", index=False)
top = holders[((holders["total_cash_bdt"] >= 50000) | (holders["companies_count"] >= 3))
              & (holders["settled_all"] == 0) & (holders["institution"] == 0)].drop(columns=["settled_all", "institution"])
top.to_csv(OUT / "top_holders.csv", index=False)
review_df.to_csv(OUT / "review_queue.csv", index=False)
docs_df = pd.DataFrame(docs)
_e = pd.to_numeric(docs_df["extracted_total"], errors="coerce"); _s = pd.to_numeric(docs_df["stated_total"], errors="coerce")
docs_df["coverage_pct"] = ((_e / _s) * 100).where(_s > 0).round(1)
docs_df.to_csv(OUT / "documents_report.csv", index=False)
docs_df[docs_df["status"].isin(["failed", "partial"])].to_csv(OUT / "failed_or_partial.csv", index=False)

(OUT / "unclaimed.sqlite").unlink(missing_ok=True)
con = sqlite3.connect(OUT / "unclaimed.sqlite")
schema = (ROOT / "schema.sql").read_text().replace("raw_row_json      TEXT                -- the original extracted cells, for audit",
                                                    "raw_row_json      TEXT,\n  issues            TEXT").replace("total_cash_bdt    REAL,", "total_cash_bdt    REAL,\n  total_cash_unverified_bdt REAL,")
con.executescript(schema)
for tname, df in (("documents", docs_df), ("dividends", div_out), ("holders", hold_csv), ("review_queue", review_df),
                  ("settled_claims", sd)):
    if len(df):
        df.to_sql(tname, con, if_exists="append", index=False)
con.commit(); con.close()

# ---------- summary ----------
pc = docs_df["status"].value_counts().to_dict()
cash = div_out[(div_out["dividend_type"] != "stock") & ~div_out["issues"].str.contains("implausible_amount|name_looks_like_address|unverified_int_amount")]
lines = ["# Unclaimed dividends - summary", "", f"Documents: {pc}", f"Dividend rows: {len(div_out):,}",
         f"Holders: {len(holders):,}", f"Total cash BDT (all rows): {cash['net_amount'].sum():,.2f}",
         f"Total shares (stock rows): {div_out['shares'].sum():,.0f}",
         f"Holders in 2+ companies: {(holders['companies_count'] >= 2).sum():,}",
         f"Match methods: {Counter(div_out['match_method'])}", f"Review queue: {len(review_df):,} written (of {REVIEW_TOTAL:,} candidate pairs; highest-value 5,000 kept)", "",
         "## Top 10 companies by cash BDT", cash.groupby("company")["net_amount"].sum().nlargest(10).round(2).to_string(),
         "", "## Cash BDT by year", cash.groupby("year_start")["net_amount"].sum().round(2).to_string()]
(OUT / "summary.md").write_text("\n".join(lines))
print("\n".join(lines[:12]))
