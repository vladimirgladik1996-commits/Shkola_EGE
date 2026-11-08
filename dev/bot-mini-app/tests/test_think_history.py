"""Рассуждения Вишенки в «Чате с ИИ» переживают выход из мини-аппа и АПК (одна страница).

История чата живёт в localStorage.chHist, но раньше при сохранении шаги вырезались
(CH.filter(m=>!m.err&&!m.live&&!m.step)) — а рассуждения это шаги st:"think": возврат в
мини-апп/АПК (перезагрузка страницы) их съедал. Плюс сохранение шло только в начале и в
конце запроса — уход посреди стрима терял всё накопленное.

Стало: рассуждения сохраняются шагами «думает» (до 32 последних, включая недописанные),
восстанавливаются блоками «💜 Вишенка думает…», досохраняются на ходу раз в 1,5 с и на
pagehide; сбрасываются только кнопкой «Создать новый чат». В историю для модели шаги по-
прежнему не попадают (фильтр !m.step в chAsk). Логика сохранения/восстановления вырезается
из docs/index.html и прогоняется на node.

Запуск: python3 tests/test_think_history.py  (без node — честный пропуск)
"""
import json, os, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()


def ok(m):
    print("  ✓", m)


# ---------- 1. статический контракт страницы ----------
assert "setInterval(chSave,1500)" in html, "шаги стрима должны досохраняться на ходу раз в 1,5 с"
assert html.count("clearInterval(chAsk.tick)") >= 2, "таймер автосохранения гасится и в finish(), и при ошибке"
assert 'addEventListener("pagehide",chSave)' in html, "уход из мини-аппа/АПК посреди стрима — досохраняем накопленное"
assert "CH=[];chSave();" in html[html.index("function chNew("):], "история (включая рассуждения) сбрасывается только кнопкой «Создать новый чат»"
assert "const ok=CH.filter(m=>!m.err&&!m.live&&!m.step)" in html, "история для модели остаётся без шагов — рассуждения не уходят в промт"
ok("страница: автосохранение раз в 1,5 с (+pagehide), сброс — только «Создать новый чат», модель не получает шаги")

# ---------- 2. живое поведение: настоящий chSave + восстановление из страницы ----------
save = html[html.index("const chSave="):html.index("const CH_SVG=")]
restore = html[html.index("try{const h=JSON.parse(localStorage.chHist"):html.index("const chSave=")]
assert "s:\"think\"" in save and "m.s==\"think\"" in restore, "рассуждения сохраняются и восстанавливаются шагами «думает»"

node = shutil.which("node")
if not node:
    print("  - node не найден — сохранение/восстановление не проверены (поставьте node.js)")
    sys.exit(0)

HARNESS = r"""
const localStorage={},addEventListener=()=>{};let CH=[];
function chRestore(){""" + restore + r"""}
""" + save + r"""
const show=()=>JSON.stringify(CH.map(m=>({r:m.r,step:!!m.step,st:m.st||"",lbl:m.lbl||"",t:m.t,live:!!m.live})));
/* 1) уход посреди стрима: живая (недописанная) мысль тоже сохраняется; шаги работы и ошибки — нет */
CH=[{r:"u",t:"вопрос"},
    {r:"a",step:1,st:"read",lbl:"читает материалы",t:"читаю"},
    {r:"a",step:1,st:"think",lbl:"думает",t:"первая мысль"},
    {r:"a",step:1,st:"think",t:"вторая мысль",live:1},
    {r:"a",t:"ошибка",err:1}];
chSave();const raw=JSON.stringify(JSON.parse(localStorage.chHist));
CH=[];chRestore();
console.log("R1:"+show());console.log("RAW1:"+raw);
/* 2) потолок: сохраняются последние 32 шага рассуждений */
CH=[{r:"u",t:"вопрос"}];for(let i=0;i<40;i++)CH.push({r:"a",step:1,st:"think",t:"мысль "+i});
chSave();CH=[];chRestore();
console.log("R2:"+JSON.stringify(CH.map(m=>m.t)));
/* 3) в историю для модели рассуждения не попадают */
console.log("R3:"+JSON.stringify(CH.filter(m=>!m.err&&!m.live&&!m.step).map(m=>m.t)));
"""
r = subprocess.run([node, "-e", HARNESS], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
out = {}
for ln in r.stdout.splitlines():
    k, _, v = ln.partition(":")
    if k in ("R1", "R2", "R3"):
        out[k] = json.loads(v)
    elif k == "RAW1":
        out[k] = v                       # сырая строка localStorage: ищем в ней подстроки

r1 = out["R1"]
assert [m["t"] for m in r1] == ["вопрос", "первая мысль", "вторая мысль"], r1
assert all(m["step"] and m["st"] == "think" and m["lbl"] == "думает" and not m["live"] for m in r1[1:]), r1
ok("уход посреди стрима: мысли сохраняются и восстанавливаются блоками «💜 Вишенка думает…» (живая — тоже)")
raw1 = out["RAW1"]
assert all(x not in raw1 for x in ("читаю", "ошибка")) and "вторая мысль" in raw1
ok("шаги работы и ошибки не сохраняются — только реплики и рассуждения")

r2 = out["R2"]
assert r2[0] == "вопрос" and r2[1:] == ["мысль %d" % i for i in range(8, 40)], r2[:3]
ok("потолок 32 шага: сохраняются последние рассуждения (ранние вытесняются)")

assert out["R3"] == ["вопрос"], out["R3"]
ok("рассуждения не уходят в историю для модели — промт не засоряется шагами")
print("test_think_history: OK")
