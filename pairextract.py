#!/usr/bin/env python3
"""Records split across lines (identity line: SL YEAR WARRANT NAME ...; amount line: [FOLIO|BO] shares rate gross tax net).
Pairs them in page order; only accepts pages where both counts agree."""
import csv, re, subprocess, sys, json
from pathlib import Path
import extract as X

ROOT = Path(__file__).resolve().parent
man = {r["id"]: r for r in csv.DictReader(open(ROOT / "manifest.csv", encoding="utf-8-sig"))}
IDL = re.compile(r"^\s*(\d{1,6})\s+((?:19|20)\d\d)\s+(\d{5,9})\s+(\S.*?)(?:\s{3,}.*)?$")
TOK = re.compile(r"^\(?-?\d[\d,]*(\.\d+)?\)?$")


def amount_line(ln):
    toks = ln.split()
    if not toks:
        return None
    j = len(toks)
    while j > 0 and TOK.match(toks[j - 1]):
        j -= 1
    run = toks[j:]
    if len(run) < 3 or len(run) > 8:
        return None
    _, _, trip = X.repair_amounts(run)
    if not trip:
        return None
    ident = toks[0] if j > 0 and re.fullmatch(r"\d{4,16}", toks[0]) else ""
    return ident, trip


def run(doc, dtype="cash"):
    p = ROOT / man[doc]["local_path"]
    txt = subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True, errors="replace").stdout
    rows, bad_pages = [], 0
    for pno, page in enumerate(txt.split("\f"), 1):
        ids, amts = [], []
        for ln in page.split("\n"):
            m = IDL.match(ln)
            if m:
                ids.append(m.groups())
                continue
            a = amount_line(ln)
            if a:
                amts.append(a)
        if not ids and not amts:
            continue
        if len(ids) != len(amts):
            bad_pages += 1
            continue
        for rn, ((sl, yr, wr, name), (ident, (g, t, n))) in enumerate(zip(ids, amts), 1):
            bo = ident if re.fullmatch(r"\d{16}", ident) else ""
            rows.append(dict(doc_id=doc, page=pno, row_on_page=rn, dividend_year=yr, dividend_type=dtype, warrant_no=wr,
                             folio_or_bo_raw=ident, bo_id=bo, folio_no="" if bo else ident, holder_name_raw=name.strip(),
                             shares="", gross_amount=g, tax_amount=t, net_amount=n, issues="paired_lines", line=f"{sl} {yr} {wr} {name[:60]} | {ident} {g} {t} {n}"))
    return rows, bad_pages


if __name__ == "__main__":
    out = ROOT / "extracted" / "raw_pair"; out.mkdir(exist_ok=True)
    for d in sys.argv[1].split(","):
        rows, bad = run(d)
        with open(out / f"{d}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=X.FIELDS, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
        print(d, "rows", len(rows), "net", round(sum(r["net_amount"] for r in rows), 2), "pages with count mismatch", bad)
