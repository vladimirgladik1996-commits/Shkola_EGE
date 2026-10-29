"""«Расписание»: UX-правки (задача 3 плана пушей).
1) вкладка «Неделя» открывается с самого начала сетки (без автоскролла к красной линии «сейчас»);
2) под сеткой нет инструкций («Тап по пустой клетке…»);
3) тап по чипу занятия на «Дне» открывает карточку занятия — как на «Неделе»
   (регресс: у button инлайн onclick="form(...)" видит свойство button.form и не видит функцию;
   вызов идёт через window.form).
Живой Chromium + мок API (страница об этом не знает). Без playwright/браузера — пропуск.
Запуск: python3 tests/test_schedule_ui_e2e.py"""
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
    ok(f"Chromium недоступен ({type(e).__name__}) — E2E UX расписания пропущен")

if not HAVE:
    print("OK")
    sys.exit(0)

today = datetime.date.today()
dk = lambda n: (today + datetime.timedelta(days=n)).isoformat()
DAY_A, DAY_B = dk(1), dk(3)

DB = {
    "students": [
        {"id": 1, "name": "\u0410\u043d\u043d\u0430 \u041a.", "price": 1500, "paid": 0, "done": 0, "spent": 0, "balance": 0,
         "photo": None, "deleted": 0, "grade": "11", "subject": "\u0420\u0443\u0441\u0441\u043a\u0438\u0439"},
    ],
    "lessons": [
        {"id": 101, "day": DAY_A, "time": "10:00", "dur": 60, "student_id": 1, "regular_id": None,
         "name": "\u0410\u043d\u043d\u0430 \u041a.", "price": 1500, "past": 0},
        {"id": 102, "day": DAY_B, "time": "12:00", "dur": 90, "student_id": 1, "regular_id": None,
         "name": "\u0410\u043d\u043d\u0430 \u041a.", "price": 1500, "past": 0},
    ],
    "payments": [],
    "cur": "\u20bd", "env": "",
}


def route_handler(route):
    req, url = route.request, route.request.url
    if url.endswith("/index.html") or url.endswith("/"):
        with open(HTML, encoding="utf-8") as f:
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=f.read())
    if "/api/data" in url:
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(DB, ensure_ascii=False))
    return route.fulfill(status=200, content_type="application/json", body="{}")


with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 393, "height": 640})   # низкий экран: сетка недели не влезает целиком
    pg.route("**/*", route_handler)
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.add_init_script("""window.Telegram={WebApp:{ready:function(){},expand:function(){},initData:'',
        BackButton:{onClick:function(){},show:function(){},hide:function(){}},
        HapticFeedback:{impactOccurred:function(){},notificationOccurred:function(){}},
        showAlert:function(){},showConfirm:function(q,cb){cb(true)},showPopup:function(o,cb){cb&&cb('ok')}}};
        localStorage.schedView='w';""")
    pg.goto("http://app.local/index.html")
    pg.wait_for_function("typeof L!=='undefined'&&Array.isArray(L)&&L.length===2", timeout=15000)
    pg.wait_for_timeout(600)   # даём отработать стартовым анимациям и отложенным прокруткам чата

    # 1. «Неделя» открывается с самого начала сетки: прокрутки к красной линии нет
    pg.evaluate("tab='c';setView('d');render()")
    pg.evaluate("window.scrollTo(0,300)")   # как будто пользователь промотал страницу вниз
    pg.evaluate("setView('w')")
    pg.wait_for_timeout(400)
    y = pg.evaluate("scrollY")
    nl = pg.evaluate("document.querySelectorAll('.sx-nline').length")
    assert y == 0, f"неделя должна открываться сверху, scrollY={y}"
    assert nl == 1, "красная линия «сейчас» должна быть на сетке текущей недели"
    ok("вкладка «Неделя» открывается с начала сетки (scrollY=0) — к красной линии не проматывает")

    # 2. под сеткой нет инструкций
    n = pg.evaluate("document.querySelectorAll('.sx-hint').length")
    txt = pg.evaluate("document.body.innerText")
    assert n == 0, "остались .sx-hint"
    assert "\u0422\u0430\u043f \u043f\u043e \u043f\u0443\u0441\u0442\u043e\u0439" not in txt, "осталась подсказка «Тап по пустой клетке…»"
    ok("под сеткой нет инструкций («Тап по пустой клетке…» убраны)")

    # 3. тап по чипу занятия на «Дне» открывает карточку занятия (как на «Неделе»)
    pg.evaluate(f"setView('d');setDay('{DAY_A}');render()")
    pg.wait_for_timeout(300)
    pg.click(".sx-l")
    pg.wait_for_timeout(250)
    h3 = pg.evaluate("(document.querySelector('#sheet .sh h3')||{}).textContent||''")
    assert pg.evaluate("document.querySelector('#sheet').classList.contains('on')"), "карточка не открылась"
    assert "\u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0437\u0430\u043d\u044f\u0442\u0438\u0435" in h3, f"неожиданный заголовок: {h3!r}"
    ok("тап по чипу занятия на «Дне» открывает карточку занятия")
    pg.evaluate("close_()")

    # 3б. кнопка свободного интервала на «Дне» открывает форму нового занятия
    pg.evaluate("document.querySelector('.sx-gb')&&document.querySelector('.sx-gb').click()")
    pg.wait_for_timeout(250)
    h3 = pg.evaluate("(document.querySelector('#sheet .sh h3')||{}).textContent||''")
    assert "\u041d\u043e\u0432\u043e\u0435 \u0437\u0430\u043d\u044f\u0442\u0438\u0435" in h3 or not pg.evaluate("document.querySelector('.sx-gb')"), f"неожиданный заголовок: {h3!r}"
    ok("свободный интервал на «Дне» открывает форму нового занятия")

    # 4. карточка занятия открывается и с «Недели» (поведение не сломано)
    pg.evaluate("close_()")
    pg.evaluate("setView('w');render()")
    pg.wait_for_timeout(300)
    pg.click(".wg .ls")
    pg.wait_for_timeout(250)
    h3 = pg.evaluate("(document.querySelector('#sheet .sh h3')||{}).textContent||''")
    assert "\u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0437\u0430\u043d\u044f\u0442\u0438\u0435" in h3, f"неожиданный заголовок: {h3!r}"
    ok("тап по занятию на «Неделе» открывает карточку занятия (как раньше)")

    assert not errs, f"ошибки JS на странице: {errs[:3]}"
    ok("ошибок JS нет")
    b.close()
print("OK")
