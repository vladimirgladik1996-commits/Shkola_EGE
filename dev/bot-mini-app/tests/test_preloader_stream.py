"""Прелоадер «ИИ-проверки»: стрим мышления — ОДИН блок под картинкой Вишенки.
Было: рисовалось два блока с стримингом — карточка лога этапов (#ec-log) и отдельная
карточка мышления (.ec-thk) с собственной рамкой/фоном. Стало: одна карточка, внутри —
этапы (.ec-stg) и секция мышления (.ec-thk) с разделителем. Контракт по исходнику +
живой рендер ecProc() в node (без node — честный пропуск).
Запуск: python3 tests/test_preloader_stream.py"""
import json, os, re, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def ok(c): print("  ✓", c)

html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()

# --- 1. контракт: одна секция мышления, и она ВНУТРИ общего блока стрима ---
assert html.count('id="ec-think"') == 1, "окно стриминга мышления должно быть ровно одно"
assert '<div class="ec-log" id="ec-log"><div class="ec-stg" id="ec-stages">${ecLogHtml()}</div>${ecThinkHtml()}</div>' in html, \
    "мышление должно быть секцией внутри блока #ec-log, а не отдельной карточкой"
assert html.count("💭 Вишенка думает…") == 1, "заголовок мышления строится в одном месте (ecThinkHtml)"
ok("контракт: ровно один блок стрима под картинкой, мышление — его секция")

# --- 2. контракт: у секции мышления больше нет своей «карточки» ---
thk = re.search(r"\.ec-thk\{[^}]*\}", html).group(0)
assert "background:var(--card)" not in thk and "border-radius" not in thk, f"секция мышления снова карточка: {thk}"
assert "border-top" in thk, "секция мышления отделяется разделителем внутри блока"
assert ".ec-stg div{" in html, "анимация этапов должна быть на .ec-stg (иначе съест секцию мышления)"
ok("контракт: .ec-thk — секция с разделителем, анимация этапов scoped на .ec-stg")

# --- 3. контракт: обновление этапов не затирает мышление ---
up = html[html.index("function ecUpdProc(){"):html.index("function ecWhyMsg(")]
assert 'const g=$("#ec-stages")' in up, "этапы обновляются в #ec-stages, а не перезаписывают весь блок"
assert 't.textContent=ecThink' in up, "дельты мышления пишутся в #ec-think"
ok("контракт: этапы и дельты мышления обновляются раздельно в одном блоке")

# --- 4. живой рендер ecProc(): один блок с стримингом, а не два ---
if not shutil.which("node"):
    print("  - node не найден — живой рендер честно пропущен")
    sys.exit(0)
src = html[html.index("function ecLogHtml(){"):html.index("async function ecRun()")]
harness = r"""
const esc=s=>String(s);
const EC_MAS="m.png";
let ecState="run",ecThink="Сначала разберу условие…",ecStep=2;
const ecShots={w:["a","b"],t:[]},ecLive=["Распознан рукописный текст"],ecWait=["Сверяю с критериями ФИПИ…"];
""" + src + r"""
const withThink=ecProc();
ecThink=""; const without=ecProc();
const cnt=(s,re)=>(s.match(re)||[]).length;
console.log("RESULT:"+JSON.stringify({
  thk:cnt(withThink,/class="ec-thk"/g), thinkId:cnt(withThink,/id="ec-think"/g),
  thk0:cnt(without,/class="ec-thk"/g),
  inside:withThink.indexOf('class="ec-thk"')>withThink.indexOf('id="ec-log"'),
  stages:withThink.includes('id="ec-stages"'),
  oneCard:cnt(withThink,/class="ec-log"/g)===1}));
"""
r = subprocess.run(["node"], input=harness, capture_output=True, text=True)
assert r.returncode == 0, f"node: {r.stderr[-500:]}"
res = json.loads([ln for ln in r.stdout.splitlines() if ln.startswith("RESULT:")][0][7:])
assert res["thk"] == 1 and res["thinkId"] == 1, f"два блока с стримингом: {res}"
assert res["thk0"] == 0, f"без мышления карточка рисоваться не должна: {res}"
assert res["inside"] and res["stages"] and res["oneCard"], res
ok("живой рендер ecProc(): один блок (#ec-log) с этапами и мышлением внутри; без мышления — только этапы")

print("test_preloader_stream: OK")
