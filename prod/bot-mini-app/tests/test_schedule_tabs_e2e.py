"""«Расписание»: вкладки День/Неделя/Месяц связаны — добавление/перенос/удаление занятия
видны на всех трёх вкладках. Живой Chromium + мок API (страница об этом не знает):
index.html грузится как есть, /api/* обслуживается тестом. Без playwright/браузера — пропуск
(проверка фронтенда, серверная часть покрыта test_api.py).
Запуск: python3 tests/test_schedule_tabs_e2e.py"""
import datetime, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "docs", "index.html")


def ok(c):
    print("  \u2713", c)


try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p:
        _p.chromium.launch(args=["--no-sandbox"]).close()
    HAVE = True
except Exception as e:
    HAVE = False
    ok(f"Chromium недоступен ({type(e).__name__}) — E2E вкладок расписания пропущен")

if not HAVE:
    print("OK")
    sys.exit(0)

today = datetime.date.today()
dk = lambda n: (today + datetime.timedelta(days=n)).isoformat()
DAY_A, DAY_B = dk(2), dk(5)

DB = {
    "students": [
        {"id": 1, "name": "\u0410\u043d\u043d\u0430 \u041a.", "price": 1500, "paid": 0, "done": 0, "spent": 0, "balance": 0,
         "photo": None, "deleted": 0, "grade": "11", "subject": "\u0420\u0443\u0441\u0441\u043a\u0438\u0439"},
        {"id": 2, "name": "\u0411\u043e\u0440\u0438\u0441 \u041c.", "price": 1200, "paid": 0, "done": 0, "spent": 0, "balance": 0,
         "photo": None, "deleted": 0, "grade": "9", "subject": "\u041c\u0430\u0442\u0435\u043c\u0430\u0442\u0438\u043a\u0430"},
    ],
    "lessons": [
        {"id": 101, "day": DAY_A, "time": "10:00", "dur": 60, "student_id": 1, "regular_id": None,
         "name": "\u0410\u043d\u043d\u0430 \u041a.", "price": 1500, "past": 0},
        {"id": 102, "day": DAY_B, "time": "12:00", "dur": 60, "student_id": 2, "regular_id": None,
         "name": "\u0411\u043e\u0440\u0438\u0441 \u041c.", "price": 1200, "past": 0},
    ],
    "payments": [],
    "cur": "\u20bd", "env": "",
}
NXT = [200]


def route_handler(route):
    req, url = route.request, route.request.url
    if url.endswith("/index.html") or url.endswith("/"):
        with open(HTML, encoding="utf-8") as f:
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=f.read())
    if "/api/data" in url:
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(DB, ensure_ascii=False))
    if "/api/lesson/add" in url:
        b = json.loads(req.post_data)
        while any(l["id"] == NXT[0] for l in DB["lessons"]):
            NXT[0] += 1
        lid = NXT[0]
        NXT[0] += 1
        DB["lessons"].append({"id": lid, "day": b["day"], "time": b["time"], "dur": b.get("dur", 60),
                              "student_id": b["student_id"], "regular_id": None,
                              "name": "\u0410\u043d\u043d\u0430 \u041a.", "price": 1500, "past": 0})
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": lid}))
    if "/api/lesson/move" in url:
        b = json.loads(req.post_data)
        for l in DB["lessons"]:
            if l["id"] == b["id"]:
                l.update({"day": b["day"], "time": b["time"], "dur": b.get("dur", 60)})
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": 1}))
    if "/api/lesson/cancel" in url or "/api/lesson/stop" in url:
        b = json.loads(req.post_data)
        DB["lessons"] = [l for l in DB["lessons"] if l["id"] != b["id"]]
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": 1}))
    return route.fulfill(status=200, content_type="application/json", body="{}")


