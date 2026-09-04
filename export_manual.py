# -*- coding: utf-8 -*-
"""Выгрузка очереди в HTML-страницу для ручной отправки.

Каждая строка: ссылка на чат в Telegram, кнопка копирования письма и
кнопка открытия резюме под эту вакансию. Отметки «отправлено» хранятся
в localStorage — переживают перезагрузку страницы.

    python export_manual.py
    → jobhunter_отправка.html  (открыть двойным кликом)
"""
import html
import sys
from pathlib import Path

from sqlalchemy import select

from jobhunter.config import ROOT
from jobhunter.db import session_scope
from jobhunter.models import Application, ContactKind, Job, Status

OUT = ROOT / "jobhunter_отправка.html"

CSS = """
:root{--ink:#14181f;--muted:#5b6472;--accent:#1f4e79;--rule:#dfe5ec;--bg:#f6f8fb;
      --ok:#1a7f37;--warn:#9a6700}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:14.5px/1.55 -apple-system,Segoe UI,Roboto,sans-serif}
header{background:#fff;border-bottom:1px solid var(--rule);padding:16px 24px;
       position:sticky;top:0;z-index:10;display:flex;gap:20px;align-items:center;flex-wrap:wrap}
h1{margin:0;font-size:18px;color:var(--accent)}
.stat{color:var(--muted);font-size:13px}
.wrap{max-width:1080px;margin:0 auto;padding:20px 24px 80px}
.card{background:#fff;border:1px solid var(--rule);border-radius:12px;
      padding:16px 18px;margin-bottom:14px;transition:.15s}
.card.done{opacity:.45;background:#f3f6f4}
.top{display:flex;align-items:flex-start;gap:14px;flex-wrap:wrap}
.score{font-weight:700;font-size:19px;color:var(--accent);min-width:38px}
.title{font-weight:600;flex:1;min-width:240px}
.tag{background:#eef2f7;border-radius:6px;padding:2px 9px;font-size:12px;color:var(--muted)}
.msg{background:#f7f9fc;border:1px solid var(--rule);border-radius:8px;
     padding:11px 13px;margin:11px 0;font-size:13.5px;white-space:pre-wrap}
.row{display:flex;gap:9px;flex-wrap:wrap;align-items:center}
.btn{display:inline-flex;align-items:center;gap:6px;background:var(--accent);color:#fff;
     border:0;border-radius:8px;padding:9px 15px;font-size:13.5px;cursor:pointer;
     text-decoration:none;font-family:inherit}
.btn:hover{filter:brightness(1.1)}
.btn.ghost{background:#fff;color:var(--accent);border:1px solid var(--accent)}
.btn.ok{background:var(--ok)}
.btn.copied{background:var(--ok)}
.chk{margin-left:auto;display:flex;align-items:center;gap:7px;font-size:13px;color:var(--muted)}
.chk input{width:17px;height:17px;cursor:pointer}
.hint{background:#fff8e6;border:1px solid #f0dfae;border-radius:10px;
      padding:13px 16px;margin-bottom:18px;font-size:13.5px}
.sec{margin:26px 0 12px;font-size:15px;color:var(--accent);font-weight:600}
"""

JS = """
const KEY='jobhunter_sent_v1';
const done=new Set(JSON.parse(localStorage.getItem(KEY)||'[]'));
function save(){localStorage.setItem(KEY,JSON.stringify([...done]));count();}
function count(){
  const t=document.querySelectorAll('.card').length;
  document.getElementById('cnt').textContent=done.size+' из '+t;
}
function toggle(id,el){
  const card=document.getElementById('c'+id);
  if(el.checked){done.add(id);card.classList.add('done');}
  else{done.delete(id);card.classList.remove('done');}
  save();
}
function copy(id,btn){
  const t=document.getElementById('m'+id).textContent;
  navigator.clipboard.writeText(t).then(()=>{
    const old=btn.textContent;btn.textContent='Скопировано';btn.classList.add('copied');
    setTimeout(()=>{btn.textContent=old;btn.classList.remove('copied');},1400);
  });
}
window.addEventListener('DOMContentLoaded',()=>{
  done.forEach(id=>{
    const c=document.getElementById('c'+id),k=document.getElementById('k'+id);
    if(c){c.classList.add('done');}if(k){k.checked=true;}
  });
  count();
});
"""


