#!/usr/bin/env python3
"""Generic line-based extractor for unclaimed-dividend PDFs. Resumable; one raw CSV + stats JSON per document."""
import csv, json, re, sys, os
from pathlib import Path
from multiprocessing import Pool
import pdfplumber

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "extracted" / "raw"; STAT = ROOT / "extracted" / "stats"
RAW.mkdir(parents=True, exist_ok=True); STAT.mkdir(parents=True, exist_ok=True)
BN = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
BO = re.compile(r"^\d{16}$")
MONEY = re.compile(r"^\(?-?\d[\d,]*\.\d+\)?$")
INT = re.compile(r"^\d[\d,]*$")
YEAR = re.compile(r"\b((?:19|20)\d\d)(?:\s*[-–/]\s*((?:19|20)?\d\d))?\b")
FIELDS = ["doc_id", "page", "row_on_page", "dividend_year", "dividend_type", "warrant_no", "folio_or_bo_raw",
          "bo_id", "folio_no", "holder_name_raw", "shares", "gross_amount", "tax_amount", "net_amount",
          "issues", "line"]


def num(t):
    t = t.replace(",", "").strip("()")
    try:
        return float(t)
    except ValueError:
        return None



import itertools
def repair_amounts(tail):
    """Find gross, tax, net among the numeric tokens after the name by arithmetic (gross - tax [- extra] = net), whole numbers included.
    Returns (decimals, repaired_flag, (gross, tax, net) | None)."""
    toks = []
    for t in tail:
        if toks and (t.startswith(",") or t.startswith(".")) and re.match(r"^\d", toks[-1]):
            toks[-1] += t
        else:
            toks.append(t)
    vals = [num(t) for t in toks if MONEY.match(t) or INT.match(t)]
    vals = [v for v in vals if v is not None]
    for w in range(len(vals) - 3, -1, -1):          # size-3 windows, last first
        g, tx, n = vals[w], vals[w + 1], vals[w + 2]
        if g > 0 and tx > 0 and n > 0 and abs(g - tx - n) <= 0.05:
            return [v for v in vals if True], False, (g, tx, n)
    for w in range(len(vals) - 4, -1, -1):          # size-4: gross - tax - extra = net
        g, t1, t2, n = vals[w], vals[w + 1], vals[w + 2], vals[w + 3]
        if g > 0 and n > 0 and t1 >= 0 and t2 >= 0 and (t1 > 0 or t2 > 0) and abs(g - t1 - t2 - n) <= 0.05:
            return vals, False, (g, t1 + t2, n)
    dec = [num(t) for t in toks if MONEY.match(t)]
    return dec, False, None


def merge_two_line(lines):
    """Records printed over two lines: 'SL YEAR ID ... gross' then 'NAME ... shares tax net'. Merge to 'SL YEAR ID NAME ... shares tax net'."""
    out, i = [], 0
    while i < len(lines):
        a = lines[i]
        ta = a.split()
        if (len(ta) >= 3 and INT.match(ta[0]) and re.match(r"^(19|20)\d\d$", ta[1]) and re.match(r"^\d{4,}$", ta[2])
                and sum(1 for t in ta[3:] if MONEY.match(t)) <= 1):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                b = lines[j].strip()
                tb = b.split()
                if tb and re.match(r"^[A-Za-z]", tb[0]) and any(MONEY.match(t) for t in tb) and not any(BO.match(t) for t in tb):
                    out.append(" ".join(ta[:3]) + " " + b)
                    i = j + 1
                    continue
        out.append(a)
        i += 1
    return out

def dtype_of(text, default="cash"):
    s = text.lower()
    if re.search(r"ipo.{0,20}refund|refund", s): return "ipo_refund"
    if re.search(r"right\s*share|rights?\s*issue", s): return "right"
    if re.search(r"fraction", s): return "fraction"
    if re.search(r"subscription", s): return "other"
    if re.search(r"stock div|bonus|stock", s) and not re.search(r"cash div", s): return "stock"
    if re.search(r"cash|dividend|unclaim|unpaid", s): return "cash" if default is None else default
    return default


