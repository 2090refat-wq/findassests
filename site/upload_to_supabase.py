#!/usr/bin/env python3
"""Load output/unclaimed.sqlite into the Supabase lookup project via the temporary `ingest` edge function.
Needs SUPABASE_REF and UPLOAD_TOKEN env vars. Contains personal data in flight: run only against your own project.
Cross-document duplicates (same company, id, year, type, amounts in several PDFs) are merged: the lowest doc_id is kept."""
import json, os, sqlite3, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF, TOKEN = os.environ["SUPABASE_REF"], os.environ["UPLOAD_TOKEN"]
URL = f"https://{REF}.supabase.co/functions/v1/ingest"
BATCH = int(os.environ.get("BATCH", "4000"))
LIMIT = int(os.environ.get("LIMIT", "0"))
BAD = ("implausible_amount", "name_looks_like_address", "unverified_int_amount", "unreadable_amount", "name_missing")


def post(payload, tries=4):
    data = json.dumps(payload).encode()
    for k in range(tries):
        try:
            req = urllib.request.Request(URL, data=data, headers={"content-type": "application/json", "x-upload-token": TOKEN})
            return json.load(urllib.request.urlopen(req, timeout=120))
        except Exception as e:
            err = e
            time.sleep(2 ** (k + 1))
    raise SystemExit(f"upload failed: {err}")


db = sqlite3.connect(ROOT / "output" / "unclaimed.sqlite")
docs = [dict(doc_id=r[0], company=r[1], title=r[2], source_url=r[3], status=r[4])
        for r in db.execute("select doc_id, company, title, source_url, status from documents")]
print(post({"docs": docs}))

sql = """
with k as (
  select d.*, case when bo_id != '' then bo_id else 'F' || replace(replace(folio_no,' ',''),'-','') end as idk,
         min(doc_id) over (partition by company, case when bo_id != '' then bo_id else 'F' || replace(replace(folio_no,' ',''),'-','') end,
                           coalesce(dividend_year,''), coalesce(dividend_type,''), coalesce(net_amount,-1), coalesce(shares,-1)) as keep_doc
  from dividends d where bo_id != '' or folio_no != ''
)
select bo_id, company, upper(replace(replace(folio_no,' ',''),'-','')) as folio_key, folio_no, dividend_year, year_start,
       dividend_type, net_amount, shares, holder_name_raw, doc_id, page, issues
from k where doc_id = keep_doc
""" + (f" limit {LIMIT}" if LIMIT else "")
sent, t0, buf = 0, time.time(), []


def flush():
    global sent, buf
    if not buf:
        return
    r = post({"items": buf})
    if not r.get("ok"):
        raise SystemExit(r)
    sent += len(buf); buf = []
    print(f"{sent:,} rows  ({time.time()-t0:.0f}s)  db total {r['total_items']:,}", flush=True)


for r in db.execute(sql):
    iss = r[12] or ""
    buf.append(dict(bo_id=r[0], company=r[1], folio_key=r[2] if r[0] == "" else "", folio_no=r[3] if r[0] == "" else "",
                    year=r[4], year_start=r[5], dtype=r[6], net=r[7], shares=r[8], name=r[9], doc_id=r[10], page=r[11],
                    verified=not any(b in iss for b in BAD),
                    flags=";".join(t for t in iss.split(";") if t in BAD or t in ("ocr", "dup_row", "institution"))))
    if len(buf) >= BATCH:
        flush()
flush()
print(post({"finish": time.strftime("%Y-%m-%d")}))
