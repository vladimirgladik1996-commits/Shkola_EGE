"""Шаги Вишенки в «Чате с ИИ»: мышление и работа идут отдельными сообщениями диалога
(новое под старым), готовый ответ — последним сообщением, и он же уходит в Telegram.

Логика живёт в docs/index.html — поэтому тест вырезает настоящие функции из страницы
и прогоняет их на node: события стрима /api/chat превращаются в список сообщений диалога.
Запуск: python3 tests/test_chat_steps.py  (без node — честный пропуск)
"""
import json, os, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()


def ok(m):
    print("  ✓", m)


steps = html[html.index("const CH_ST={"):html.index("function chUpd(")]   # chLabel/chStep/chStepNew/chStepCur/chStepEnd/chThink/chReadLabel
a = html.index("async function chAsk(){")                                  # сам прогон стрима: шаги → ответ последним
chask = html[a:html.index("}   // ушли из раздела") + 1]
assert "function chThink(" in steps and "function chStepNew(" in steps and "const answer=text=>" in chask
ok("логика шагов и ответа вырезана из docs/index.html")

HARNESS = r"""
let CH=[],chBusy=false,chFmt=0,chDeep=false,chAtt=[],chRec=null;
const CH_IDLE=60000;   /* потолок тишины стрима — как в docs/index.html (см. apiStream) */
const CH_FMT=["text","docx","pdf","image"],tab="ch",esc=s=>String(s),chSave=()=>{},chUpd=()=>{};
let EVENTS=[],SNAPS=[];
const show=()=>JSON.stringify(CH.map(m=>({r:m.r,step:!!m.step,st:m.st||"",lbl:m.lbl||"",t:m.t,tg:m.tg||0,err:m.err||0,live:!!m.live,a:(m.a||[]).map(x=>x.n)})));
async function apiStream(p,b,onEv){for(const e of EVENTS){onEv(e);SNAPS.push(show())}}
""" + steps + r"""
""" + chask + r"""
(async()=>{
  /* 1) обычный вопрос: читает вложение → думает (по абзацам) → пишет → ответ последним (+ в Telegram) */
  CH.push({r:"u",t:"привет",a:[{n:"Требования Fipi.ru.pdf",k:"doc"}]});
  EVENTS=[{t:"status",v:"read"},{t:"think",v:"Сначала подумаю.\n\n"},{t:"think",v:"И ещё мысль."},
          {t:"status",v:"write"},{t:"text",v:"Ответ: "},{t:"text",v:"42"},{t:"done",reply:"Ответ: 42"},{t:"tg"}];
  chFmt=0; SNAPS=[]; await chAsk();
  console.log("RUN1:"+show()); console.log("SNAPS1:"+JSON.stringify(SNAPS));
  /* 2) инфографика: шаги конвертации → готовый файл последним сообщением */
  CH=[]; CH.push({r:"u",t:"инфографика",a:[{n:"фото.jpg",k:"img"}]});
  EVENTS=[{t:"status",v:"read"},{t:"status",v:"pdf"},{t:"status",v:"png"},
          {t:"done",reply:"Готово: инфографика.",file:{name:"Тема.png",kind:"image",sent:true,img:"data:image/png;base64,AAA"}},
          {t:"tg"}];
  chFmt=3; await chAsk();
  console.log("RUN2:"+show());
  /* 3) сбой: шаги остаются, честная ошибка — последним сообщением */
  CH=[]; CH.push({r:"u",t:"вопрос"});
  EVENTS=[{t:"status",v:"think"},{t:"think",v:"мысль"},{t:"fail",why:"net"}];
  chFmt=0; await chAsk();
  console.log("RUN3:"+show());
})();
"""

node = shutil.which("node")
if not node:
    print("  - node не найден — шаги фронтенда не проверены (поставьте node.js)")
    sys.exit(0)
r = subprocess.run([node, "-e", HARNESS], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
out = {ln.split(":", 1)[0]: json.loads(ln.split(":", 1)[1]) for ln in r.stdout.splitlines() if ln.startswith(("RUN", "SNAPS"))}

# --- 1) мысли и работа — сообщениями: новое под старым, ответ — в самом конце ---
run1 = out["RUN1"]
assert [m["lbl"] for m in run1[1:4]] == ["читает Требования Fipi.ru.pdf", "думает", "думает"], run1
ok("каждая операция — своё сообщение от имени Вишенки («Вишенка читает Требования Fipi.ru.pdf», «Вишенка думает»…)")
assert [m["t"] for m in run1[2:4]] == ["Сначала подумаю.", "И ещё мысль."] and all(m["step"] for m in run1[1:4])
ok("мысли Кими стримятся по абзацам: новая мысль — новым сообщением под старой")
snaps = [json.loads(s) for s in out["SNAPS1"]]          # каждый снимок — JSON-строка списка сообщений
w = [s for s in snaps if any(m["lbl"] == "пишет ответ" for m in s)]
assert w and any(m["lbl"] == "пишет ответ" and m["t"] == "Ответ: " and m["live"] for s in w for m in s)
ok("пока ответ пишется — видно шаг «Вишенка пишет ответ» со стримом текста")
ans = run1[-1]
assert ans["r"] == "a" and ans["t"] == "Ответ: 42" and not ans["step"] and ans["tg"] == 1 and not ans["lbl"], ans
ok("готовый ответ — последним сообщением диалога, помечен как доставленный в Telegram")
assert not [m for m in run1 if m["step"] and m["st"] == "write"]
ok("шаг «пишет ответ» не дублируется с ответом — текст остаётся только в ответе")

# --- 2) конвертация видна по шагам, файл — последним сообщением ---
run2 = out["RUN2"]
assert [m["lbl"] for m in run2 if m["step"]] == ["смотрит фото", "конвертирует HTML в PDF", "рисует инфографику"], run2
ok("работа показана списком: «Вишенка смотрит фото», «Вишенка конвертирует HTML в PDF», «Вишенка рисует инфографику»")
last2 = run2[-1]
assert not last2["step"] and last2["t"] == "Готово: инфографика." and last2["a"] == ["Тема.png"] and last2["tg"] == 1
ok("для файла готовый ответ — последним сообщением (и ушёл в Telegram)")

# --- 3) сбой не превращается в падение ---
run3 = out["RUN3"]
assert run3[-1]["err"] == 1 and run3[-1]["t"].startswith("ИИ сейчас не отвечает") and [m["t"] for m in run3 if m["st"] == "think"] == ["мысль"]
ok("сбой: шаги видны, последним — честное сообщение об ошибке с кнопкой повтора")
print("test_chat_steps: OK")