def check_views(pg, day, t, should, label):
    """Занятие (day,t) должно быть/не быть на всех трёх вкладках День/Неделя/Месяц."""
    res = {}
    for v in ("d", "w", "m"):
        pg.evaluate(f"if(view=='{v}')render();else setView('{v}')")
        if v == "d":
            n = pg.evaluate(f"document.querySelectorAll('.sx-l[data-day=\"{day}\"][data-t=\"{t}\"]').length")
        elif v == "w":
            n = pg.evaluate(f"document.querySelectorAll('.ls[data-day=\"{day}\"][data-t=\"{t}\"]').length")
        else:
            n = pg.evaluate(f"document.querySelectorAll('.d[data-k=\"{day}\"] .c[title=\"{t}\"]').length")
        res[v] = n
    assert all((n > 0) == should for n in res.values()), f"{label}: {res}"
    ok(f"{label}: видно на всех вкладках {res}")


with sync_playwright() as p:
    br = p.chromium.launch(args=["--no-sandbox"])
    pg = br.new_page()
    pg.on("dialog", lambda d: d.accept())
    pg.route("**/*", route_handler)
    pg.add_init_script("""window.Telegram={WebApp:{ready:function(){},expand:function(){},initData:'',
        BackButton:{onClick:function(){},show:function(){},hide:function(){}}}};localStorage.schedView='d';""")
    pg.goto("http://app.local/index.html")
    pg.wait_for_function("typeof L!=='undefined'&&Array.isArray(L)&&L.length===2", timeout=15000)
    ok("страница загрузилась: ученики и занятия пришли из /api/data")
    pg.evaluate("tab='c';render()")

    # 1. новое занятие через вкладку «Месяц» → видно на Месяц/Неделя/День
    pg.evaluate(f"setView('m')")
    pg.evaluate(f"addAt('{DAY_A}')")
    pg.evaluate("document.querySelector('#ft').value='15:00'")
    pg.evaluate("save(0)")
    pg.wait_for_function(f"L.some(l=>l.day==='{DAY_A}'&&l.time==='15:00')", timeout=15000)
    ok("добавление на вкладке «Месяц» (addAt→save): /api/lesson/add, занятие встало в L")
    check_views(pg, DAY_A, "15:00", True, "после добавления на «Месяц»")

    # 2. перенос занятия (15:00 → 16:30)
    lid = pg.evaluate(f"L.find(l=>l.day==='{DAY_A}'&&l.time==='15:00').id")
    pg.evaluate(f"form({lid})")
    pg.evaluate("document.querySelector('#ft').value='16:30'")
    pg.evaluate(f"save({lid})")
    pg.wait_for_function(f"L.some(l=>l.day==='{DAY_A}'&&l.time==='16:30')", timeout=15000)
    ok("перенос (form→save с новым временем): /api/lesson/move")
    check_views(pg, DAY_A, "15:00", False, "старого времени больше нет")
    check_views(pg, DAY_A, "16:30", True, "после переноса на 16:30")

    # 3. удаление занятия (cancelL → подтверждение → /api/lesson/cancel)
    pg.evaluate(f"cancelL({lid})")
    pg.wait_for_function(f"!L.some(l=>l.id==={lid})", timeout=15000)
    ok("удаление (cancelL→подтверждение): /api/lesson/cancel")
    check_views(pg, DAY_A, "16:30", False, "после удаления занятия")

    # 4. вкладки «День» и «Месяц» рендерят занятия и карточку дня
    pg.evaluate(f"setView('d');setDay('{DAY_B}')")
    pg.wait_for_timeout(150)
    assert pg.evaluate("document.querySelectorAll('.sx-l').length") == 1, "на «Дне» должен быть 1 урок"
    ok("вкладка «День»: занятие дня рендерится")
    pg.evaluate(f"setView('m');dayCard('{DAY_B}')")
    pg.wait_for_timeout(100)
    assert pg.evaluate("document.querySelector('#sheet').classList.contains('on')"), "dayCard должен открыться"
    ok("вкладка «Месяц» → карточка дня (dayCard) открывается")
    br.close()

print("OK")
