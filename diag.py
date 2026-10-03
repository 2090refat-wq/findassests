import sys, csv, re, subprocess, json
import extract as X
man={r['id']:r for r in csv.DictReader(open('manifest.csv',encoding='utf-8-sig'))}
def run(doc, n=10, pages=None):
    p=man[doc]['local_path']; st=json.load(open(f'extracted/stats/{doc}.json'))
    raw=list(csv.DictReader(open(f'extracted/raw/{doc}.csv',encoding='utf-8')))
    got={r['line'].strip() for r in raw}
    np_=st['pages']; 
    out=subprocess.run(['pdftotext','-layout',p,'-'],capture_output=True,text=True,errors='replace').stdout.translate(X.BN)
    pgs=out.split('\f')
    un=[];tot=[]
    for i,t in enumerate(pgs,1):
        for l in t.split('\n'):
            s=l.strip()
            if not s or s in got: continue
            if re.search(r'\btotal\b',s,re.I) and re.search(r'\d',s): tot.append((i,s[:150]))
            elif re.search(r'\d',s) and len(s.split())>=3: un.append((i,s[:150]))
    print(f'## {doc} {man[doc]["company"]} dtype={st["dtype"]} rows={st["rows"]} ext={st["extracted_total"]} sh={st["extracted_shares"]} unparsed_data_like={len(un)}')
    print('  title:',man[doc]['title'][:100])
    print('  sample parsed:',[ (r['holder_name_raw'][:20],r['net_amount'] or r['shares']) for r in raw[:2]])
    step=max(1,len(un)//n)
    for i,s in un[::step][:n]: print('   UN p%d: %s'%(i,s))
    for i,s in tot[:4]+tot[-3:]: print('   TOT p%d: %s'%(i,s))
if __name__=='__main__':
    for d in sys.argv[1:]: run(d)