def build() -> Path:
    with session_scope() as sess:
        rows = sess.scalars(
            select(Application)
            .where(Application.status.in_([Status.PENDING_APPROVAL.value,
                                           Status.APPROVED.value]))
            .order_by(Application.score.desc())).all()

        tg, mail, seen = [], [], set()
        for a in rows:
            j = sess.get(Job, a.job_id)
            if not j:
                continue
            item = {
                "id": a.id, "score": a.score, "tag": j.tag or "",
                "title": j.title or j.tag or "(без названия)",
                "msg": a.message_body or "",
                "cv": a.cv_path or "",
                "source": j.source.split(":")[0],
            }
            if j.contact_kind == ContactKind.USER_HANDLE.value and j.contact_handle:
                h = j.contact_handle.lower()
                if h in seen:
                    continue
                seen.add(h)
                item["handle"] = j.contact_handle
                tg.append(item)
            elif j.contact_kind == ContactKind.EMAIL.value:
                item["email"] = (j.contact_url or "").replace("mailto:", "")
                mail.append(item)

    def card(it, kind):
        cv_link = ""
        if it["cv"] and Path(it["cv"]).exists():
            url = Path(it["cv"]).as_uri()
            cv_link = ('<a class="btn ghost" href="%s" target="_blank">Резюме</a>'
                       % html.escape(url))
        if kind == "tg":
            # текст в ссылку не кладём: Telegram Desktop его часто теряет,
            # надёжнее скопировать кнопкой и вставить в поле
            open_btn = ('<a class="btn" href="https://t.me/%s" target="_blank">'
                        'Открыть чат @%s</a>'
                        % (html.escape(it["handle"]), html.escape(it["handle"])))
        else:
            subj = "Отклик: %s — Акопян Сурен" % it["title"][:60]
            open_btn = ('<a class="btn" href="mailto:%s?subject=%s">Написать %s</a>'
                        % (html.escape(it["email"]),
                           html.escape(subj.replace(" ", "%20")),
                           html.escape(it["email"])))
        return (
            '<div class="card" id="c%d">'
            '<div class="top"><div class="score">%.0f</div>'
            '<div class="title">%s<br><span class="tag">%s</span> '
            '<span class="tag">%s</span></div>'
            '<label class="chk"><input type="checkbox" id="k%d" '
            'onchange="toggle(%d,this)"> отправлено</label></div>'
            '<div class="msg" id="m%d">%s</div>'
            '<div class="row">%s'
            '<button class="btn ghost" onclick="copy(%d,this)">Копировать текст</button>'
            '%s</div></div>'
            % (it["id"], it["score"], html.escape(it["title"]),
               html.escape(it["tag"]), html.escape(it["source"]),
               it["id"], it["id"], it["id"], html.escape(it["msg"]),
               open_btn, it["id"], cv_link))

    body = [
        '<div class="hint"><b>Как отправлять:</b> «Копировать текст» → '
        '«Открыть чат» → вставить (Ctrl+V) → Enter. Резюме прикладывать '
        '<b>после ответа</b> — вложение в первом сообщении незнакомому '
        'человеку повышает риск жалобы на спам. Отмечай галочкой отправленные, '
        'отметки сохраняются.</div>',
        '<div class="sec">Telegram — %d контактов</div>' % len(tg),
    ]
    body += [card(it, "tg") for it in tg]
    if mail:
        body.append('<div class="sec">Email — %d адресов</div>' % len(mail))
        body += [card(it, "mail") for it in mail]

    page = (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<title>jobhunter — отправка</title>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>%s</style></head><body>"
        "<header><h1>Отклики к отправке</h1>"
        "<span class='stat'>Telegram: %d &nbsp;·&nbsp; Email: %d &nbsp;·&nbsp; "
        "отмечено: <b id='cnt'>0</b></span></header>"
        "<div class='wrap'>%s</div><script>%s</script></body></html>"
        % (CSS, len(tg), len(mail), "".join(body), JS))

    OUT.write_text(page, encoding="utf-8")
    return OUT


if __name__ == "__main__":
    p = build()
    print("Готово: %s" % p)
    sys.exit(0)
