#!/usr/bin/env python3
"""Column-position fallback: find the Net/Bonus column from each page's header and read the number under it.
Used for documents where the generic line parser does not reproduce the printed total."""
import csv, json, re, subprocess, sys, os
from pathlib import Path
from multiprocessing import Pool
import extract as X

ROOT = Path(__file__).resolve().parent
RAWC = ROOT / "extracted" / "raw_col"; RAWC.mkdir(parents=True, exist_ok=True)
man = {r["id"]: r for r in csv.DictReader(open(ROOT / "manifest.csv", encoding="utf-8-sig"))}
KW = re.compile(r"^(sl|serial|folio|bo|boid|bo_id|name|net\w*|gross\w*|tax\w*|amount|shares?|bonus\w*|dividend|holder\w*|address\w*|warrant|payable|total|stock|cash|father\w*|year|tk|taka|no|fraction\w*)$")
NUM = re.compile(r"^\(?-?\d[\d,]*(\.\d+)?\)?$")
CASH_PRI = [r"^net", r"^payable", r"^amount", r"^dividend"]
STOCK_PRI = [r"^bonus", r"^shares?$", r"^stock", r"^no$"]


def norm(t):
    return t.strip("().:#,-_/*").lower()


def find_header(lines, pri):
    """Return (start, end) character span of the preferred amount column, or None."""
    for i, ln in enumerate(lines):
        toks = [(m.start(), m.end(), norm(m.group())) for m in re.finditer(r"\S+", ln)]
        kws = [t for t in toks if KW.match(t[2])]
        if len(kws) < 3 or sum(1 for t in toks if NUM.match(t[2])) > 2 or re.search(r"\b\d{16}\b", ln):
            continue
        zone = list(kws)
        for nxt in lines[i + 1:i + 3]:   # headers wrapped over 2-3 lines
            tk2 = [(m.start(), m.end(), norm(m.group())) for m in re.finditer(r"\S+", nxt)]
            if tk2 and all(KW.match(t[2]) or not t[2] for t in tk2) and not any(NUM.match(t[2]) for t in tk2):
                zone += [t for t in tk2 if KW.match(t[2])]
        for p in pri:
            hit = [t for t in zone if re.match(p, t[2])]
            if hit:
                a, b = hit[0][0], hit[0][1]
                # extend over adjacent keyword words on the same line (e.g. "Net Dividend Amount")
                for t in sorted(zone):
                    if t[0] >= b and t[0] - b <= 2 and t[2] in ("dividend", "amount", "payable", "tk", "taka", "divident"):
                        b = t[1]
                return (a, b)
        return None
    return None


def pick_number(ln, span):
    a, b = span
    best, bd = None, 10 ** 9
    for m in re.finditer(r"\S+", ln):
        t = m.group()
        if not NUM.match(t):
            continue
        s, e = m.start(), m.end()
        ov = min(e, b + 6) - max(s, a - 6)
        d = abs((s + e) / 2 - (a + b) / 2)
        if ov > 0 and d < bd:
            best, bd = t, d
    return best


def process(doc_id):
    out = RAWC / f"{doc_id}.csv"
    p = ROOT / man[doc_id]["local_path"]
    try:
        npg = len(__import__("pypdf").PdfReader(str(p)).pages)
    except Exception:
        npg = 0
    dtype = X.dtype_of(man[doc_id]["title"])
    ctx = {"year": "", "dtype": dtype}
    rows, hdr, first = [], None, True
    for a in range(1, max(npg, 1) + 1, 100):
        b = min(a + 99, npg)
        txt = subprocess.run(["pdftotext", "-layout", "-f", str(a), "-l", str(b), str(p), "-"],
                             capture_output=True, text=True, errors="replace").stdout.translate(X.BN)
        pages = txt.split("\f")
        for i in range(b - a + 1):
            pno = a + i
            lines = (pages[i] if i < len(pages) else "").split("\n")
            if len("".join(lines).strip()) < 40:
                continue
            if first:
                head = " ".join(l for l in lines[:8] if not re.search(r"\b\d{16}\b", l))
                ctx["dtype"] = X.dtype_of(man[doc_id]["title"] + " " + head, ctx["dtype"])
                my = X.YEAR.search(man[doc_id]["title"] + " " + head)
                if my:
                    ctx["year"] = my.group(0)
                first = False
            pri = STOCK_PRI if ctx["dtype"] in ("stock", "right") else CASH_PRI
            h = find_header(lines, pri)
            if h:
                hdr = h
            rn = 0
            for ln in lines:
                if not ln.strip():
                    continue
                low = ln.lower()
                if "total" in low and not re.search(r"\b\d{16}\b", ln):
                    continue
                if len(ln.split()) <= 14 and not re.search(r"\b\d{16}\b", ln) and re.search(r"year|dividend|fy|agm", low):
                    my = X.YEAR.search(ln)
                    if my:
                        ctx["year"] = my.group(0)
                if hdr is None:
                    continue
                r = X.parse_line(ln.strip(), ctx, need_amount=False)
                bo = re.search(r"\b(\d{16})\b", ln)
                if r is None and not bo:
                    continue
                numtok = pick_number(ln, hdr)
                if numtok is None:
                    continue
                val = X.num(numtok)
                if r is None:   # name not on this line: keep BO + amount, flag
                    r = dict(dividend_year=ctx["year"], dividend_type=ctx["dtype"], warrant_no="",
                             folio_or_bo_raw=bo.group(1), bo_id=bo.group(1), folio_no="", holder_name_raw="",
                             shares=None, gross_amount=None, tax_amount=None, net_amount=None,
                             issues="name_missing", line=ln.strip()[:300])
                is_stock = ctx["dtype"] in ("stock", "right")
                r["shares"] = val if is_stock else None
                r["net_amount"] = None if is_stock else val
                r["gross_amount"] = r["tax_amount"] = None
                r["issues"] = ";".join(x for x in (r.get("issues", "").replace("stock_ambiguous_numbers", "").replace("amount_repaired", "").replace("gross_tax_net_mismatch", "").split(";") + ["col_amount"]) if x)
                rn += 1
                r.update(doc_id=doc_id, page=pno, row_on_page=rn)
                rows.append(r)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=X.FIELDS, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    return doc_id, len(rows)