def parse_line(line, ctx, need_amount=True):
    """Return (row dict | None). ctx has year, dtype."""
    toks = line.split()
    if len(toks) < 3:
        return None
    # locate BO
    bo_i = next((i for i, t in enumerate(toks) if BO.match(t)), None)
    # layout with the name printed BEFORE the BO ID: move the BO in front of the name so one code path handles both
    if (bo_i is not None and bo_i + 1 < len(toks) and (INT.match(toks[bo_i + 1]) or MONEY.match(toks[bo_i + 1]))
            and any(re.search(r"[A-Za-z\u0980-\u09FF]", t) and not re.search(r"\d", t) for t in toks[:bo_i])):
        a = next((i for i in range(bo_i) if re.search(r"[A-Za-z\u0980-\u09FF]", toks[i]) and not re.search(r"\d", toks[i])), None)
        if a is not None:
            jn = a
            while jn < bo_i and not re.search(r"\d", toks[jn]):
                jn += 1
            toks = toks[:a] + [toks[bo_i]] + toks[a:jn] + toks[jn:bo_i] + toks[bo_i + 1:]
            bo_i = a
    # first alphabetic name token (no digits, has a letter), after any id region
    start = (bo_i + 1) if bo_i is not None else 0
    name_i = None
    for i in range(start, len(toks)):
        t = toks[i]
        if re.search(r"[A-Za-zঀ-৿]", t) and not re.search(r"\d", t):
            name_i = i
            break
    if name_i is None:
        return None
    pre = toks[:name_i] if bo_i is None else toks[:bo_i] + toks[bo_i + 1:name_i]
    # amounts: numeric tokens after name tokens
    j = name_i
    while j < len(toks) and not (MONEY.match(toks[j]) or (INT.match(toks[j]) and False)):
        j += 1
    # name is non-numeric tokens from name_i until first all-numeric token
    k = name_i
    while k < len(toks) and not (MONEY.match(toks[k]) or INT.match(toks[k])):
        k += 1
    name = " ".join(toks[name_i:k])
    tail = toks[k:]
    nums = [t for t in tail if MONEY.match(t) or INT.match(t)]
    dec = [t for t in nums if MONEY.match(t)]
    ints = [t for t in nums if INT.match(t)]
    issues = []
    ident_raw = " ".join(([toks[bo_i]] if bo_i is not None else []) + [t for t in pre])
    if bo_i is None and not pre:
        return None
    year = ctx["year"]
    for t in pre + toks[name_i:k]:
        m = re.match(r"^((?:19|20)\d\d)[-–/]((?:19|20)?\d\d)$", t)
        if m:
            year = t
    pre2 = [t for t in pre if t != year]
    # drop leading serial number
    if pre2 and INT.match(pre2[0]) and (len(pre2) >= 2 or bo_i is not None) and len(pre2[0]) <= 6:
        pre2 = pre2[1:]
    if bo_i is None and not pre2:
        return None
    bo = toks[bo_i] if bo_i is not None else ""
    folio = " ".join(pre2) if bo_i is None else ""
    warrant = ""
    if bo_i is not None and pre2:
        warrant = " ".join(pre2)
    shares = gross = tax = net = None
    if ctx["dtype"] in ("stock", "right"):
        run = []
        for t in toks[k:]:
            if INT.match(t) or MONEY.match(t):
                run.append(t)
            else:
                break
        if len(run) == 1 and INT.match(run[0]):
            shares = num(run[0])
        elif run:
            shares = -1.0
            issues.append("stock_ambiguous_numbers")
    else:
        decs, repaired, trip = repair_amounts(tail)
        if trip:
            gross, tax, net = trip
            if repaired:
                issues.append("amount_repaired")
        elif decs:
            net = decs[-1]
            if len(decs) >= 3:
                issues.append("gross_tax_net_mismatch")
        elif need_amount:
            return None
    if shares == -1.0:
        shares = None
    elif shares is None and net is None and need_amount:
        return None
    if len(name) < 2:
        return None
    return dict(dividend_year=year, dividend_type=ctx["dtype"], warrant_no=warrant, folio_or_bo_raw=ident_raw,
                bo_id=bo, folio_no=folio, holder_name_raw=name, shares=shares, gross_amount=gross,
                tax_amount=tax, net_amount=net, issues=";".join(issues), line=line[:300])



