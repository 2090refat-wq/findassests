import csv, re, pdfplumber, json
from pathlib import Path
m=[r for r in csv.DictReader(open('manifest.csv',encoding='utf-8-sig')) if r['status']=='ok']
out=[]
for r in m:
    p=Path(r['local_path']); row={'id':r['id'],'company':r['company'],'role':r['role'],'path':str(p),'pages':r['pages'],'pdf_kind':'other','lang':'','sample':''}
    if p.suffix=='.pdf':
        try:
            with pdfplumber.open(p) as pdf:
                n=len(pdf.pages); idx=sorted({0,n//2,n-1}); txt=[]; textpages=0
                for i in idx:
                    t=pdf.pages[i].extract_text() or ''; txt.append(t)
                    if len(t.strip())>80: textpages+=1
                row['pdf_kind']='text' if textpages==len(idx) else ('scanned' if textpages==0 else 'mixed')
                j=' '.join(txt); row['lang']='bn' if re.search('[ঀ-৿]',j) else 'en'
                row['sample']=(txt[0] or '')[:300].replace('\n',' | ')
        except Exception as e: row['pdf_kind']='error'; row['sample']=str(e)[:100]
    elif p.suffix in('.xlsx','.xls','.csv'): row['pdf_kind']='spreadsheet'
    out.append(row)
w=csv.DictWriter(open('extracted/triage.csv','w',newline='',encoding='utf-8'),fieldnames=list(out[0])); w.writeheader(); w.writerows(out)
import collections; print(collections.Counter(r['pdf_kind'] for r in out))
