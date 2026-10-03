import pandas as pd, random, subprocess, csv, re, json
random.seed(7)
man={r['id']:r for r in csv.DictReader(open('manifest.csv',encoding='utf-8-sig'))}
d=pd.read_csv('output/dividends_all.csv',dtype=str,keep_default_na=False)
# 1. integrity: no row without doc_id, page, company
print('rows missing doc_id/page/company:',((d.doc_id=='')|(d.page=='')|(d.company=='')).sum(),'of',len(d))
# 2. random re-check of 10 rows against the PDF page text (non-flagged, with name+amount)
clean=d[(d.issues=='')&(d.net_amount!='')&(d.holder_name_raw!='')]
sample=clean.sample(10,random_state=7)
ok=0
for _,r in sample.iterrows():
    p='/home/user/findassests/'+man[r.doc_id]['local_path']
    t=subprocess.run(['pdftotext','-layout','-f',r.page,'-l',r.page,p,'-'],capture_output=True,text=True).stdout
    t=re.sub(r'\s+',' ',t)
    amt=f"{float(r.net_amount):,.2f}"
    amt2=f"{float(r.net_amount):.2f}"
    name_ok=r.holder_name_raw.split()[0].lower() in t.lower() and ' '.join(r.holder_name_raw.split()[:2]).lower() in t.lower()
    amt_ok=(amt in t) or (amt2 in t) or (str(int(float(r.net_amount))) in t)
    ident=r.bo_id or r.folio_no
    id_ok=(ident.replace(' ','') in t.replace(' ','')) if ident else True
    good=name_ok and amt_ok and id_ok; ok+=good
    print(r.dividend_id, r.holder_name_raw[:28],'|',r.net_amount,'|',ident[:18],'-> name',name_ok,'amt',amt_ok,'id',id_ok)
print('spot check passed',ok,'/ 10')
# 3. totals check summary
docs=pd.read_csv('output/documents_report.csv',keep_default_na=False)
print(docs.total_check.value_counts().to_dict(), docs.status.value_counts().to_dict())
# 4. fuzzy auto-merges: none are performed
print('match methods',d.match_method.value_counts().to_dict())