import subprocess
def page_source(pdf, path, npages, use_poppler):
    """Yield page text. Big PDFs go through poppler pdftotext in 100-page chunks (pdfplumber exhausts memory)."""
    if not use_poppler:
        for p in pdf.pages:
            yield p.extract_text() or ""
        return
    for a in range(1, npages + 1, 100):
        b = min(a + 99, npages)
        out = subprocess.run(["pdftotext", "-layout", "-f", str(a), "-l", str(b), str(path), "-"],
                             capture_output=True, text=True, errors="replace").stdout
        pages = out.split("\f")
        for i in range(b - a + 1):
            yield pages[i] if i < len(pages) else ""

def process(job):
    doc_id, path, role, title = job
    sp = STAT / f"{doc_id}.json"
    if sp.exists():
        return doc_id
    stats = dict(doc_id=doc_id, role=role, rows=0, pages=0, textless_pages=0, unparsed_idlike=0,
                 stated_total=None, extracted_total=0.0, extracted_shares=0.0, error="", dtype="", year="")
    rows = []
    try:
        with pdfplumber.open(path) as pdf:
            stats["pages"] = len(pdf.pages)
            npages = len(pdf.pages)
            use_poppler = True
            ctx = {"year": "", "dtype": dtype_of(title)}
            first = True
            totals = []
            for pno, page in enumerate(page_source(pdf, path, npages, use_poppler), 1):
                text = page.translate(BN)
                if len(text.strip()) < 40:
                    stats["textless_pages"] += 1
                    continue
                lines = merge_two_line(text.split("\n"))
                if first:
                    head = " ".join(l for l in lines[:8] if not any(BO.match(t) for t in l.split()) and not MONEY.search(l))
                    ctx["dtype"] = dtype_of(title + " " + head, ctx["dtype"])
                    my = YEAR.search(title + " " + head)
                    if my:
                        ctx["year"] = my.group(0)
                    first = False
                rn = 0
                for ln in lines:
                    r = parse_line(ln, ctx)
                    if r:
                        rn += 1
                        r.update(doc_id=doc_id, page=pno, row_on_page=rn)
                        rows.append(r)
                        continue
                    low = ln.lower()
                    if "total" in low:
                        ms = [num(t) for t in ln.split() if MONEY.match(t) or INT.match(t)]
                        ms = [x for x in ms if x is not None]
                        if ms:
                            totals.append((("grand" in low), ms[-1]))
                        continue
                    # heading lines update context
                    if len(ln.split()) <= 14 and not any(BO.match(t) for t in ln.split()):
                        if re.search(r"year|dividend|fy|agm", low):
                            my = YEAR.search(ln)
                            if my:
                                ctx["year"] = my.group(0)
                        nd = dtype_of(ln, None) if re.search(r"stock div|bonus|fraction|right share|refund|cash div|subscription", low) else None
                        if nd:
                            ctx["dtype"] = nd
                    if any(BO.match(t) for t in ln.split()):
                        stats["unparsed_idlike"] += 1
            if totals:
                g = [v for isg, v in totals if isg]
                stats["stated_total"] = (g[-1] if g else max(v for _, v in totals))
                stats["totals"] = [v for _, v in totals][:300]
    except Exception as e:
        stats["error"] = f"{type(e).__name__}: {e}"[:200]
    stats["rows"] = len(rows)
    stats["extracted_total"] = round(sum(r["net_amount"] or 0 for r in rows), 2)
    stats["extracted_shares"] = round(sum(r["shares"] or 0 for r in rows), 2)
    stats["dtype"] = ctx["dtype"] if rows is not None else ""
    with open(RAW / f"{doc_id}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    sp.write_text(json.dumps(stats))
    return doc_id


if __name__ == "__main__":
    tri = list(csv.DictReader(open(ROOT / "extracted" / "triage.csv", encoding="utf-8")))
    man = {r["id"]: r for r in csv.DictReader(open(ROOT / "manifest.csv", encoding="utf-8-sig"))}
    only = set(sys.argv[1].split(",")) if len(sys.argv) > 1 else None
    jobs = [(t["id"], t["path"], t["role"], man[t["id"]]["title"]) for t in tri
            if t["pdf_kind"] in ("text", "mixed") and t["role"] in ("list", "settled_claims", "check_relevance")
            and (not only or t["id"] in only)]
    print(len(jobs), "documents to extract", flush=True)
    with Pool(os.cpu_count() or 2) as p:
        for i, d in enumerate(p.imap_unordered(process, jobs), 1):
            if i % 20 == 0:
                print(i, "done", flush=True)
    print("ALL DONE", flush=True)
