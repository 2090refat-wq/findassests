import json
def candidates(st, extra=()):
    t = [v for v in (st.get("totals") or []) + list(extra) if v and v >= 100]
    c = set(t)
    for nums in (st.get("total_lines") or [])[:300]:      # e.g. one "Total" line with a column per year
        nums = [v for v in nums if v and v >= 100]
        c |= set(nums)
        if len(nums) >= 2:
            c.add(round(sum(nums), 2))
    if t:
        c |= {round(sum(t), 2), round(sum(t) - max(t), 2), round(sum(t) / 2, 2)}
    return c
def check(ext, cands, tol=0.005):
    if not cands:
        return "no_total_printed", None
    best = min(cands, key=lambda c: abs(c - ext))
    return ("match" if ext and abs(best - ext) <= tol * best else "mismatch"), best

def score_raw(path, st):
    """(status, relative_error, ext, rows) for one raw CSV against the printed totals."""
    import pandas as pd
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception:
        return ("n/a", 9e9, 0, 0)
    tm = df.holder_name_raw.str.match(r"(?i)^\W*(grand\s+|sub\s*-?\s*)?total\b")
    ex = [float(v) for v in pd.to_numeric(df.loc[tm, "net_amount"], errors="coerce").dropna()]
    df = df[~tm]
    if not len(df):
        return ("n/a", 9e9, 0, 0)
    stock = df.dividend_type.isin(["stock", "right"]).mean() > 0.5
    v = pd.to_numeric(df["shares" if stock else "net_amount"], errors="coerce")
    v = v.where(v.abs() < 1e9)           # reject values that are ID digits / concatenations
    ext = float(v.sum())
    status, best = check(ext, candidates(st, ex))
    if best is None:
        return (status, 0.0, ext, len(df))
    return (status, abs(ext - best) / best if best else 9e9, ext, len(df))


def year_hits(path, st):
    """(years_with_printed_total_match, years_with_nonzero_sum) for a per-year extraction."""
    import pandas as pd
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if not len(df):
        return 0, 0
    stock = df.dividend_type.isin(["stock", "right"]).mean() > 0.5
    df["v"] = pd.to_numeric(df["shares" if stock else "net_amount"], errors="coerce")
    ys = df.groupby("dividend_year")["v"].sum()
    ys = ys[ys > 0]
    cands = candidates(st)
    hit = [y for y, v in ys.items() if any(abs(v - c) <= 0.005 * c for c in cands)]
    return len(hit), len(ys)
