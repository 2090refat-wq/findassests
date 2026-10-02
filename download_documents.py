#!/usr/bin/env python3
"""
Download every unclaimed-dividend document listed in download_list.csv.

Works in a Claude cloud session (with Custom network access, see PROMPT.md)
and on your own computer (Windows / macOS / Linux, Python 3.9+).

    pip install requests beautifulsoup4 pypdf
    python download_documents.py                 # download everything
    python download_documents.py --only D012,D013
    python download_documents.py --retry-failed  # re-run only failed rows
    python download_documents.py --dry-run       # show the plan, download nothing

It is resumable: rows already marked "ok" in manifest.csv are skipped.

Outputs
  downloads/<company>/<id>__<filename>   the files
  manifest.csv                           one row per file: status, size, sha256, pages, error
  discovered_links.csv                   file links found inside web pages (kind=html_page / wp_download_page)
"""
import argparse, csv, hashlib, re, sys, time, urllib.parse
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing package: pip install requests beautifulsoup4 pypdf")
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

HERE = Path(__file__).resolve().parent
LIST = HERE / "download_list.csv"
OUT = HERE / "downloads"
MANIFEST = HERE / "manifest.csv"
DISCOVERED = HERE / "discovered_links.csv"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
FILE_EXT = re.compile(r"\.(pdf|xlsx?|docx?|csv|jpe?g|png)(\?|#|$)", re.I)
KEYWORDS = re.compile(r"unclaim|unpaid|undistribut|unsettl|dividend|suspense|refund", re.I)
MANIFEST_FIELDS = ["id", "parent_id", "company", "title", "url", "final_url", "kind", "role",
                   "status", "http_status", "content_type", "local_path", "bytes", "sha256",
                   "pages", "error", "downloaded_at"]

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9,bn;q=0.8"})
_last_hit = {}


def polite_get(url, **kw):
    """GET with per-host 1.5 s spacing and 3 retries with backoff."""
    host = urllib.parse.urlparse(url).netloc
    wait = 1.5 - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    err = None
    for attempt in range(3):
        try:
            r = session.get(url, timeout=90, allow_redirects=True, **kw)
            _last_hit[host] = time.time()
            if r.status_code in (429, 500, 502, 503, 504):
                err = f"HTTP {r.status_code}"
                time.sleep(5 * (attempt + 1))
                continue
            return r
        except requests.RequestException as e:
            err = f"{type(e).__name__}: {e}"
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(err)


def slug(s, n=60):
    s = re.sub(r"[^\w\-]+", "_", s.strip()).strip("_")
    return s[:n] or "file"


def gdrive_id(url):
    m = re.search(r"/d/([A-Za-z0-9_-]{10,})", url) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", url)
    return m.group(1) if m else None


def fetch_gdrive(url):
    fid = gdrive_id(url)
    if not fid:
        raise RuntimeError("could not read Google Drive file id")
    dl = f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
    r = polite_get(dl)
    if "text/html" in r.headers.get("Content-Type", "") and b"%PDF" not in r.content[:1024]:
        # older confirm-token flow
        r2 = polite_get(f"https://drive.google.com/uc?export=download&id={fid}")
        tok = re.search(r'confirm=([0-9A-Za-z_-]+)', r2.text)
        if tok:
            r2 = polite_get(f"https://drive.google.com/uc?export=download&confirm={tok.group(1)}&id={fid}")
        r = r2
    return r


def find_file_links(html, base_url):
    """Return (url, text) for document links on a page, preferring dividend-related ones."""
    links = []
    if BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            links.append((urllib.parse.urljoin(base_url, a["href"]), a.get_text(" ", strip=True)))
        for tag in soup.find_all(["iframe", "embed", "object"]):
            src = tag.get("src") or tag.get("data")
            if src:
                links.append((urllib.parse.urljoin(base_url, src), "embedded"))
    else:
        for href in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
            links.append((urllib.parse.urljoin(base_url, href), ""))
    out, seen = [], set()
    for u, t in links:
        is_file = FILE_EXT.search(u) or "wpdmdl=" in u or "drive.google.com/file" in u or "download=" in u
        if is_file and u not in seen and (KEYWORDS.search(u) or KEYWORDS.search(t) or "wpdmdl=" in u):
            seen.add(u)
            out.append((u, t))
    return out


def ext_for(resp, url):
    ct = resp.headers.get("Content-Type", "").lower()
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
    if m and "." in m.group(1):
        return Path(urllib.parse.unquote(m.group(1))).suffix.lower()
    if resp.content[:4] == b"%PDF" or "pdf" in ct:
        return ".pdf"
    if "spreadsheet" in ct or "excel" in ct:
        return ".xlsx"
    if "word" in ct:
        return ".docx"
    if "jpeg" in ct:
        return ".jpg"
    m = FILE_EXT.search(url)
    return "." + m.group(1).lower() if m else ".bin"


def pdf_pages(path):
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:
        return ""


