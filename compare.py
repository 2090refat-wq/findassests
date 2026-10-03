import csv, json, sys, pandas as pd
from totalcheck import candidates, check
ids = open('/tmp/mm.txt').read().split(',') if len(sys.argv) < 2 else sys.argv[1].split(',')
res = []
for d in ids:
    st = json.load(open(f'extracted/stats/{d}.json'))
    out = {'doc': d}
    for tag, path in (('gen', f'extracted/raw/{d}.csv'), ('col', f'extracted/raw_col/{d}.csv')):
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
        except Exception:
            out[tag] = ('n/a', None, 0); continue
        # drop total-labelled rows (they feed the totals check)
        tm = df.holder_name_raw.str.match(r"(?i)^\W*(grand\s+|sub\s*-?\s*)?total\b")
        ex = [float(v) for v in pd.to_numeric(df.loc[tm, 'net_amount'], errors='coerce').dropna()]
        df = df[~tm]
        stock = (df.dividend_type.isin(['stock', 'right'])).mean() > 0.5 if len(df) else False
        ext = pd.to_numeric(df['shares' if stock else 'net_amount'], errors='coerce').sum()
        status, best = check(ext, candidates(st, ex))
        out[tag] = (status, best, len(df), round(ext, 2))
    res.append(out)
rows = []
for o in res:
    g, c = o['gen'], o['col']
    rows.append((o['doc'], g[0], g[2] if len(g) > 2 else 0, g[3] if len(g) > 3 else '', c[0], c[2] if len(c) > 2 else 0, c[3] if len(c) > 3 else '', c[1] if len(c)>1 else None))
df = pd.DataFrame(rows, columns=['doc', 'gen', 'gen_rows', 'gen_ext', 'col', 'col_rows', 'col_ext', 'printed'])
pd.set_option('display.width', 220)
print(df['gen'].value_counts().to_dict(), df['col'].value_counts().to_dict())
print(df.to_string())
