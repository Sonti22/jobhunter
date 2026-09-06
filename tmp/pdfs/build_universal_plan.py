from pathlib import Path
import json, math, re, html, sys
from collections import Counter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, Color, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Table, TableStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from universal_content import MODULES, MERGE_MAP, GLOSSARY
from universal_sources_extra import EXTRA, EXTRA_BY_MODULE

ROOT=Path('C:/Projects/jobhunter')
TMP=ROOT/'tmp/pdfs'
OUT=ROOT/'output/pdf'
PDF=OUT/'AI_ML_Architect_Универсальный_план_Сурен_v3.pdf'
PHOTO=OUT/'assets/architect_photoillustration.png'
OUT.mkdir(parents=True,exist_ok=True)
for name,path in [('AR','arial.ttf'),('AB','arialbd.ttf'),('AI','ariali.ttf')]:
    pdfmetrics.registerFont(TTFont(name,'C:/Windows/Fonts/'+path))
pdfmetrics.registerFontFamily('AR',normal='AR',bold='AB',italic='AI',boldItalic='AB')
NAVY=HexColor('#152d42'); TEAL=HexColor('#087f83'); AQUA=HexColor('#e8f5f3'); INK=HexColor('#223446')
MUTED=HexColor('#607587'); PALE=HexColor('#f2f6f9'); LINE=HexColor('#d7e1e8'); AMBER=HexColor('#bd701c'); SAND=HexColor('#fff3df')
W,H=595.276,841.89; MARGIN=42; CW=W-2*MARGIN; BOTTOM=51
SOURCES={}
for prefix,name in [('B','backend'),('A','ai'),('R','ru')]:
    data=json.loads((TMP/f'universal_sources_{name}.json').read_text(encoding='utf-8'))
    for i,r in enumerate(data): SOURCES[f'{prefix}{i+1:02d}']=r
SOURCES.update(EXTRA)
for m in MODULES:
    for l in m['lessons']:
        assert l['doc'] in SOURCES and l['video'] in SOURCES,(m['code'],l)
        assert 'video' in SOURCES[l['video']]['kind'],(m['code'],l['video'])
USED=sorted({r for m in MODULES for l in m['lessons'] for r in (l['doc'],l['video'])}|{r for v in EXTRA_BY_MODULE.values() for r in v})
CORE=sum(m['hours'] for m in MODULES if m['track']=='Основной маршрут')
BASE=sum(m['hours'] for m in MODULES if m['track']=='Подготовительная база')
ADV=sum(m['hours'] for m in MODULES if m['track']=='Углубление')
SESSIONS=sum(len(m['lessons']) for m in MODULES)

def clean(s):
    return str(s).replace('\u00a0',' ').replace('\u2011','-').replace('\u2013','-').replace('\u2014','-').replace('\u2010','-')
def e(s): return html.escape(clean(s))
def para(text,size=10.3,bold=False,color=INK,leading=None,align=TA_LEFT,markup=False):
    return Paragraph(text if markup else e(text),ParagraphStyle('p',fontName='AB' if bold else 'AR',fontSize=size,leading=leading or size*1.32,textColor=color,alignment=align,spaceAfter=0,splitLongWords=True))
def link(code,label=None):
    s=SOURCES[code]
    return f'<a href="{html.escape(s["url"],quote=True)}" color="#087f83">{e(label or s["title"])} [{code}]</a>'
def anchor(code): return 'mod_'+code.replace('Б','B').replace('Д','D')
def access_short(s):
    if s['url'].startswith('https://www.deeplearning.ai'): return 'Регистрация; условия доступа проверить на странице.'
    a=s.get('access','').lower()
    if 'платный видеокурс' in a:return 'Платный курс; открытые docs - бесплатная альтернатива.'
    if 'регистрац' in a:return 'Регистрация может потребоваться; API/облако отдельно.'
    return 'Просмотр/чтение открыты; внешние ресурсы отдельно.'

