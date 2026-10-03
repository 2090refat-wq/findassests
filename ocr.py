#!/usr/bin/env python3
"""OCR pages without a text layer (300 dpi, tesseract eng+ben); rows flagged 'ocr' for human checking."""
import csv, json, re, subprocess, tempfile, sys
from pathlib import Path
import pdfplumber
import extract as X

ROOT = Path(__file__).resolve().parent
tri = {r["id"]: r for r in csv.DictReader(open(ROOT / "extracted/triage.csv", encoding="utf-8"))}
man = {r["id"]: r for r in csv.DictReader(open(ROOT / "manifest.csv", encoding="utf-8-sig"))}


def ocr_page(path, pno):
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-r", "300", "-f", str(pno), "-l", str(pno), "-png", str(path), f"{td}/p"], check=True)
        img = next(Path(td).glob("p*.png"))
        return subprocess.run(["tesseract", str(img), "-", "-l", "eng+ben", "--psm", "6"], capture_output=True,
                              text=True, errors="replace").stdout


jobs = {}
for f in (ROOT / "extracted/stats").glob("*.json"):
    s = json.loads(f.read_text())
    if s["textless_pages"] > 0 and s["role"] != "summary":
        jobs[s["doc_id"]] = None
for i, r in tri.items():
    if r["pdf_kind"] == "scanned" and r["role"] in ("list", "settled_claims", "check_relevance"):
        jobs[i] = None
for doc_id in sorted(jobs):
    path = ROOT / man[doc_id]["local_path"]
    sp = ROOT / "extracted/stats" / f"{doc_id}.json"
    rp = ROOT / "extracted/raw" / f"{doc_id}.csv"
    st = json.loads(sp.read_text()) if sp.exists() else dict(doc_id=doc_id, role=tri[doc_id]["role"], rows=0, pages=0,
                                                              textless_pages=0, unparsed_idlike=0, stated_total=None,
                                                              extracted_total=0.0, extracted_shares=0.0, error="", dtype="", year="")
    if st.get("ocr"):
        continue
    rows = list(csv.DictReader(open(rp, encoding="utf-8"))) if rp.exists() else []
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        todo = [p for p in range(1, n + 1) if len((pdf.pages[p - 1].extract_text() or "").strip()) < 40]
    ctx = {"year": st.get("year", ""), "dtype": X.dtype_of(man[doc_id]["title"], "cash")}
    got = 0
    for pno in todo:
        text = ocr_page(path, pno).translate(X.BN)
        m = X.YEAR.search(text[:400])
        if m and not ctx["year"]:
            ctx["year"] = m.group(0)
        ctx["dtype"] = X.dtype_of(man[doc_id]["title"] + " " + text[:300], ctx["dtype"])
        rn = 0
        for ln in text.split("\n"):
            r = X.parse_line(ln.strip(), ctx)
            if r:
                rn += 1
                r["issues"] = (r["issues"] + ";ocr").strip(";")
                r.update(doc_id=doc_id, page=pno, row_on_page=rn)
                rows.append(r); got += 1
    with open(rp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=X.FIELDS, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    st.update(ocr=True, ocr_pages=len(todo), ocr_rows=got, textless_pages=0, rows=len(rows), pages=st.get("pages") or n,
              extracted_total=round(sum(float(r["net_amount"] or 0) for r in rows), 2))
    sp.write_text(json.dumps(st))
    print(doc_id, "ocr pages", len(todo), "rows", got, flush=True)