def save(resp, row, parent_id=""):
    company_dir = OUT / slug(row["company"], 40)
    company_dir.mkdir(parents=True, exist_ok=True)
    name = Path(urllib.parse.unquote(urllib.parse.urlparse(resp.url).path)).stem or row["title"]
    path = company_dir / f"{row['id']}__{slug(name)}{ext_for(resp, resp.url)}"
    path.write_bytes(resp.content)
    return {
        "id": row["id"], "parent_id": parent_id, "company": row["company"], "title": row["title"],
        "url": row["url"], "final_url": resp.url, "kind": row["kind"], "role": row["role"],
        "status": "ok", "http_status": resp.status_code,
        "content_type": resp.headers.get("Content-Type", ""), "local_path": str(path.relative_to(HERE)),
        "bytes": len(resp.content), "sha256": hashlib.sha256(resp.content).hexdigest(),
        "pages": pdf_pages(path) if path.suffix == ".pdf" else "", "error": "",
        "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def fail(row, msg, parent_id="", http_status=""):
    return {"id": row["id"], "parent_id": parent_id, "company": row["company"], "title": row["title"],
            "url": row["url"], "kind": row["kind"], "role": row["role"], "status": "failed",
            "http_status": http_status, "error": msg[:300], "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def looks_like_document(resp):
    ct = resp.headers.get("Content-Type", "").lower()
    return resp.ok and (resp.content[:4] == b"%PDF" or not ct.startswith("text/html"))


def process(row, discovered_writer):
    """Return a list of manifest rows (a web page can yield several files)."""
    kind, url = row["kind"], row["url"]
    try:
        if kind == "gdrive":
            r = fetch_gdrive(url)
            return [save(r, row)] if looks_like_document(r) else [fail(row, "Drive returned a web page (file may be private or need sign-in)", http_status=r.status_code)]

        r = polite_get(url)
        if not r.ok:
            return [fail(row, f"HTTP {r.status_code}", http_status=r.status_code)]
        if looks_like_document(r):
            return [save(r, row)]

        # An HTML page: look for the real document links inside it.
        found = find_file_links(r.text, r.url)
        if not found:
            return [fail(row, "page loaded but no document links found (may need a real browser / JavaScript)", http_status=r.status_code)]
        results = []
        for n, (link, text) in enumerate(found, 1):
            child = dict(row, id=f"{row['id']}-{n:02d}", url=link, title=(text or row["title"])[:150])
            discovered_writer.writerow({"parent_id": row["id"], "child_id": child["id"], "company": row["company"], "link_text": text, "url": link})
            try:
                cr = fetch_gdrive(link) if "drive.google.com" in link else polite_get(link)
                results.append(save(cr, child, parent_id=row["id"]) if looks_like_document(cr)
                               else fail(child, "child link returned a web page", parent_id=row["id"], http_status=cr.status_code))
            except Exception as e:
                results.append(fail(child, str(e), parent_id=row["id"]))
        return results
    except Exception as e:
        return [fail(row, str(e))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated ids, e.g. D001,D014")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-reference", action="store_true",
                    help="also download forms/notices/guides (role=reference_only)")
    a = ap.parse_args()

    rows = list(csv.DictReader(open(LIST, encoding="utf-8-sig")))
    done, failed, previous = set(), set(), []
    if MANIFEST.exists():
        previous = list(csv.DictReader(open(MANIFEST, encoding="utf-8-sig")))
        done = {m["id"].split("-")[0] for m in previous if m["status"] == "ok"}
        failed = {m["id"].split("-")[0] for m in previous if m["status"] != "ok"}

    todo = []
    for r in rows:
        if a.only and r["id"] not in a.only.split(","):
            continue
        if r["role"] == "reference_only" and not a.include_reference:
            continue
        if r["id"] in done and not a.only:
            continue
        if a.retry_failed and r["id"] not in failed:
            continue
        todo.append(r)

    print(f"{len(rows)} rows in list, {len(done)} already downloaded, {len(todo)} to do")
    if a.dry_run:
        for r in todo:
            print(f"  {r['id']}  {r['kind']:<16} {r['role']:<15} {r['company'][:28]:<28} {r['url'][:90]}")
        return

    OUT.mkdir(exist_ok=True)
    keep = [m for m in previous if m["id"].split("-")[0] not in {r["id"] for r in todo}]
    new_disc = not DISCOVERED.exists()
    with open(DISCOVERED, "a", newline="", encoding="utf-8-sig") as df:
        dw = csv.DictWriter(df, fieldnames=["parent_id", "child_id", "company", "link_text", "url"])
        if new_disc:
            dw.writeheader()
        results = []
        for i, r in enumerate(todo, 1):
            res = process(r, dw)
            results += res
            ok = sum(1 for x in res if x["status"] == "ok")
            print(f"[{i}/{len(todo)}] {r['id']} {r['company'][:30]:<30} ok={ok} failed={len(res)-ok}"
                  + (f"  ({res[0].get('error','')[:70]})" if ok == 0 else ""))
            # write the manifest after every row so an interruption loses nothing
            with open(MANIFEST, "w", newline="", encoding="utf-8-sig") as mf:
                mw = csv.DictWriter(mf, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
                mw.writeheader()
                mw.writerows(keep + results)

    allm = keep + results
    ok = sum(1 for m in allm if m["status"] == "ok")
    print(f"\nDone. {ok} files ok, {len(allm) - ok} failed. See manifest.csv")


if __name__ == "__main__":
    main()