class Book:
    def __init__(self,path,old=None,total=None):
        self.c=canvas.Canvas(str(path),pagesize=(W,H),pageCompression=1)
        self.c.setTitle('AI/ML Architect - Универсальный план для Сурена, v3')
        self.c.setAuthor('План подготовлен для Сурена на основе двух версий и вакансии Интерика Лаб')
        self.c.setSubject('36 модулей, 168 занятий, видео, документация, схемы, пилот и портфолио')
        self.n=0;self.open=False;self.y=0;self.pages={};self.old=old or {};self.total=total
        self.bounds=[];self.figures=0
    def finish(self):
        if self.open:
            if self.y<BOTTOM: raise ValueError(f'Overflow on page {self.n}: y={self.y}')
            self.bounds.append((self.n,round(self.y,1)))
            self.c.showPage();self.open=False
    def new(self,title,kicker='Универсальный маршрут',key=None,subtitle=None,outline=True):
        self.finish(); self.n+=1;self.open=True
        c=self.c;c.setFillColor(white);c.rect(0,0,W,H,fill=1,stroke=0)
        c.setFillColor(TEAL);c.rect(MARGIN,H-38,22,3,fill=1,stroke=0)
        c.setFont('AB',8);c.setFillColor(MUTED);c.drawString(MARGIN+31,H-38,'СУРЕН  /  AI/ML ARCHITECT')
        c.setFont('AR',8);c.drawRightString(W-MARGIN,H-38,clean(kicker).upper())
        c.setStrokeColor(LINE);c.line(MARGIN,43,W-MARGIN,43)
        c.setFont('AR',8);c.setFillColor(MUTED);c.drawString(MARGIN,28,'Универсальный план v3  |  06.09.2026')
        c.drawRightString(W-MARGIN,28,f'{self.n:03d}'+(f' / {self.total}' if self.total else ''))
        c.setFillColor(TEAL);c.drawCentredString(W/2,28,'Содержание')
        c.linkAbsolute('Содержание','toc',(W/2-35,22,W/2+35,38),thickness=0)
        if key:
            self.pages[key]=(self.n,title);c.bookmarkPage(key)
            if outline:c.addOutlineEntry(clean(title),key,level=0,closed=False)
        self.y=H-70
        self.p(title,24,bold=True,leading=27.5,color=NAVY,space=9)
        if subtitle:self.p(subtitle,10,color=MUTED,space=12)
    def p(self,text,size=10.3,bold=False,color=INK,space=7,leading=None,markup=False,width=None,x=None):
        x=MARGIN if x is None else x;width=CW if width is None else width
        p=para(text,size,bold,color,leading,markup=markup);_,h=p.wrap(width,1000)
        p.drawOn(self.c,x,self.y-h);self.y-=h+space
        return h
    def label(self,text):self.p(text,9,bold=True,color=TEAL,space=7)
    def section(self,title,text):
        self.p(title,12,bold=True,color=NAVY,space=5);self.p(text,space=10)
    def bullets(self,items,size=10.3):
        for t in items:self.p('• '+t,size,space=7)
    def callout(self,title,body,tint=AQUA):
        p1=para(title,10.5,True,TEAL);p2=para(body,10.1)
        _,h1=p1.wrap(CW-24,1000);_,h2=p2.wrap(CW-24,1000);hh=h1+h2+29
        self.c.setFillColor(tint);self.c.roundRect(MARGIN,self.y-hh,CW,hh,7,fill=1,stroke=0)
        p1.drawOn(self.c,MARGIN+12,self.y-11-h1);p2.drawOn(self.c,MARGIN+12,self.y-17-h1-h2)
        self.y-=hh+12
    def table(self,rows,widths=None,size=9.6,head=True,pad=7,blankheight=None):
        widths=widths or [CW/len(rows[0])]*len(rows[0])
        data=[[para(v,size,bold=(head and i==0),color=white if head and i==0 else INK) for v in row] for i,row in enumerate(rows)]
        t=Table(data,colWidths=widths,rowHeights=None if blankheight is None else [None]+[blankheight]*(len(rows)-1))
        cmds=[('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),pad),('RIGHTPADDING',(0,0),(-1,-1),pad),('TOPPADDING',(0,0),(-1,-1),pad),('BOTTOMPADDING',(0,0),(-1,-1),pad),('LINEBELOW',(0,0),(-1,-1),.4,LINE)]
        if head:cmds.append(('BACKGROUND',(0,0),(-1,0),NAVY))
        for i in range(1 if head else 0,len(rows)):
            if i%2:cmds.append(('BACKGROUND',(0,i),(-1,i),PALE))
        t.setStyle(TableStyle(cmds));_,hh=t.wrap(CW,1000)
        t.drawOn(self.c,MARGIN,self.y-hh);self.y-=hh+12
    def flow(self,labels,caption,kind='flow'):
        c=self.c;y=self.y;boxh=43;gap=13;n=len(labels);bw=(CW-(n-1)*gap)/n
        for i,l in enumerate(labels):
            x=MARGIN+i*(bw+gap);c.setFillColor(AQUA if i%2==0 else PALE);c.setStrokeColor(LINE)
            c.roundRect(x,y-boxh,bw,boxh,5,fill=1,stroke=1)
            pp=para(l,9.3,True,TEAL,11.4,TA_CENTER);_,hh=pp.wrap(bw-12,200);pp.drawOn(c,x+6,y-(boxh+hh)/2)
            if i<n-1 and kind=='flow':
                ax=x+bw+2;ay=y-boxh/2;c.setStrokeColor(TEAL);c.line(ax,ay,ax+gap-4,ay)
                c.line(ax+gap-4,ay,ax+gap-7,ay+2);c.line(ax+gap-4,ay,ax+gap-7,ay-2)
        self.y-=boxh+7;self.p(caption,9.1,color=MUTED,leading=11.8,space=10);self.figures+=1
    def bookmark_link(self,key,label):
        n=self.old.get(key,(0,''))[0]
        return f'<a href="#{key}" color="#087f83">{e(label)} <b>{n or "..."}</b></a>'
    def module(self,m):
        code=m['code'];key=anchor(code)
        self.new(f'{code}. {m["title"]}',m['track'],key,outline=True)
        self.p(f'{m["hours"]} ч активной работы  |  {len(m["lessons"])} занятий  |  резерв: около 25%',9.2,bold=True,color=TEAL,space=6)
        self.p(m['goal'],10.5,space=7)
        self.p('Перед стартом: '+m['prereq'],9.1,color=MUTED,space=9)
        self.flow(m['labels'],m['caption'],'compare' if code in ('Д1','Д3') else 'flow')
        n=len(m['lessons']); hs=[m['hours']//n+(i<m['hours']%n) for i in range(n)]
        for i,(l,hrs) in enumerate(zip(m['lessons'],hs)):
            self.p(f'{i+1:02d} / {hrs} ч. {l["title"]}',10.3,True,color=NAVY,space=3)
            self.p(l['task'],9.9,leading=13,space=7)
        self.p('<b>Готовность.</b> '+e(m['ready']),9.8,markup=True,space=5)
        self.p('<b>Сохранить.</b> '+e(m['artifact']),9.4,markup=True,space=5)
        self.p('<b>Объяснить без подсказки.</b> '+e(m['question']),9.4,markup=True,space=4)
        self.new('Материалы и разбор',f'{code} / {m["title"][:37]}',key+'_media',outline=False)
        self.p('Каждая карточка соответствует занятию на предыдущей странице. Повторяющееся видео смотрится по указанной теме, а не заново целиком.',10,color=MUTED,space=12)
        for i,l in enumerate(m['lessons']):
            ds,vs=SOURCES[l['doc']],SOURCES[l['video']]
            parts=[(f'<b>{i+1:02d}. {e(l["title"])}</b>',10.3,True),
                   ('Видео: '+link(l['video'])+f' <font color="#607587">({e(vs.get("language","EN").upper())})</font>',9.5,False),
                   ('Читать: '+link(l['doc'])+f' <font color="#607587">({e(ds.get("language","EN").upper())})</font>',9.5,False),
                   (e(l['focus']),9.2,False)]
            ps=[];hh=17
            for tx,sz,bo in parts:
                pp=para(tx,sz,False,INK,sz*1.28,markup=True);_,ph=pp.wrap(CW-38,1000);ps.append((pp,ph));hh+=ph+3
            self.c.setFillColor(PALE);self.c.roundRect(MARGIN,self.y-hh,CW,hh,6,fill=1,stroke=0)
            self.c.setFillColor(TEAL);self.c.circle(MARGIN+11,self.y-15,4,fill=1,stroke=0)
            yy=self.y-9
            for pp,ph in ps:pp.drawOn(self.c,MARGIN+24,yy-ph);yy-=ph+3
            self.y-=hh+8
        extras=EXTRA_BY_MODULE.get(code,[])
        if extras:self.p('<b>Дополнить по задаче:</b> '+ ' · '.join(link(k) for k in extras),9.3,markup=True,space=9)
        # Access terms close to the video links, detailed verification in the registry.
        notes=sorted({access_short(SOURCES[l['video']]) for l in m['lessons']})
        self.p('Доступ: '+' '.join(notes),8.5,color=MUTED,space=8)
        self.p('<b>На что обратить внимание.</b> '+e(m['pitfall']),9.5,markup=True,space=5)
    def save(self):
        self.finish();self.c.save()

def draw_box(b,x,y,w,h,title,body='',fill=PALE):
    c=b.c;c.setFillColor(fill);c.setStrokeColor(LINE);c.roundRect(x,y-h,w,h,7,fill=1,stroke=1)
    p=para(title,11,True,TEAL,14,TA_CENTER);_,hh=p.wrap(w-14,1000);p.drawOn(c,x+7,y-10-hh)
    if body:
        p=para(body,9.3,False,INK,12,TA_CENTER);_,bh=p.wrap(w-18,1000);p.drawOn(c,x+9,y-hh-17-bh)
def arrow(b,x1,y1,x2,y2):
    c=b.c;c.setStrokeColor(TEAL);c.setLineWidth(1.3);c.line(x1,y1,x2,y2)
    ang=math.atan2(y2-y1,x2-x1);a=5
    for d in (-.5,.5):c.line(x2,y2,x2-a*math.cos(ang+d),y2-a*math.sin(ang+d))
    c.setLineWidth(1)

def cover(b):
    b.n=1;b.open=True;c=b.c
    c.setFillColor(NAVY);c.rect(0,0,W,H,fill=1,stroke=0)
    c.setFillColor(HexColor('#7adbd2'));c.setFont('AB',10);c.drawString(MARGIN,H-55,'УНИВЕРСАЛЬНЫЙ ПЛАН  /  ВЕРСИЯ 3')
    c.setFont('AB',46);c.setFillColor(white);c.drawString(MARGIN,H-125,'AI/ML')
    c.drawString(MARGIN,H-180,'Architect')
    p=para('От бизнес-задачи до работающего AI-сервиса',23,True,white,29);_,hh=p.wrap(CW,1000);p.drawOn(c,MARGIN,H-205-hh)
    c.setFont('AR',13);c.drawString(MARGIN,H-302,'Для Сурена  •  По вакансии «Интерика Лаб»')
    c.drawImage(str(PHOTO),0,315,width=W,height=W/3,mask='auto')
    c.setFont('AR',7.5);c.setFillColor(HexColor('#bbced9'));c.drawString(MARGIN,304,'Фотоиллюстрация создана AI: разработка, документы, работа команды.')
    for i,(num,label) in enumerate([('36','модулей'),(str(SESSIONS),'занятий'),(str(CORE)+' ч','основной маршрут')]):
        xx=MARGIN+i*174;c.setFillColor(HexColor('#7adbd2'));c.setFont('AB',30);c.drawString(xx,248,num)
        c.setFillColor(white);c.setFont('AR',11);c.drawString(xx,228,label)
    p=para('Python и backend · LLM, RAG и OCR · CRM, 1С, МойСклад\nДиалоги и агенты · Надёжность · Лидерство · Реальный пилот',12,False,white,18);_,hh=p.wrap(CW,1000);p.drawOn(c,MARGIN,175-hh)
    c.setFillColor(HexColor('#bbced9'));c.setFont('AR',10);c.drawString(MARGIN,70,'Видео, документация, схемы, практика и критерии готовности')
    c.setFont('AR',9);c.drawString(MARGIN,49,'Объединены обе версии плана  |  6 сентября 2026')
    b.y=60;b.pages['cover']=(1,'Обложка');c.bookmarkPage('cover');c.addOutlineEntry('Обложка','cover',0,False);b.figures+=1

def front(b):
    cover(b)
    b.new('Как пользоваться этим планом','Начало маршрута','start')
    b.p('Это единая программа из двух планов: 18-модульной версии на 48 страниц и присланной 21-модульной версии на 34 страницы. Повторы объединены, полезные специализации сохранены, порядок и ошибки исправлены.',11,space=13)
    b.callout('Один сквозной проект','AI-помощник менеджера отвечает по разрешённым документам, извлекает поля, показывает review и после подтверждения выполняет ограниченное действие в CRM или учётной системе.')
    b.section('Три части маршрута',f'Б1-Б4: {BASE} часа повторения базы по результатам диагностики. 01-24: {CORE} часа основного маршрута. Д1-Д8: {ADV} часа углубления, когда это оправдано проектом или стеком компании. Все темы присутствуют; уровень практики выбирается осознанно.')
    b.section('Результат каждого занятия','Прочитать тему, посмотреть нужный фрагмент видео, реализовать свою задачу, проверить существенный отказ, сохранить результат и объяснить его. Часы включают всю эту работу; это не длительность видеоролика.')
    b.section('Как устроены материалы','У каждого занятия есть ссылка на видео и документацию, а у каждого модуля - собственная схема. RU/EN обозначает язык. Некоторые ссылки ведут к странице курса или каталогу с указанным названием урока. Если видео объясняет общий паттерн на другом продукте, это прямо отмечено.')
    b.section('Как пользоваться изображениями','Схемы и макеты нарисованы специально для плана. Они объясняют устройство и проверку системы. Фотоиллюстрация на обложке создана AI и не изображает реальную команду работодателя. Видео открывается по ссылке; для просмотра нужен интернет.')
    b.section('Как работать с AI-помощником','Используй его для объяснений, поиска причин и обсуждения вариантов. Затем измени условие, воспроизведи ключевой фрагмент самостоятельно и объясни поведение при отказе. Сохраняй собственные решения и измерения.')
    b.p('Проверены страницы материалов и их тематика 5-6 сентября 2026 года; все видео целиком не просматривались. Условия доступа, интерфейсы и API меняются: версия проекта фиксируется отдельно.',9.5,color=MUTED)

    b.new('Объём, темп и календарь','Планирование','calendar')
    rows=[['Часть','Работа','С резервом 25%'],['Диагностика','6-8 ч','отдельно'],['Б1-Б4, при пробелах',f'{BASE} ч',f'{BASE*1.25:.0f} ч'],['01-24, основной маршрут',f'{CORE} ч',f'{CORE*1.25:.0f} ч'],['Д1-Д8, все углубления',f'{ADV} ч',f'{ADV*1.25:.0f} ч'],['Всё, включая повторение базы',f'{BASE+CORE+ADV} ч',f'{(BASE+CORE+ADV)*1.25:.0f} ч']]
    b.table(rows,[CW*.49,CW*.23,CW*.28])
    b.p('Резерв - планировочный ориентир, а не гарантия завершения. Если критерий не пройден, модуль продолжается. С нуля в программировании подготовка может потребовать значительно больше 92 часов.',10,space=13)
    rows=[['Часов в неделю','Основной маршрут','Все части'],*[[str(h),f'{math.ceil(CORE/h)}-{math.ceil(CORE*1.25/h)} недель',f'{math.ceil((BASE+CORE+ADV)/h)}-{math.ceil((BASE+CORE+ADV)*1.25/h)} недель'] for h in [10,15,20,30]]]
    b.table(rows,[CW*.28,CW*.36,CW*.36])
    b.section('Рекомендуемый ритм при 20 ч/неделю','4 ч на видео и чтение, 12 ч на реализацию, 3 ч на проверку и разбор ошибок, 1 ч на журнал решений. Это отправная точка; фактическое соотношение корректируется по пробелам.')
    b.section('Пилот имеет календарную длительность','32 часа модуля 23 распределяются примерно на 2-4 недели наблюдения. Малый поток, ожидание доступов, напарника или документов увеличивают срок. Параллельно можно оформлять портфолио, сохраняя различие версий.')
    b.section('Ранние контрольные точки','После 03 - рабочий стенд. После 08 - документ до CRM с review. После 11 - измеряемый RAG. После 15 - диалог и оператор. После 21 - нагрузка, откат и restore. После 23 - отчёт эксплуатации.')
    b.p('Для вакансии с требованием 6+ лет план даёт доказательства навыков и проекта. Коммерческий стаж и ответственность за команду подтверждаются отдельными реальными примерами.',9.5,color=MUTED)

    for part,mods in enumerate([MODULES[:16],MODULES[16:]]):
        b.new('Содержание' if part==0 else 'Содержание: продолжение','Навигация','toc' if part==0 else 'toc2')
        if part==0:
            b.p('Кликабельные названия и закладки PDF ведут к началу модуля. На следующей странице каждого модуля расположены материалы по занятиям.',10,color=MUTED,space=12)
        current=None
        for m in mods:
            if m['track']!=current:b.label(m['track']);current=m['track']
            b.p(b.bookmark_link(anchor(m['code']),f'{m["code"]}. {m["title"]}  /  '),10.3,markup=True,space=8)
        if part==1:
            b.label('Практические приложения')
            for key,title in [('atlas_rag','Атлас схем и макетов'),('metrics','Метрики и экономика'),('templates','Рабочие шаблоны'),('failures','Сценарии отказов'),('interview','Вопросы интервью'),('short','Подготовка за 14 дней'),('glossary','Словарь'),('tracker','Личный трекер'),('resources','Реестр материалов')]:
                b.p(b.bookmark_link(key,title+'  /  '),9.8,markup=True,space=5)

    b.new('Что объединено из двух планов','Полнота программы','merge')
    b.p('Нумерация источников ниже относится к исходным PDF. «Наш план» - 48 страниц; «присланный v2» - 34 страницы. Ссылки обновлены и привязаны к конкретным занятиям.',9.5,color=MUTED,space=10)
    b.table([['Тема','Наш план','Присланный v2','Здесь']]+MERGE_MAP,[CW*.43,CW*.16,CW*.22,CW*.19],size=8.4,pad=4)
    b.p('Исправления: у КПП нет контрольной суммы; OData 1С поддерживает проведение; OCR сохраняет исходный НДС и расхождение; правильная передача оператору отделена от доли автоматизации.',9.5,space=5)

    b.new('Сквозной проект и границы системы','Архитектура','project')
    b.p('Два пути пользователя: вопрос по регламентам и загрузка документа для подготовки действия. Первая версия узкая; новые возможности добавляются по результатам проверки.',10,space=12)
    y=b.y;bw=150;xx=[MARGIN,MARGIN+181,MARGIN+362]
    draw_box(b,xx[0],y,bw,70,'Каналы','Telegram / CRM / web\nодин основной канал',AQUA)
    draw_box(b,xx[1],y,bw,70,'Backend API','Identity, ACL, контракты\noperation_id, версии')
    draw_box(b,xx[2],y,bw,70,'Диалог и review','История, источники\nподтверждение, оператор',AQUA)
    arrow(b,xx[0]+bw,y-35,xx[1],y-35);arrow(b,xx[1]+bw,y-35,xx[2],y-35)
    yy=y-113
    draw_box(b,xx[0],yy,bw,78,'Файлы и БД','Исходники, metadata\nсостояния, outbox\nправа и retention')
    draw_box(b,xx[1],yy,bw,78,'Broker + worker','OCR / extraction\nRAG / eval\nretries, лимиты',AQUA)
    draw_box(b,xx[2],yy,bw,78,'Интеграции','LLM API / local\nCRM / 1С / МойСклад\nсверка результата')
    arrow(b,xx[1]+bw/2,y-70,xx[1]+bw/2,yy);arrow(b,xx[1],yy-39,xx[0]+bw,yy-39);arrow(b,xx[1]+bw,yy-39,xx[2],yy-39)
    b.y=yy-97;b.figures+=1
    b.callout('Контроль на всём пути','API, worker, поиск, кеш, файлы и инструменты используют проверенный tenant. Источник истины, версия и автор подтверждения сохраняются независимо от канала и модели.')
    b.section('Основной стек','Python, FastAPI, Pydantic, SQLAlchemy/Alembic, PostgreSQL/pgvector, Celery и один broker, Docker Compose, pytest. Один доступный LLM API или Ollama, PaddleOCR, одна основная CRM; Qdrant сравнивается измерениями.')
    b.section('Что сделать сначала','Один тип документа с текстовым слоем, несколько полей, один разрешённый вид изменения. Добавить исходник, редактирование, подтверждение версии и фактический результат. Сканы, сложный поиск и второй канал появятся позже.')
    b.section('Российские интеграции','Битрикс24/amoCRM, 1С, МойСклад и n8n присутствуют в маршруте. Глубокая запись сначала реализуется для одного тестового контура, остальные начинаются с чтения и контрактных сценариев. Mock честно обозначается.')

    b.new('Входная диагностика: 6-8 часов','Стартовая работа','diagnostic')
    b.p('Выполни работу без пошагового копирования решения. Разрешено читать документацию и уточнять понятия; после помощи измени условие и объясни поведение.',11,space=13)
    b.bullets(['Создай POST /documents, GET /jobs/{id}, GET /documents/{id}; ограничь размер/тип файла и опиши ошибки.',
    'Храни клиентов, документы, версии и задания в PostgreSQL. Подними пустую БД миграциями и объясни транзакцию.',
    'Создай двух пользователей разных клиентов. Запрети чтение чужого документа, задания и исходного файла через прямой ID.',
    'Добавь Compose, README и проверки: успех, неверный ввод, нет прав. Пересоздай контейнер без потери постоянных данных.',
    'Измени требование: новая версия документа и обязательное поле. Обнови схему/API и объясни судьбу старых данных.'])
    b.table([['Оценка пункта','Рабочий смысл'],['0','Не реализовано или невозможно объяснить.'],['1','Работает после пошаговой помощи; изменение условия пока затрудняет.'],['2','Самостоятельно работает; объясняешь решение, ограничение и отказ.']],[CW*.25,CW*.75])
    b.section('Как выбрать старт','Пробелы в коде ведут в Б1, в HTTP/Git - Б2, данных - Б3, воспроизводимости - Б4. Можно пропускать подтверждённую базу. Ошибки прав исправляются до работы с чужими данными.')
    b.callout('Первый результат сегодня','Выбери один процесс и 10 разрешённых примеров. Создай репозиторий, паспорт задачи и журнал часов. Выполни диагностику, затем запланируй первые пять занятий.')

    b.new('Как вести обучение и доказательства','Рабочая привычка','routine')
    b.flow(['Тема + видео','Своя реализация','Существенный отказ','Отчёт + следующий шаг'],'Каждый цикл заканчивается проверяемым изменением проекта, а не отметкой о просмотре.')
    b.table([['В репозитории','Что сохранять'],['README / docs','Запуск, схема, ADR, контракты, ограничения и версии.'],['datasets / evals','Разрешённые примеры, правила разметки, split, runner, отчёты.'],['tests / evidence','Проверки значимых отказов, трассы, итоговое состояние, замеры.'],['ops / runbooks','Выпуск, откат, backup/restore, инциденты, доступы без секретов.'],['pilot / portfolio','Паспорт, журнал, до/после, handover и личный вклад.']],[CW*.29,CW*.71])
    b.section('Одна карточка эксперимента','Гипотеза → состав данных → изменяемый фактор → версия конфигурации → метрика и знаменатель → результат по случаям → ограничения → решение. Сохраняй отрицательные результаты: они объясняют выбор.')
    b.section('Порог перехода','Переходить дальше можно, когда критерий модуля выполнен и понятен существенный отказ. Если часть требует внешнего доступа, пометь её статус и проверь независимые компоненты, сохранив незакрытый пункт.')
    b.section('Работа с материалами','Видео даёт обзор и пример. Документация определяет API выбранной версии. Схема помогает объяснить взаимодействие. Собственная практика проверяет понимание. Платный сертификат не является обязательным результатом.')
    b.p('Часть курсов и демо использует коммерческие API. В учебном проекте выбирай доступную среду и лимит расходов; локальный вариант допускается только после проверки нужных возможностей.',9.5,color=MUTED)

def atlas(b):
    b.new('Атлас: как проходит запрос RAG','Наглядные объяснения','atlas_rag')
    b.p('Учебная схема. Сверху - подготовка корпуса, снизу - ответ пользователю. Доступ, версия и источник сохраняются на обоих путях.',10,space=14)
    b.label('Подготовка данных')
    b.flow(['Документы + ACL','Парсинг / chunking','Embeddings','Индекс + metadata'],'Идентификатор, страница и версия связывают фрагмент с исходником.')
    b.label('Обработка вопроса')
    y=b.y; bw=150; xs=[MARGIN,MARGIN+181,MARGIN+362]
    for x,t,body in zip(xs,['1. Вопрос','2. Retrieval','3. Reranking'],['Проверенный пользователь\nи tenant','Только разрешённые\nкандидаты','Упорядочить кандидатов\nпо полезности']):draw_box(b,x,y,bw,70,t,body,AQUA)
    arrow(b,xs[0]+bw,y-35,xs[1],y-35);arrow(b,xs[1]+bw,y-35,xs[2],y-35)
    yy=y-110
    for x,t,body in zip(xs,['6. Проверка','5. Генерация','4. Контекст'],['Ответ поддержан\nисточником?','Инструкция + контекст\nОграничение длины','Фрагменты с ID\nи бюджетом токенов']):draw_box(b,x,yy,bw,75,t,body)
    arrow(b,xs[2]+bw/2,y-70,xs[2]+bw/2,yy);arrow(b,xs[2],yy-37,xs[1]+bw,yy-37);arrow(b,xs[1],yy-37,xs[0]+bw,yy-37)
    b.y=yy-92;b.figures+=1
    b.table([['Где ошибка','Что проверять'],['Ответа нет в корпусе','Полнота, актуальность, права и корректный отказ.'],['Источник есть, но не найден','Парсинг, chunking, фильтр, embeddings и retrieval.'],['Источник найден, но ответ неверен','Контекст, инструкции, интерпретация и поддержка утверждения.'],['Ответ верен, ссылка неверна','Соответствие источника, страницы и версии.']],[CW*.38,CW*.62])
    b.p('Видео и документация: '+link('X32')+' · '+link('A09')+' · '+link('A11'),9.5,markup=True)

    b.new('Атлас: проверка документа человеком','Схематичный экран','atlas_ocr')
    b.p('Учебный макет, не скриншот продукта. Все значения вымышлены; цель - показать связь исходника и редактируемых полей.',10,color=MUTED,space=14)
    c=b.c;y=b.y;left=MARGIN;right=MARGIN+270
    c.setFillColor(PALE);c.roundRect(left,y-295,245,295,8,fill=1,stroke=0)
    c.setFillColor(white);c.setStrokeColor(LINE);c.rect(left+18,y-274,209,251,fill=1,stroke=1)
    c.setFont('AB',14);c.setFillColor(NAVY);c.drawString(left+34,y-48,'СЧЁТ  /  ПРИМЕР')
    lines=['Поставщик: Учебная компания','Номер: DEMO-017','Дата: 06.09.2026','Сумма позиций: 12 450,00','Итого в документе: 12 500,00','КПП: 123456789']
    for i,t in enumerate(lines):
        yy=y-79-i*24;c.setFont('AR',9.5);c.setFillColor(INK);c.drawString(left+32,yy,t)
    c.setStrokeColor(AMBER);c.setLineWidth(1.6);c.rect(left+28,y-180,182,40,fill=0,stroke=1);c.setLineWidth(1)
    draw_box(b,right,y,241,295,'Поля и review','',AQUA)
    vals=[('Сумма из документа','12 500,00'),('Контрольная сумма позиций','12 450,00'),('Расхождение','50,00 - проверить'),('КПП','Формат / организация'),('Версия черновика','v3, исходник DEMO-017')]
    yy=y-53
    for k,v in vals:
        c.setFont('AR',8.7);c.setFillColor(MUTED);c.drawString(right+16,yy,k)
        c.setFont('AB',10.3);c.setFillColor(AMBER if k=='Расхождение' else INK);c.drawString(right+16,yy-16,v);yy-=39
    c.setFillColor(TEAL);c.roundRect(right+16,y-279,209,30,5,fill=1,stroke=0);c.setFont('AB',10);c.setFillColor(white);c.drawCentredString(right+120,y-269,'Исправить и подтвердить v3')
    b.y=y-312;b.figures+=1
    b.bullets(['По нажатию на поле показывается фрагмент исходника и страница.',
    'Исходное распознанное значение и исправление сохраняются раздельно.',
    'Подтверждение содержит пользователя, время, версию данных и допустимое действие.',
    'При изменении исходника или суммы старое подтверждение перестаёт разрешать запись.'])
    b.callout('Три разных результата проверки','Валидная JSON-схема; корректность по бизнес-правилам; правильность по исходному документу. Только вместе с правами и подтверждением они позволяют выполнить согласованное действие.')
    b.p('Материалы: '+link('A17')+' · '+link('A18')+' · '+link('X23')+' · '+link('X24'),9.5,markup=True)

    b.new('Атлас: состояния и неизвестный исход','Надёжность','atlas_state')
    b.flow(['queued','running','needs_review','approved vN'],'Каждый переход фиксируется в БД; approval относится к неизменённой версии предложения.')
    b.flow(['executing','Ответ получен?','Сверить CRM','succeeded / review'],'Потеря ответа не означает, что CRM не выполнила действие.')
    b.table([['Событие','Правильная реакция'],['Повтор того же event ID','Найти существующую операцию; не создавать новую.'],['Тот же ключ с другим payload','Отклонить конфликт; не считать безопасным повтором.'],['Старое подтверждение v3, данные v4','Остановить запись и получить новое подтверждение.'],['CRM записала, worker завершился','Проверить внешний ID и состояние до повторной записи.'],['Ошибка после лимита попыток','Остановить автоматический retry, сохранить причину и ручной путь.'],['Пользователь отменил операцию','Проверить фактическую стадию: выполненный внешний эффект не исчезает от смены локального статуса.']],[CW*.43,CW*.57],size=10)
    b.callout('Кто владеет состоянием','Broker знает о доставке. Workflow знает о шагах. Прикладная БД хранит бизнес-операцию. Внешняя CRM хранит собственное фактическое состояние. Их необходимо согласовывать.')
    b.p('Материалы: '+link('B11')+' · '+link('B10')+' · '+link('A15'),9.5,markup=True)

    b.new('Атлас: dashboard результата','Учебный пример','atlas_dashboard')
    b.p('Ниже вымышленные числа для объяснения метрик. Это не результаты готового проекта и не обещанные пороги качества.',10,color=MUTED,space=16)
    y=b.y;cards=[('1 000','операций в когорте'),('900','корректно приняты'),('38,89 ₽','на принятый результат')]
    for i,(num,lbl) in enumerate(cards):
        x=MARGIN+i*174;c=b.c;c.setFillColor(AQUA);c.roundRect(x,y-83,163,83,7,fill=1,stroke=0)
        c.setFont('AB',27);c.setFillColor(TEAL);c.drawString(x+12,y-37,num)
        c.setFont('AR',9);c.setFillColor(MUTED);c.drawString(x+12,y-60,lbl)
    b.y-=108
    b.label('Состав тех же 1 000 операций')
    for name,value,color in [('Принято автоматически',600,TEAL),('Принято после работы человека',300,HexColor('#4f96ac')),('Неуспешно',60,AMBER),('Ещё не завершено',40,HexColor('#9aabb8'))]:
        b.p(f'{name}: {value}',10,space=4);c=b.c;c.setFillColor(PALE);c.rect(MARGIN,b.y-12,CW,12,fill=1,stroke=0);c.setFillColor(color);c.rect(MARGIN,b.y-12,CW*value/1000,12,fill=1,stroke=0);b.y-=24
    b.figures+=1
    b.section('Почему нужны две метрики','Успех когорты на момент отчёта: 900/1000 = 90%. Автоматически принятые операции: 600/1000 = 60%. Участие человека не превращает 300 корректных результатов в неуспех. Незавершённые задачи показываются отдельно.')
    b.section('Что добавить на рабочий экран','p95 полного времени, возраст очереди, доля исправлений критичных полей, повторные попытки, версия модели/prompt/индекса и период наблюдения. По operation ID открывается трасса и фактический результат.')
    b.p('Для цены в этом примере использованы 35 000 ₽ операционных затрат / 900 принятых результатов. Подробный расчёт приведён далее. Материалы: '+link('B27')+' · '+link('B28'),9.5,markup=True)

def metrics_templates(b):
    b.new('Метрики: качество и бизнес-результат','Рабочая справка','metrics')
    b.p('Для каждой метрики сохраняй определение, период/когорту, числитель, знаменатель, размер выборки, версии и исключения. Порог согласуется по цене ошибки и baseline.',10,space=12)
    b.table([['Метрика','Определение и условие'],
      ['Precision / recall / F1','Precision = TP/(TP+FP), recall = TP/(TP+FN). F1 - гармоническое среднее. При нулевом знаменателе заранее задаётся политика; ошибки смотреть по классам.'],
      ['Recall@k','Найденные релевантные элементы среди top-k / все размеченные релевантные элементы. Документ и фрагмент - разные единицы; фиксируй выбранную.'],
      ['MRR','Среднее 1/rank первого релевантного результата; 0, если его нет. Вопросы без возможного ответа выделяются отдельно.'],
      ['Ответ по источнику','Доля оцениваемых ответов, где существенные утверждения поддержаны разрешёнными актуальными источниками. Указывать рубрику и ручную проверку.'],
      ['Корректный отказ','Правильные отказы на вопросах без достаточного разрешённого ответа / такие вопросы. Избыточные отказы на отвечаемых вопросах измеряются отдельно.'],
      ['Правильное поле','Верные значения по эталонной разметке после согласованной нормализации / оцениваемые поля. Критичные поля и пропуски учитывать отдельно.'],
      ['Правильный документ','Документы со всеми требуемыми критичными полями и корректной структурой / оцениваемые документы. Это строже прохождения валидаторов.'],
      ['Принятый результат','Бизнес-операция фактически корректно завершена и принята по правилам процесса. Успешный HTTP 200 или JSON не являются достаточным доказательством.']],[CW*.28,CW*.72],size=9.6)
    b.p('Основа для оценки: '+link('A02')+' · '+link('X11')+' · '+link('A11')+'. Формулы применяются к выбранной единице разметки; отсутствие примеров не означает нулевой риск.',9.5,markup=True)

    b.new('Диалоги, надёжность и неопределённость','Рабочая справка','metrics2')
    b.table([['Показатель','Что именно измерять'],['Решение задачи диалога','Корректный ответ, разрешённое действие, правильный отказ или необходимая эскалация - по заранее заданной рубрике.'],['Доля автоматизации','Диалоги/операции, корректно завершённые без участия человека / выбранная когорта. Не смешивать с общей успешностью.'],['Корректный handoff','Верная передача нужному оператору с контекстом и без потери состояния; отдельно - необоснованные передачи и пропущенные эскалации.'],['Время процесса','Активное время человека и полное время от входа до принятия; p50/p95 и незавершённые случаи отдельно.'],['Надёжность','Доля операций, выполненных по контракту; дубли, потеря состояния, возраст очереди, число попыток и ручное восстановление.'],['RPO / RTO','Сначала целевые значения, затем фактически измеренные потеря данных и время восстановления на тестовом restore.'],['Стоимость принятия','Все затраты за период / корректно принятые результаты за тот же период; неудачные попытки остаются в числителе.']],[CW*.30,CW*.70],size=10)
    b.section('Как сравнивать варианты','Используй одинаковые примеры и учитывай зависимость: документы одного шаблона или клиента не всегда независимы. При bootstrap пересэмплируй подходящие независимые единицы/группы. Покажи разброс и число случаев, а не только процент с двумя знаками.')
    b.section('Как задавать порог','Утечки, запрещённые действия и критичные дубли блокируют переход при обнаружении. Для точности, p95, стоимости и ручной доли согласуй baseline, целевой уровень, исключения и объём проверки. Ноль ошибок в маленьком тесте не доказывает нулевую вероятность.')
    b.p('Материалы: '+link('A03')+' · '+link('B17')+' · '+link('A14'),9.5,markup=True)

    b.new('Полная стоимость и окупаемость','Учебный расчёт','cost')
    b.p('Все числа ниже вымышленные. Это шаблон расчёта, а не стоимость конкретного провайдера или обещание окупаемости.',10,color=MUTED,space=12)
    b.table([['Затраты за месяц','Пример, ₽'],['LLM, включая неудачные попытки','4 000'],['OCR','3 000'],['Инфраструктура','8 000'],['Лицензии','2 000'],['Review и исправления пользователей','12 000'],['Поддержка, инциденты и сопровождение','6 000'],['Операционные затраты','35 000'],['Корректно принятых результатов','900'],['Операционная стоимость результата','35 000 / 900 = 38,89']],[CW*.71,CW*.29])
    b.section('Разработка учитывается отдельно и прозрачно','Если разработка стоила 90 000 ₽ и распределяется на три месяца, добавляется 30 000 ₽/месяц: (35 000 + 30 000)/900 = 72,22 ₽. Показывай обе величины, срок распределения и объём; не смешивай разовую инвестицию и операционный cash flow.')
    b.section('Окупаемость - только по достижимому эффекту','Если сопоставимый ручной процесс реально стоит 60 000 ₽/месяц, операционный эффект примера равен 25 000 ₽/месяц; простой срок возврата 90 000/25 000 = 3,6 месяца. Это условный сценарий без дисконта и налоговых расчётов. При отсутствии реального сокращения расходов отражай высвобождённое время отдельно.')
    b.callout('Чувствительность','Пересчитай модель при меньшем потоке, удвоенном review, дорогих повторах и увеличении поддержки. Решение о внедрении должно переживать реалистичное изменение допущений.')

    b.new('Шаблон: задача и границы проекта','Рабочие документы','templates')
    b.table([['Поле','Что записать'],['Пользователь и процесс','Роль, последняя реальная задача, as-is и владелец результата.'],['Вход и выход','Типы/объём документов, канал, поля, конечное действие, источник истины.'],['Ценность и baseline','Активное время, ошибка, нынешние затраты; источник и дата измерения.'],['Scope / non-goals','Один основной сценарий; какие типы, действия и интеграции пока исключены.'],['Данные и ответственность','Категории данных, доступы, провайдеры, размещение, retention и согласования.'],['Нефункциональные требования','Нагрузка, время, восстановление, изоляция и бюджет эксплуатации.'],['Приёмка','Метрика, числитель/знаменатель, набор, порог, владелец проверки.'],['Оценка','Работы, диапазоны, неизвестные, зависимости, внешние доступы и резерв.'],['Пилот и поддержка','Пользователи, период, ручной путь, stop/go и ответственный за инцидент.'],['Изменение scope','Новое требование, влияние на срок/стоимость/качество и согласованное решение.']],[CW*.28,CW*.72],size=10)
    b.section('Десять вопросов на discovery','Кто выполняет задачу? Как она решается сейчас? Что считается верным? Какова цена ошибки? Какие документы и исключения? Где источник истины? Кто имеет право записи? Какие данные допустимы для модели? Кто поддерживает решение? По чему примут пилот?')
    b.p('Видео: '+link('B31')+' · Схемы: '+link('B16'),9.5,markup=True)

    b.new('Шаблон: ADR, задача и review','Рабочие документы','adr')
    b.label('ADR - одна страница на одно решение')
    b.table([['Часть','Содержание'],['Контекст и статус','Задача, ограничения, предложено/принято/заменено.'],['Критерии','Цена ошибки, время, нагрузка, доступы, поддержка, стоимость.'],['Варианты','Минимум два реалистичных решения, включая простое.'],['Решение и последствия','Почему выбран вариант; обязанности, ограничения, известные отказы.'],['Проверка и пересмотр','Измерения, испытания, допущения и событие для нового обсуждения.']],[CW*.3,CW*.7],size=10)
    b.section('Пример решения','Подтверждённое изменение CRM выполняется worker: внешний API нестабилен, а пользователю нужен сохраняемый статус. Цена выбора - очередь, идемпотентность, сверка неизвестного исхода и поддержка. Альтернатива - синхронная запись; отказ от неё объясняется измеренными тайм-аутами.')
    b.label('Карточка передачи разработчику')
    b.p('Цель → границы → вход/выход → контракт и примеры → данные/доступы → критерии приёмки → значимый отказ → инструкция запуска → срок и зависимости. Оставь исполнителю пространство для решения.',10,space=12)
    b.label('Review и handover')
    b.bullets(['Проверить инварианты БД, полномочия, повторы и неизвестный исход.',
    'Проверить понятность тестов, миграций, логов и восстановления.',
    'Разделить обязательные исправления и предпочтения оформления.',
    'Принять успешный путь и существенный сбой; дать другому человеку повторить запуск.'],10)
    b.p('Материалы: '+link('X31')+' · '+link('B18')+' · '+link('B32'),9.5,markup=True)

    b.new('Шаблон: паспорт и отчёт пилота','Рабочие документы','pilot')
    b.table([['Перед запуском','Заполнить'],['Гипотеза','Какая операция станет быстрее/точнее и для кого.'],['Границы','Один сценарий, пользователи, разрешённые данные, интеграция и версия.'],['Baseline и критерии','Как получены исходные измерения; пороги качества, времени и стоимости.'],['Поддержка и stop','Владелец процесса, ответственный разработчик, ручной путь и условия остановки.'],['Наблюдение','Период, требуемый поток, журнал операций и правила фиксации инцидента.']],[CW*.31,CW*.69],size=10)
    b.label('Журнал одного наблюдения')
    b.p('Дата • operation ID • тип задачи • версия системы • фактический результат • активное время человека • исправления • расходы • причина ошибки • trace • решение и владелец.',10,space=13)
    b.label('Отчёт закрытия')
    b.bullets(['Состав задач, число пользователей/операций, пропуски и ограничения выборки.',
    'Сравнение с baseline на сопоставимых случаях; ручная работа и поддержка включены.',
    'Ошибки по категориям, инциденты, исправления и остаточные риски.',
    'Go / no-go / продлить наблюдение с объяснением, а не только общий балл.',
    'Передача: запуск, доступы, версии, backup, восстановление, мониторинг, контакты ответственных.'],10)
    b.callout('Если реального клиента пока нет','Проведи учебную эксплуатацию с другими людьми и обозначь её как репетицию. Сохрани методику и результаты, но не представляй их коммерческим внедрением.')
    b.p('Материалы: '+link('B17')+' · '+link('B31')+' · '+link('X10'),9.5,markup=True)

FAILURES=[
 ('Дублирующий webhook','Одна операция и одно согласованное изменение; существующий результат доступен по тому же ключу.'),
 ('Ключ прежний, payload другой','Конфликт отклонён; старое действие не подтверждает новое содержимое.'),
 ('Два пользователя меняют документ','Проверка версии предотвращает потерю обновления; конфликт виден человеку.'),
 ('Подтверждение устарело','Новая версия требует нового review; старая не разрешает запись.'),
 ('Worker остановлен до/после CRM','Состояние сохранено; неизвестный внешний исход сверяется перед повтором.'),
 ('БД commit, публикация не выполнена','Outbox/relay восстанавливает доставку; задание не теряется.'),
 ('ACK потерян и задача доставлена снова','Повтор не создаёт дублирующий бизнес-эффект в проверенном контракте.'),
 ('Постоянно падающая задача','Ограниченный retry, изоляция, причина и управляемый ручной replay.'),
 ('LLM: 429, тайм-аут, обрыв','Ограниченные ожидания и попытки, статус, контроль бюджета и ручной путь.'),
 ('Один клиент создаёт большой поток','Квоты и параллелизм ограничивают влияние; задержка остальных измеряется.'),
 ('Документ содержит prompt injection','Недоверенный текст не получает полномочий; запрещённые tools/URL блокируются.'),
 ('Запрос чужого документа/истории','Запрет до выдачи данных и передачи контекста модели, включая кеш и worker.'),
 ('Смена модели/prompt ухудшила ответ','Eval/мониторинг выявляет регрессию; есть ограниченный выпуск и возврат версии.'),
 ('Сеть оборвалась при handoff','Оператор и бот имеют согласованное состояние; сообщение не теряется и не дублируется.'),
 ('Удаление и восстановление backup','Политика хранения/удаления сохраняется; восстановление не возобновляет запрещённую обработку.'),
 ('Новый релиз при старых заданиях','Версии данных/контрактов совместимы или есть явный путь миграции/завершения.')]

INTERVIEW=[
 ('Как перейти от «нужен AI» к решению?','Уточнить процесс, пользователя, данные, цену ошибки, права, объём и поддержку. Сформулировать scope, baseline, критерии пилота и простую альтернативу до выбора модели.'),
 ('Когда правила, ML, RAG, fine-tuning или агент?','Правила - для точной логики; ML - по измеренному baseline; RAG - для внешнего контекста; обучение - под подтверждённое изменение поведения; агент - когда нужен контролируемый выбор шагов.'),
 ('Почему RAG ошибается?','Разделить парсинг, chunking, полноту/права, retrieval, reranking, контекст и генерацию. Показать конкретный вопрос, ожидаемый источник и причину промаха.'),
 ('Как оценить LLM-as-a-judge?','Согласовать рубрику, сравнить с человеческой разметкой, разобрать расхождения, сохранить версии и учитывать смещения. Средний балл дополняется покейсными и критичными проверками.'),
 ('Как извлекать реквизиты?','Схема, неизвестные поля, источник/координаты, нормализация, проверка смысловых связей и review. КПП без checksum; НДС сохраняется и сверяется, а не подменяется.'),
 ('Как избежать дубля в CRM?','Устойчивый operation key, уникальность, допустимая внешняя идемпотентность и сверка неизвестного исхода. Разобрать остановку после внешней записи до локального commit.'),
 ('Зачем outbox?','Локальная транзакция сохраняет данные и событие вместе. Relay может повторять публикацию, поэтому consumer дедуплицирует. Внешний побочный эффект требует отдельной защиты.'),
 ('Что делать с устаревшим approval?','Связать подтверждение с конкретной версией и параметрами. При изменении данных остановить запись и повторить review; пользовательское нажатие не отменяет проверку сервера.'),
 ('Как защититься от prompt injection?','Данные не получают полномочий. Ограничить инструменты, аргументы, сеть и роль; проверять авторизацию вне модели, значимые действия подтверждать и испытывать атаки через документы.'),
 ('Как изолировать клиентов?','Проверенный tenant проходит через API, worker, поиск, кеш, файлы и аудит. Отрицательные проверки с реальными ролями БД и административными путями подтверждают границы.'),
 ('Как выбирать провайдера?','Сравнить допустимость данных, доступность, русский язык, capabilities, качество на своём наборе, latency и TCO. Резервная модель проходит те же проверки.'),
 ('Когда нужен собственный сервис 1С?','Стандартный OData поддерживает операции, включая проведение. Собственный HTTP-сервис выбирается по составной бизнес-операции, контракту и дополнительным правилам конкретной конфигурации.'),
 ('Какие метрики нужны бизнесу?','Принятые результаты, активное время, исправления, ручная доля и полная стоимость. Корректная передача оператору может быть успешным исходом; автоматизация измеряется отдельно.'),
 ('Как выпускать AI-изменения?','Версии кода, модели, промпта, индекса и данных; eval, ограниченный выпуск, мониторинг, откат и совместимость незавершённых заданий. Восстановление подтверждается практикой.'),
 ('Чем подтверждается лидерство?','Декомпозиция, передача задачи, ревью, согласование компромисса и результат команды. Показывать личный вклад и происхождение опыта, включая учебное сотрудничество.'),
 ('Что доказывает пилот?','Наблюдаемый эффект на ограниченном процессе при конкретных данных, пользователях и версиях. Называть число случаев, ограничения, поддержку, ошибки и основания go/no-go.')]

def checks_interview(b):
    for k in range(2):
        b.new(f'Испытания отказов: {k*8+1}-{k*8+8}','Приёмка системы','failures' if k==0 else 'failures2')
        b.p('Проводить на тестовом стенде с разрешёнными данными. Для каждого сценария: вход, момент сбоя, ожидаемый инвариант, фактическое состояние, trace и результат.',10,space=12)
        b.table([['Сценарий','Ожидаемый результат']]+[[f'{i+1}. {s}',r] for i,(s,r) in enumerate(FAILURES) if k*8<=i<k*8+8],[CW*.39,CW*.61],size=10,pad=8)
        b.callout('Доказательство','Приложи конечное состояние БД и внешней системы, журнал и измерение. Одно сообщение «тест прошёл» не показывает, что сохраняется бизнес-инвариант.')
    for k in range(2):
        b.new(f'Собеседование: вопросы {k*8+1}-{k*8+8}','Защита решений','interview' if k==0 else 'interview2')
        b.p('Отвечай через собственный пример: ограничение → решение → проверка → результат. Ниже опорные пункты, а не текст для заучивания.',9.8,color=MUTED,space=12)
        for i,(q,a) in enumerate(INTERVIEW):
            if k*8<=i<k*8+8:
                b.p(f'{i+1}. {q}',10.5,True,space=4);b.p(a,10,space=10)
    b.new('Если интервью скоро: 14 дней','Короткий маршрут','short')
    b.p('Эта ветка нужна для уже отправленного отклика при имеющейся базе. Она готовит демонстрацию и разговор; основной маршрут и опыт эксплуатации остаются отдельными задачами.',10,space=12)
    b.table([['Дни','Конкретная работа'],['1-2','Входная проверка, разбор вакансии, вопросы о команде и один выбранный процесс.'],['3-4','API, схема контекста, путь данных, 10-20 разрешённых документов и выбранная модель.'],['5-6','Один RAG с источниками либо extraction; выбрать то, что можешь объяснить и проверить.'],['7-8','Фиксированный eval, ошибки, latency и расходы; числа имеют знаменатель и ограничения.'],['9-10','Review, CRM-контракт, дубль, отказ модели и запрет чужого ID; fake явно отмечен.'],['11-12','README, ADR, runbook, ограничения и demo 7-10 минут.'],['13-14','Две репетиции, три истории личного решения, вопросы работодателю.']],[CW*.17,CW*.83],size=10)
    b.section('Первые 90 дней после выхода','Дни 1-30: обследовать процесс, данные, доступы и существующий backend, согласовать baseline и pilot scope. Дни 31-60: доставить ограниченный сценарий, измерить качество и провести совместное review. Дни 61-90: завершить наблюдение пилота, передать поддержку и предложить следующий объём по результатам.')
    b.section('Что уточнить у компании','Доля backend и обучения моделей; типы документов; текущие CRM/1С; размер команды; допустимые провайдеры; поддержка и дежурства; готовность данных; критерии успешного пилота; результат первых трёх месяцев.')

def glossary_tracker(b):
    for k in range(2):
        b.new('Словарь: '+('основа' if k==0 else 'эксплуатация'),'Справка','glossary' if k==0 else 'glossary2')
        b.table([['Термин','Рабочий смысл']]+[list(x) for x in GLOSSARY[k*12:(k+1)*12]],[CW*.3,CW*.7],size=10,pad=9)
        b.callout('Проверка понимания','Для каждого термина найди конкретное место в своём проекте и объясни, что произойдёт при его неправильном использовании.')
    for k in range(3):
        b.new(f'Личный трекер: часть {k+1}','Прогресс','tracker' if k==0 else f'tracker{k+1}')
        b.p('Заполняй после практической проверки: дата, фактические часы, commit/отчёт и оставшийся пробел. Статусы: не начато / в работе / доработать / проверено.',10,space=12)
        b.table([['Модуль','Дата / часы / доказательство','Следующий пробел']]+[[m['code']+'. '+m['title'],'',''] for m in MODULES[k*12:(k+1)*12]],[CW*.44,CW*.31,CW*.25],size=9.2,pad=7,blankheight=39)
        b.p('Следующие пять занятий: __________________________________________________________',10,space=17)
        b.p('Что мешает переходу: ____________________________________________________________',10,space=17)
        b.p('Дата следующей проверки: ________________________________________________________',10)

def registry(b):
    # A full source registry keeps access conditions and verification separate from learning hours.
    for start in range(0,len(USED),8):
        b.new('Реестр материалов'+('' if start==0 else f': {start+1}-{min(start+8,len(USED))}'),'Видео и первичные источники','resources' if start==0 else f'resources{start}',outline=start==0)
        if start==0:b.p('Коды совпадают с карточками занятий. Проверка относится к странице и тематике материала, а не к полному просмотру видео или успешной оплате/регистрации. Старые записи используются для принципов; API сверяется с docs.',9.3,color=MUTED,space=10)
        for code in USED[start:start+8]:
            s=SOURCES[code];kind='ВИДЕО / КУРС' if 'video' in s['kind'] else ('ИНТЕРАКТИВНЫЙ КУРС' if s['kind']=='course' else 'ДОКУМЕНТАЦИЯ / ПЕРВОИСТОЧНИК')
            b.p(link(code),10,markup=True,space=3)
            b.p(kind+' · '+s.get('language','EN').upper()+' · '+re.sub(r'^https?://','',s['url']).split('/')[0],8.2,color=TEAL,space=3)
            b.p(s.get('access','Открытая страница.'),8.6,color=MUTED,space=3)
            check=s.get('verification','Страница проверена.')
            # Keep exact qualification for 403/index-only and catalog links; ordinary confirmations can be compact.
            if len(check)>200 and not any(x in check for x in ['403','индекс','2020','каталог']):check='Проверены первичная страница, название и тематика. Полное воспроизведение видео не проверялось.'
            b.p(check,8.5,color=MUTED,space=11)

def provenance(b):
    b.new('Происхождение, изображения и старт','О документе','provenance')
    b.section('Два исходных документа','Объединены AI_ML_Architect_Полный_план_Сурен_v2.pdf (48 страниц, 18 модулей) и AI_ML_Architect_План_изучения_Сурен_v2.pdf (34 страницы, 21 модуль), а также предоставленное описание вакансии Интерика Лаб. Таблица соответствия находится в начале программы.')
    b.section('Что изменено','Архитектура, discovery, CI и очередь перенесены к началу. Российские данные, диалоги, клиентская работа и интеграции сохранены. Финальный пилот проводится. Уточнены КПП, OData, НДС, handoff-метрики, стоимость ручного труда и восстановление после удаления.')
    b.section('Иллюстрации','Все схемы, графики и макеты в документе созданы специально для этого плана. Это учебные объяснения, не реальные замеры и не скриншоты внедрённых продуктов. Фотоиллюстрация обложки создана встроенным инструментом image_gen, без использования фотографий реальной команды.')
    b.p('Сохранённый фотоактив: '+str(PHOTO).replace('\\','/'),8.9,color=MUTED,space=9)
    b.p('Промпт фотоиллюстрации (содержательная часть):',9.5,True,space=5)
    b.p('Create one elegant editorial photographic triptych, a single 3:1 image split into three equally sized vertical scenes with narrow neutral dividers. Left: developer hands at laptop with abstract code editor and notebook with boxes and arrows. Center: generic invoices and scanner on office desk, one hand checking a page. Right: three adult software colleagues reviewing a monitor and sketching architecture on glass. Natural photographs, subtle navy/teal accents, soft daylight, matte texture. No readable words, brands, watermarks, holograms or robot faces. Fictional staged scenes, not photographs of an actual company.',9.2,leading=12.4,space=10)
    b.p('Полный исходный промпт и реестр источников сохранены вместе с рабочими материалами сборки. На обложке использован один сгенерированный файл без подмены его фотографией реального внедрения.',9,color=MUTED,space=12)
    b.callout('Следующее действие','Пройди входную диагностику, выбери один процесс и 10 разрешённых примеров. Назначь первые пять занятий. Первый рабочий стенд должен появиться в модуле 03, первый полный бизнес-сценарий - в 08.')

def build(path,old=None,total=None):
    b=Book(path,old,total);front(b)
    for m in MODULES:b.module(m)
    atlas(b);metrics_templates(b);checks_interview(b);glossary_tracker(b);registry(b);provenance(b);b.save();return b

if __name__=='__main__':
    first=build(TMP/'universal_firstpass.pdf')
    book=build(PDF,first.pages,first.n)
    assert book.n==first.n
    meta={'pdf':str(PDF),'pages':book.n,'modules':len(MODULES),'lessons':SESSIONS,'core_hours':CORE,'foundation_hours':BASE,'advanced_hours':ADV,'sources':len(USED),'video_entries':sum('video' in SOURCES[s]['kind'] for s in USED),'unique_urls':len({SOURCES[s]['url'] for s in USED}),'figures':book.figures,'page_map':book.pages,'bottom_positions':book.bounds}
    (TMP/'universal_build_meta.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'assets/universal_sources_registry.json').write_text(json.dumps({s:SOURCES[s] for s in USED},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in meta.items() if k not in ('page_map','bottom_positions')},ensure_ascii=False,indent=2))