YCOL = re.compile(r"(?:(Bonus|Right|Cash|Stock|Fraction)[\s_-]*)?((?:19|20)\d\d(?:\s*[-/]\s*\d{2,4})?)", re.I)


def find_year_cols(lines):
    for ln in lines[:40]:
        if re.search(r"\b\d{16}\b", ln):
            continue
        ms = [m for m in YCOL.finditer(ln)]
        if len(ms) >= 2 and sum(1 for t in ln.split() if NUM.match(t) and len(t.replace(",", "")) > 4) == 0:
            cols = []
            for m in ms:
                kind = (m.group(1) or "").lower()
                cols.append((re.sub(r"\s+", "", m.group(2)), kind, (m.start() + m.end()) / 2.0))
            return cols
    return None


def process_years(doc_id):
    p = ROOT / man[doc_id]["local_path"]
    try:
        npg = len(__import__("pypdf").PdfReader(str(p)).pages)
    except Exception:
        npg = 0
    base = X.dtype_of(man[doc_id]["title"])
    rows, cols, seen = [], None, False
    for a in range(1, max(npg, 1) + 1, 100):
        b = min(a + 99, npg)
        txt = subprocess.run(["pdftotext", "-layout", "-f", str(a), "-l", str(b), str(p), "-"],
                             capture_output=True, text=True, errors="replace").stdout.translate(X.BN)
        pages = txt.split("\f")
        for i in range(b - a + 1):
            lines = (pages[i] if i < len(pages) else "").split("\n")
            c2 = find_year_cols(lines)
            if c2:
                cols, seen = c2, True
            if not cols:
                continue
            rn = 0
            for ln in lines:
                if not ln.strip() or ("total" in ln.lower() and not re.search(r"\b\d{16}\b", ln)):
                    continue
                kind0 = base
                r = X.parse_line(ln.strip(), {"year": "", "dtype": kind0}, need_amount=False)
                bo = re.search(r"\b(\d{16})\b", ln)
                if r is None and not bo:
                    continue
                cells = []
                for m in re.finditer(r"\S+", ln):
                    tk = m.group()
                    if not NUM.match(tk):
                        continue
                    ctr = (m.start() + m.end()) / 2.0
                    j = min(range(len(cols)), key=lambda k: abs(cols[k][2] - ctr))
                    if abs(cols[j][2] - ctr) <= 9 and "." in tk or (abs(cols[j][2] - ctr) <= 9 and kind0 in ("stock", "right")):
                        cells.append((j, X.num(tk)))
                for j, v in cells:
                    if not v:
                        continue
                    label, kind, _ = cols[j]
                    dt = {"bonus": "stock", "right": "right", "stock": "stock", "fraction": "fraction", "cash": "cash"}.get(kind, kind0)
                    rr = dict(r) if r else dict(folio_or_bo_raw=bo.group(1), bo_id=bo.group(1), folio_no="", holder_name_raw="",
                                                warrant_no="", shares=None, gross_amount=None, tax_amount=None, net_amount=None, issues="name_missing")
                    rr.update(dividend_year=label, dividend_type=dt, line=ln.strip()[:300])
                    rr["issues"] = ";".join(x for x in (rr.get("issues", "").replace("stock_ambiguous_numbers", "").replace("amount_repaired", "").replace("gross_tax_net_mismatch", "").split(";") + ["year_column"]) if x)
                    rr["shares"] = v if dt in ("stock", "right") else None
                    rr["net_amount"] = None if dt in ("stock", "right") else v
                    rr["gross_amount"] = rr["tax_amount"] = None
                    rn += 1
                    rr.update(doc_id=doc_id, page=a + i, row_on_page=rn)
                    rows.append(rr)
    if not seen:
        return doc_id, -1
    outd = ROOT / "extracted" / "raw_years"; outd.mkdir(exist_ok=True)
    with open(outd / f"{doc_id}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=X.FIELDS, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    return doc_id, len(rows)


if __name__ == "__main__":
    ids = sys.argv[1].split(",")
    fn = process_years if len(sys.argv) > 2 and sys.argv[2] == "years" else process
    with Pool(os.cpu_count() or 2) as pool:
        for d, n in pool.imap_unordered(fn, ids):
            print(d, n, flush=True)
