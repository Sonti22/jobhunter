from pathlib import Path
import json, re
from PIL import Image, ImageOps, ImageDraw, ImageFont
from pypdf import PdfReader
import pdfplumber
P=Path(__file__).parent
meta=json.loads((P/'universal_build_meta.json').read_text(encoding='utf-8'))
f=Path(meta['pdf']);r=PdfReader(f)
ann=0;uris=[];internal=0;problems=[]
for i,p in enumerate(r.pages):
    t=p.extract_text()
    if '\ufffd' in t or '\u25a0' in t:problems.append((i+1,'replacement glyph'))
    if len(t)<150:problems.append((i+1,'too little text'))
    for a in p.get('/Annots',[]):
        a=a.get_object();ann+=1
        if a.get('/A',{}).get('/URI'):uris.append(str(a['/A']['/URI']))
        elif a.get('/Dest'):internal+=1
with pdfplumber.open(f) as doc:
    for i,p in enumerate(doc.pages):
        # Allow intentionally full-bleed cover image; text must remain inside paper.
        for ch in p.chars:
            if ch['x0']<0 or ch['x1']>p.width+.5 or ch['top']<-1 or ch['bottom']>p.height+1:
                problems.append((i+1,'outside page',ch.get('text'),[ch['x0'],ch['x1'],ch['top'],ch['bottom']]))
for u in uris:
    if not u.startswith(('https://','http://')):problems.append(('uri',u))
    if any(s in u for s in ['search_query','search?q=','results?search_query','PLACEHOLDER']):problems.append(('search/placeholder',u))
imgs=sorted(P.glob('universal_render-*.png'))
assert len(imgs)==len(r.pages),(len(imgs),len(r.pages))
font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',17)
for start in range(0,len(imgs),16):
    sh=Image.new('RGB',(1280,1896),'#dce5eb');d=ImageDraw.Draw(sh)
    for idx,fp in enumerate(imgs[start:start+16]):
        im=Image.open(fp).convert('RGB');im.thumbnail((310,438))
        x=(idx%4)*320+5;y=(idx//4)*474+24
        sh.paste(im,(x,y));d.text((x,y-22),f'Page {start+idx+1}',font=font,fill='#152d42')
    sh.save(P/f'universal_contact_{start//16+1:02d}.jpg',quality=90)
report={'pages':len(r.pages),'bytes':f.stat().st_size,'annotations':ann,'external_link_instances':len(uris),'external_urls':len(set(uris)),'internal_links':internal,'issues':problems,'lowest_content_bottoms':sorted(meta['bottom_positions'],key=lambda x:x[1])[:12]}
(P/'universal_qa_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
