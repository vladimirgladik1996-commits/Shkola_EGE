"""«ИИ-проверка»: возврат в раздел — «Последняя проверка» + «Новая проверка».

Баг: пользователь запускал проверку, выходил из Telegram-мини-аппа, возвращался — и терял разбор
(он жил только в памяти телефона и в сеансе с TTL 30 мин; постоянно сохранялось лишь «Сохранить за учеником»).
Теперь сервер сам запоминает готовый разбор и фото работы (essaylast.py, таблица essay_last), а на входе в
раздел показывается экран из двух кнопок:
  • «Последняя проверка» — ЕГЭ/ОГЭ, дата, балл → превью самой свежей проверки;
  • «Новая проверка» — прежний первый экран с выбором ЕГЭ/ОГЭ.
Серверная часть тестируется на голой sqlite (aiohttp не нужен), клиент — настоящим index.html в Chromium
с подставным API (без playwright/браузера E2E-часть пропускается).
Запуск: python3 tests/test_essay_last.py"""
import base64, io, json, os, re, sqlite3, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
def ok(c): print("  ✓", c)

bot = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()

# ---------- 1. сервер: миграция, автосохранение ДО отдачи клиенту, эндпоинты ----------
m = re.search(r"^MIGRATIONS = \[\n(.*?)\n\]\n\ndef migrate", bot, re.S | re.M)
assert m, "не нашлось список MIGRATIONS"
MIGR = eval("[\n" + m.group(1) + "\n]")
assert "essay_last" in " ".join(MIGR[-1]), "последняя миграция должна создавать essay_last (старые не правим)"
assert all("essay_last" not in " ".join(x) for x in MIGR[:-1]), "essay_last уже была в старой миграции"
ok(f"миграция v{len(MIGR)} создаёт essay_last; старые миграции не тронуты")

i_save, i_end = bot.index('ec_last_save(uid, kind, v, rec[1]["w"])'), bot.index('_ec_job_end(job, {"t": "done", "result": v})')
assert i_save < i_end, "разбор должен сохраняться ДО отдачи клиенту: клиент мог уже выйти из приложения"
for s in ("def ec_last_save(", "essaylast.put(run,", "async def api_essay_check_last(", "essaylast.head(q,",
          '"/api/essay_check/last"', 'j.get("last") is True', "essaylast.full(q, uid)", "essaycheck, essaylast, essayreport"):
    assert s in bot, f"нет в bot.py: {s}"
assert re.search(r"def ec_last_save\(.*?except Exception:\s+log\.exception", bot, re.S), \
    "сбой записи последней проверки не должен ронять саму проверку"
ok("автосохранение при готовом разборе (до отдачи клиенту, сбой записи не ломает проверку), эндпоинты last и load{last:true}")

# ---------- 2. essaylast.py на настоящей схеме ----------
import essaylast
db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
for stmts in MIGR:
    for st in stmts:
        if "essay_" in st:
            db.execute(st)
q = lambda sql, *a: db.execute(sql, a).fetchall()
def run(sql, *a):
    c = db.execute(sql, a); db.commit(); return c.lastrowid
R = lambda got, mx, lvl="Хорошо": {"text": "т", "errors": [{"type": "Орфография"}], "criteria": [{"name": "К1"}],
                                  "score": {"got": got, "max": mx, "percent": round(got * 100 / mx), "level": lvl}}
assert essaylast.head(q, 7) is None and essaylast.full(q, 7) is None
ok("проверок нет → head/full = None (экран возврата не показывается, сразу выбор ЕГЭ/ОГЭ)")

assert essaylast.put(run, 7, "ege", R(12, 17), ["AAA", "BBB"])
h = essaylast.head(q, 7)
assert (h["kind"], h["got"], h["max"], h["percent"], h["level"]) == ("ege", 12, 17, 71, "Хорошо") and h["created"]
f = essaylast.full(q, 7)
assert f["photos"] == ["AAA", "BBB"] and f["result"]["score"]["got"] == 12 and f["id"] == 0 and f["student_name"] is None
ok("put → head (тип, дата, балл) и full (разбор + фото работы в порядке страниц)")

assert essaylast.put(run, 7, "oge", R(9, 10, "Отлично"))
h = essaylast.head(q, 7); assert (h["kind"], h["got"]) == ("oge", 9)
assert q("select count(*) c from essay_last where tg_id=7")[0]["c"] == 1 and essaylast.full(q, 7)["photos"] == []
ok("новая проверка заменяет прежнюю (одна запись на пользователя), фото прежней не «подмешиваются»")

assert essaylast.head(q, 8) is None and essaylast.put(run, 8, "ege", R(1, 2))
assert essaylast.head(q, 7)["kind"] == "oge" and essaylast.head(q, 8)["got"] == 1
ok("у каждого пользователя своя последняя проверка")

before = q("select count(*) c from essay_last")[0]["c"]
assert not essaylast.put(run, 9, "xx", R(1, 2)) and not essaylast.put(run, 9, "ege", "мусор") and not essaylast.put(run, 9, "ege", None)
assert q("select count(*) c from essay_last")[0]["c"] == before
ok("мусорный тип работы или не-разбор не записываются")

big = ["x" * (essaylast.MAX_PHOTO_CHARS // 2 + 1)] * 2
assert essaylast.put(run, 10, "ege", R(5, 6), big)
assert essaylast.full(q, 10)["photos"] == [] and essaylast.head(q, 10)["got"] == 5
assert len(json.loads(q("select photos from essay_last where tg_id=7")[0]["photos"] or "[]")) == 0
ok("слишком тяжёлые фото не раздувают БД: разбор сохраняется, фото — нет")

assert essaylast.put(run, 11, "ege", R(5, 6), ["p"] * 20)
assert len(essaylast.full(q, 11)["photos"]) == essaylast.MAX_PHOTOS
ok(f"не больше {essaylast.MAX_PHOTOS} фото на проверку (как в клиенте)")

old = json.dumps(R(3, 4), ensure_ascii=False)
run("insert into essay_checks(tg_id,student_id,kind,result) values(?,?,?,?)", 20, None, "ege", json.dumps(R(1, 4)))
run("insert into essay_checks(tg_id,student_id,kind,result) values(?,?,?,?)", 20, None, "oge", old)
h = essaylast.head(q, 20); assert (h["kind"], h["got"]) == ("oge", 3) and essaylast.full(q, 20)["photos"] == []
ok("проверки до этой версии: берётся самая свежая запись истории (без фото)")
essaylast.put(run, 20, "ege", R(8, 9))
assert essaylast.head(q, 20)["got"] == 8
ok("есть и essay_last, и история → «последняя» — essay_last (каждая проверка проходит через неё)")

run("insert into essay_last(tg_id,kind,result,photos) values(30,'ege','{битый json',null)")
assert essaylast.head(q, 30) is None and essaylast.full(q, 30) is None
ok("битая запись не роняет экран возврата: как будто проверок нет")

# ---------- 3. клиент: строки ----------
for s in ("function ecHome(", "function ecPick(", "function ecHub(", "function ecNewCheck(", "async function ecLastOpen(",
          "function ecLastBack(", "function ecLastSet(", "async function ecLastLoad(", "Последняя проверка", "Новая проверка",
          'api("/api/essay_check/last"', 'api("/api/essay_check/load",{last:true})', 'id="ec-last"', 'id="ec-new"'):
    assert s in html, f"нет на клиенте: {s}"
assert re.search(r"function ecDone\(r\)\{const n=ecNrm\(r\);ecRes=n;ecLastSet\(r,n\);", html), "ecDone не обновляет «последнюю проверку»"
assert re.search(r'if\(ecView=="last"\)\{ecLastBack\(\)\}else if\(ecView=="hist"\)', html), "«Назад» с последней проверки не ведёт на экран возврата"
assert "ecStep===1?ecShot():ecHome()" in html, "нулевой шаг должен идти через ecHome"
ok("клиент: экран возврата, открытие последней проверки с фото, «Назад», обновление после новой проверки")

# ---------- 4. клиент: настоящий index.html в Chromium с подставным API ----------
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p:
        _p.chromium.launch(args=["--no-sandbox"]).close()
    HAVE = True
except Exception as e:
    HAVE = False
    ok(f"Chromium недоступен ({type(e).__name__}) — E2E экрана возврата пропущен")
if not HAVE:
    print("OK"); sys.exit(0)

try:
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (240, 320), (250, 250, 244)).save(buf, "JPEG"); PHOTO = base64.b64encode(buf.getvalue()).decode()
except Exception:
    PHOTO = "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
RESULT = {"task": "", "text": "Автор поднимает проблему влеяния книги на человека. Я считаю что чтение формирует личность.",
    "errors": [{"type": "Орфография", "found": "влеяния", "correct": "влияния", "why": "Безударная гласная в корне.", "src": "Розенталь", "x": 30, "y": 22, "page": 0},
               {"type": "Пунктуация", "found": "считаю что", "correct": "считаю, что", "why": "Запятая перед «что».", "src": "Правила", "x": 40, "y": 38, "page": 0}],
    "criteria": [{"name": "К1 Формулировка проблемы", "max": 1, "got": 1, "why": "Проблема названа верно.", "tip": ""},
                 {"name": "Орфография", "max": 3, "got": 2, "why": "1 ошибка.", "tip": "Повторите корни."}],
    "recommendations": [{"title": "Проверьте запятые", "text": "Перед «что».", "badge": "+1 К8"}],
    "score": {"got": 3, "max": 4, "percent": 75, "level": "Хорошо"}}
LAST = {"kind": "ege", "created": "2026-10-29 14:05:00", "got": 12, "max": 17, "percent": 71, "level": "Хорошо"}
DB = {"students": [{"id": 1, "name": "Анна К.", "price": 1500, "paid": 0, "done": 0, "spent": 0, "balance": 0, "photo": None,
                    "deleted": 0, "grade": "11", "subject": "Русский"}], "lessons": [], "payments": [], "cur": "₽", "env": ""}

def boot(browser, last):
    pg = browser.new_page(viewport={"width": 393, "height": 800}); errs = []; calls = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    def h(route):
        url = route.request.url
        if url.endswith("/index.html") or url.endswith("/"):
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read())
        if "/api/data" in url:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(DB, ensure_ascii=False))
        if "/api/essay_check/last" in url:
            calls.append("last"); return route.fulfill(status=200, content_type="application/json", body=json.dumps({"last": last}, ensure_ascii=False))
        if "/api/essay_check/load" in url:
            calls.append(("load", route.request.post_data))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"id": 0, "kind": "ege", "created": last["created"], "student_id": None, "student_name": None, "result": RESULT, "photos": [PHOTO, PHOTO]}, ensure_ascii=False))
        return route.fulfill(status=200, content_type="application/json", body="{}")
    pg.route("**/*", h)
    pg.add_init_script("""window.Telegram={WebApp:{ready:function(){},expand:function(){},initData:'',BackButton:{onClick:function(){},show:function(){},hide:function(){}},
        HapticFeedback:{impactOccurred:function(){},notificationOccurred:function(){}},showAlert:function(){},showConfirm:function(q,cb){cb(true)},showPopup:function(o,cb){cb&&cb('ok')}}};""")
    pg.goto("http://app.local/index.html")
    pg.wait_for_function("typeof L!=='undefined'&&Array.isArray(L)", timeout=15000); pg.wait_for_timeout(500)
    return pg, errs, calls

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    # --- а. есть последняя проверка: на входе в раздел — две кнопки, а не выбор ЕГЭ/ОГЭ ---
    pg, errs, calls = boot(b, LAST)
    pg.evaluate("tab='ai';render()"); pg.wait_for_selector("#ec-last:not([disabled])", timeout=8000)
    t = pg.inner_text("#ec-last")
    assert "Последняя проверка" in t and "ЕГЭ" in t and "12 из 17" in t and "29" in t and "14:05" in t, f"кнопка последней проверки: {t!r}"
    assert "Новая проверка" in pg.inner_text("#ec-new")
    assert pg.evaluate("document.querySelectorAll('.ec-pb').length") == 2, "на экране возврата ровно две кнопки"
    assert pg.evaluate("!document.querySelector(\"[onclick*=ecStart]\")"), "выбор ЕГЭ/ОГЭ не должен быть виден до «Новая проверка»"
    assert calls.count("last") == 1
    ok(f"вход в раздел: две кнопки — «Последняя проверка» ({' / '.join(t.split(chr(10))[-1:])}) и «Новая проверка»")
    pg.wait_for_timeout(3500)   # дать заставке приложения закрыться
    pg.screenshot(path="/tmp/ec_hub.png")
    # --- б. «Последняя проверка» → превью самой свежей: плашка, фото поверх, ошибки ---
    pg.click("#ec-last"); pg.wait_for_function("ecStep===3", timeout=8000); pg.wait_for_timeout(500)
    assert any(isinstance(c, tuple) and json.loads(c[1]) == {"last": True} for c in calls), "превью должно грузиться с {last:true}"
    note = pg.inner_text(".ec-note"); assert "Последняя проверка" in note and "ЕГЭ" in note, note
    assert pg.evaluate("ecPhotoMode()") is True and pg.evaluate("document.querySelectorAll('.ec-page img').length") == 2, "фото работы должны вернуться"
    pg.wait_for_function("[...document.querySelectorAll('.ec-page img')].every(i=>i.complete)", timeout=5000)
    assert pg.evaluate("[...document.querySelectorAll('.ec-page img')].every(i=>i.naturalWidth>0)"), \
        "фото должны реально отрисоваться (раньше ecPhotoDesk подставлял в src объект → «[object Object]»)"
    assert pg.evaluate("document.querySelectorAll('.ec-mk, .ec-badge, [class*=\"ec-mk\"]').length") >= 0
    mk = pg.evaluate("() => { const P = document.querySelector('.ec-page').getBoundingClientRect(); return [...document.querySelectorAll('.ec-page:first-child .ec-er')].map(m => { const r = m.getBoundingClientRect(); return {pos: getComputedStyle(m).position, x: Math.round((r.left + r.width / 2 - P.left) / P.width * 100), y: Math.round((r.top + r.height / 2 - P.top) / P.height * 100)}; }); }")
    assert len(mk) == 2 and all(m["pos"] == "absolute" for m in mk), mk
    assert abs(mk[0]["x"] - 30) <= 2 and abs(mk[0]["y"] - 22) <= 2, f"метка №1 должна стоять на 30%/22% страницы: {mk}"
    assert pg.evaluate("ecRes.errors.length") == 2 and pg.evaluate("ecRes.score.got") == 3
    assert pg.evaluate("!!document.querySelector('[onclick*=\"ecSave\"]')"), "«Сохранить за учеником» должна остаться"
    ok("«Последняя проверка» → превью той же проверки: плашка с типом и датой, фото работы с отметками, ошибки, критерии")
    pg.screenshot(path="/tmp/ec_last_preview.png")
    # --- в. «Назад» с превью → снова экран возврата ---
    pg.evaluate("ecBack()"); pg.wait_for_selector("#ec-last", timeout=5000)
    assert pg.evaluate("ecStep")==0 and pg.evaluate("ecShots.w.length")==0 and pg.evaluate("ecRes")is None
    ok("«Назад» с превью последней проверки → экран возврата (фото и разбор из памяти убраны)")
    # --- г. «Новая проверка» → прежний первый экран выбора ЕГЭ/ОГЭ ---
    pg.click("#ec-new"); pg.wait_for_function("!!document.querySelector(\"[onclick*=ecStart]\")", timeout=5000)
    assert pg.evaluate("document.querySelectorAll(\"[onclick*=ecStart]\").length===2") and pg.evaluate("!document.querySelector('#ec-last')")
    ok("«Новая проверка» → первый экран раздела: «Сочинение для ЕГЭ» и «Эссе для ОГЭ»")
    pg.screenshot(path="/tmp/ec_picker.png")
    pg.evaluate("ecStart('oge')"); pg.wait_for_function("ecStep===1"); pg.evaluate("ecBack()"); pg.wait_for_timeout(200)
    assert pg.evaluate("!!document.querySelector(\"[onclick*=ecStart]\")"), "из съёмки «Назад» ведёт на выбор ЕГЭ/ОГЭ, а не на экран возврата"
    ok("из съёмки «Назад» возвращает на выбор ЕГЭ/ОГЭ (человек уже выбрал «Новая проверка»)")
    # --- д. свежая проверка сразу становится «последней» на экране возврата ---
    pg.evaluate("ecKind='oge';ecStep=0;ecDone(%s);ecStep=0;ecNew=false;ecView='';render()" % json.dumps(RESULT, ensure_ascii=False))
    pg.wait_for_selector("#ec-last"); t = pg.inner_text("#ec-last")
    assert "ОГЭ" in t and "3 из 4" in t, t
    ok("после новой проверки кнопка «Последняя проверка» показывает её: ОГЭ и новый балл")
    # --- д2. мини-апп пролежал свёрнутым: шапка обновляется в фоне и не затирается при сбое сети ---
    NEWER = dict(LAST, kind="oge", got=9, max=10, created="2026-10-30 09:00:00")
    pg.unroute("**/*"); pg.route("**/*", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"last": NEWER}))
                                  if "/api/essay_check/last" in r.request.url else r.continue_())
    pg.evaluate("ecLastAt=Date.now()-60000;ecNew=false;ecStep=0;ecView='';render()")
    pg.wait_for_function("document.querySelector('#ec-last')&&document.querySelector('#ec-last').innerText.includes('9 из 10')", timeout=6000)
    ok("свёрнутый мини-апп: шапка последней проверки сама обновилась (ОГЭ · 9 из 10), без перезагрузки")
    pg.unroute("**/*"); pg.route("**/*", lambda r: r.abort() if "/api/essay_check/last" in r.request.url else r.continue_())
    pg.evaluate("ecLastAt=Date.now()-60000;render()"); pg.wait_for_timeout(1200)
    assert "9 из 10" in pg.inner_text("#ec-last"), "при сбое сети фоновое обновление не должно затирать показанную проверку"
    ok("сбой сети при фоновом обновлении не стирает уже показанную проверку")
    assert not errs, errs; pg.close()
    # --- е. проверок ещё не было: сразу первый экран выбора, как раньше ---
    pg, errs, calls = boot(b, None)
    pg.evaluate("tab='ai';render()"); pg.wait_for_function("!!document.querySelector(\"[onclick*=ecStart]\")", timeout=8000)
    assert pg.evaluate("!document.querySelector('#ec-last')")
    ok("проверок ещё не было → сразу выбор ЕГЭ/ОГЭ, экран возврата не показывается")
    # --- ж. пока шапка грузится — «Новая проверка» уже работает ---
    pg.evaluate("document.getElementById('app').innerHTML=ecHub(null)")
    assert pg.evaluate("document.querySelector('#ec-last').disabled") and pg.evaluate("!document.querySelector('#ec-new').disabled")
    ok("пока шапка грузится — «Последняя проверка» неактивна («Загружаю…»), «Новая проверка» доступна")
    assert not errs, errs; pg.close(); b.close()
print("OK")
