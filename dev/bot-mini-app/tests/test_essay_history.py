"""«ИИ-проверка»: ученики и история (пуш 7 из PUSH-PLAN.md).

Привязка проверки к ученику (выбор можно пропустить), «Сохранить за учеником»,
постоянная история сохранённых проверок и повторный просмотр разбора без запуска
модели, отчёт по критериям ФИПИ. Строки фронта — по docs/index.html; API — живым
тестом через TestClient. Сохранение и история работают БЕЗ ИИ-провайдера.
Запуск: python3 tests/test_essay_history.py"""
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def ok(c): print("  ✓", c)

html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()

# --- 1. привязка к ученику: выбор при сохранении и в истории (экран мокапа чипа не содержит), «Без ученика» — пропустить ---
for s in ("function ecStuPick(", "function ecStuSet(", "function ecStuName(",
          "Кто написал работу — выбор можно пропустить", "Без ученика",
          'ecStuPick("save")'):
    assert s in html, f"нет выбора ученика: {s}"
ok("привязка к ученику: выбор из списка при сохранении и в истории, можно пропустить («Без ученика»)")

# --- 2. «Сохранить за учеником» на экране результата ---
for s in ("function ecSaveLabel(", "function ecSave(", "function ecSaveRun(",
          "Сохранить за учеником", "Сохранено ✓",
          'api("/api/essay_check/save"', "student_id:ecSid||null"):
    assert s in html, f"нет сохранения: {s}"
assert 'onclick="ecSave()"' in html, "кнопка сохранения не ведёт на ecSave"
ok("«Сохранить за учеником»: сохранение разбора (с ошибками, критериями и статистикой) за учеником или без него")

# --- 3. история проверок: список с датой, типом и баллом ---
for s in ("function ecHistOpen(", "function ecHistLoad(", "function ecHistScr(",
          "function ecHistStu(", "function ecHistPick(",
          "История проверок", "Сохранённых проверок пока нет",
          'api("/api/essay_check/history"', "Ученик: ${"):
    assert s in html, f"нет истории: {s}"
assert "function ecHistTap(" in html, "нет повторного просмотра из истории"
assert re.search(r"c\.kind==\"oge\"\?\"Эссе для ОГЭ\":\"Сочинение для ЕГЭ\".*?c\.student_name\|\|\"Без ученика\".*?ecWhen\(c\.created\)",
                 html, re.S), "в строке истории нет типа работы, ученика и даты"
assert re.search(r"ec-hsc\"><b>\$\{\+c\.got\|\|0\}</b><small>из \$\{\+c\.max\|\|0\}", html), \
    "в строке истории нет итогового балла"
ok("история: список с датой, типом работы (ЕГЭ/ОГЭ), учеником и итоговым баллом, фильтр по ученику")

# --- 4. повторный просмотр: экран результата из сохранённых данных, без повторного запуска модели ---
m = re.search(r"async function ecHistTap\(id\)\{.*?\nfunction ", html, re.S)
assert m, "не нашлось тело ecHistTap"
body = m.group(0)
assert 'api("/api/essay_check/load"' in body, "просмотр не грузит сохранённую проверку"
assert "/api/essay_check/run" not in body, "просмотр истории не должен запускать модель заново"
assert "ecNrm(d.result)" in body and "ecStep=3" in body, "просмотр не рендерит экран результата из сохранённых данных"
assert html.count('"/api/essay_check/run"') == 1, "проверка модели должна зоваться только из ecRun"
ok("повторный просмотр: разбор рендерится из сохранённых данных (ecNrm → ecResult), модель не запускается")

# --- 5. отчёт по критериям ФИПИ доступен и из истории (тот же экран результата) ---
assert "Критерии ФИПИ с обоснованием" in html and "function ecFipiView(" in html, "нет отчёта по критериям"
assert 'ecStep===4?ecHistScr():ecStep===3?ecResult()' in html, "история/просмотр не подключены к роутеру раздела"
assert re.search(r"ecView==\"hist\".*?ecHistOpen\(\)", html, re.S), "из просмотра истории нет возврата в историю"
assert ".ec-hi{" in html and ".ec-hsc{" in html and ".ec-saved{" in html, "нет стилей истории ec-"
ok("отчёт по критериям ФИПИ доступен из истории (тот же экран результата, вкладка «ФИПИ»)")

# --- 6. API: маршруты и поведение ---
import asyncio, json, sqlite3, time
from urllib.parse import urlencode
os.environ.update(BOT_TOKEN="***", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import auth, bot, kimi
from aiohttp.test_utils import TestClient, TestServer

routes = {}
for res in bot.make_app().router.resources():
    for r in res:
        if res.canonical.startswith("/api/essay_check/"):
            routes.setdefault(res.canonical, set()).add(r.method)
assert routes == {"/api/essay_check/upload": {"POST"}, "/api/essay_check/run": {"POST"},
                  "/api/essay_check/save": {"POST"}, "/api/essay_check/history": {"POST"},
                  "/api/essay_check/load": {"POST"}}, routes
ok("маршруты: upload / run / save / history / load — все POST и зарегистрированы в bot.py")

mem = sqlite3.connect(":memory:", check_same_thread=False); bot.db.backup(mem); mem.row_factory = sqlite3.Row
bot.db = mem; auth.init(mem); auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_pins values('hungerrr', 77)")   # nick из белого списка auth.DEFAULT_USERNAMES — иначе второй пользователь не пройдёт вход
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp()))
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(77,?,?)", (time.time() + 99999, auth._fp())); mem.commit()

def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    import hashlib, hmac
    d["hash"] = hmac.new(hmac.new(b"WebAppData", bot.TOKEN.encode(), hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)
H = {"X-Init": init(42, "Gladik_N")}
H2 = {"X-Init": init(77, "hungerrr")}

RES = {"text": "Автор поднимает проблему влеяния книги.", "task": "Условие: по тексту…",
       "errors": [
           {"type": "Орфография", "found": "влеяния", "correct": "влияния", "why": "проверка гласной", "src": "Розенталь, §…"},
           {"type": "Неведомый тип", "found": "считаю что", "correct": "считаю, что", "why": "запятая перед «что»", "src": "…"}],
       "criteria": [
           {"name": "К1", "got": 1, "max": 1, "why": "ок", "tip": ""},
           {"name": "К2", "got": 9, "max": 6, "why": "перебор", "tip": "совет"}],   # зажмётся до 6
       "recommendations": [{"title": "r1", "text": "t1", "badge": "+1"}]}

REAL_KEY = kimi.API_KEY
kimi.API_KEY = ""                       # ИИ-провайдер отключён: сохранение и история обязаны работать без него

async def main():
    app = bot.make_app()
    async with TestClient(TestServer(app)) as c:
        post = lambda p, j, h=H: c.post(p, json=j, headers=h)

        # --- 6а. без входа → 403 на всех новых маршрутах ---
        for p in ("/api/essay_check/save", "/api/essay_check/history", "/api/essay_check/load"):
            assert (await c.post(p, json={})).status == 403, p
        ok("все новые маршруты без входа → 403")

        # --- 6б. неверные данные → 400 ---
        sid = bot.run("insert into students(name,price) values('Анна Петрова',2000)")
        for body in ({"kind": "ege", "student_id": None, "result": "мусор"},
                     {"kind": "сочинение", "student_id": None, "result": RES},
                     {"student_id": None, "result": RES},                          # без типа работы
                     {"kind": "ege", "student_id": True, "result": RES},           # булев id
                     {"kind": "ege", "student_id": "1", "result": RES},            # не int
                     {"kind": "ege", "student_id": 999999, "result": RES},         # нет такого ученика
                     {"kind": "ege", "student_id": None, "result": {}},            # пустой разбор
                     {"kind": "ege", "student_id": None,
                      "result": {"text": "х" * 400_000, "errors": [{"found": "х"}]}}):   # разбор-гигант
            r = await post("/api/essay_check/save", body)
            assert r.status == 400, (body.get("kind"), body.get("student_id"), r.status)
        r = await post("/api/essay_check/save", {"kind": "ege", "student_id": 999999, "result": RES})
        assert (await r.json()).get("error") == "no_student"
        ok("мусорный разбор, кривой тип/id ученика, несуществующий ученик и пустой разбор → 400")

        # --- 6в. сохранение за учеником: привязка, дата, тип работы ---
        r = await post("/api/essay_check/save", {"kind": "oge", "student_id": sid, "result": RES})
        d = await r.json()
        assert r.status == 200 and d["ok"] and d["id"], (r.status, d)
        row = mem.execute("select * from essay_checks where id=?", (d["id"],)).fetchone()
        assert row["tg_id"] == 42 and row["student_id"] == sid and row["kind"] == "oge", dict(row)
        assert row["created"], "дата сохранённой проверки не записана"
        assert not kimi.enabled(), "тест должен идти с отключённым ИИ-провайдером"
        ok("«Сохранить за учеником»: запись в постоянную историю (ученик, тип, дата) — без ИИ-провайдера")

        r = await post("/api/essay_check/save", {"kind": "ege", "student_id": None, "result": RES})
        d2 = await r.json()
        assert r.status == 200 and mem.execute("select student_id from essay_checks where id=?", (d2["id"],)).fetchone()[0] is None
        ok("проверку можно сохранить и без ученика (пропуск привязки)")

        # --- 6г. история: список, дата/тип/балл, фильтр по ученику, своя учётка ---
        r = await post("/api/essay_check/history", {"student_id": None})
        checks = (await r.json())["checks"]
        mine = [x for x in checks if x["id"] in (d["id"], d2["id"])]
        assert r.status == 200 and len(mine) == 2, (r.status, checks)
        for x in mine:
            assert {"id", "kind", "created", "student_id", "student_name",
                    "got", "max", "percent", "level"} <= set(x), sorted(x)
        st = [x for x in mine if x["id"] == d["id"]][0]
        assert st["kind"] == "oge" and st["student_id"] == sid and st["student_name"] == "Анна Петрова", st
        assert st["got"] == 7 and st["max"] == 7 and st["percent"] == 100 and st["level"] == "Отлично", st
        assert [x for x in mine if x["id"] == d2["id"]][0]["student_id"] is None
        assert (await (await post("/api/essay_check/history", {"student_id": sid})).json())["checks"] == [st], "фильтр по ученику не работает"
        assert (await (await post("/api/essay_check/history", {"student_id": 999999})).json())["checks"] == []
        assert (await post("/api/essay_check/history", {"student_id": "x"})).status == 400
        ok("история: список с датой, типом, учеником и баллом; фильтр по ученику (и пустой для чужого id)")

        r = await post("/api/essay_check/save", {"kind": "ege", "student_id": None, "result": RES}, H2)
        alien = (await r.json())["id"]
        assert all(x["id"] != alien for x in (await (await post("/api/essay_check/history", {"student_id": None})).json())["checks"])
        assert all(x["id"] != d["id"] for x in (await (await post("/api/essay_check/history", {"student_id": None}, H2)).json())["checks"])
        ok("история своя для каждого аккаунта: чужие проверки не видны")

        # --- 6д. повторный просмотр: полный разбор из сохранённых данных ---
        r = await post("/api/essay_check/load", {"id": d["id"]})
        v = await r.json()
        assert r.status == 200 and v["id"] == d["id"] and v["kind"] == "oge", (r.status, v)
        assert v["student_id"] == sid and v["student_name"] == "Анна Петрова" and v["created"] == row["created"]
        res = v["result"]
        assert res["text"] == RES["text"] and res["task"] == RES["task"]
        assert [e["i"] for e in res["errors"]] == [1, 2] and res["errors"][1]["type"] == "Речь"   # нормализация сохранена
        assert res["criteria"][1]["got"] == 6 and res["score"] == {"got": 7, "max": 7, "percent": 100, "level": "Отлично"}
        assert set(res) == {"text", "task", "errors", "criteria", "recommendations", "score", "missing", "why"}, sorted(res)
        assert res["missing"] == [] and res["why"] == "", (res["missing"], res["why"])   # все блоки с данными
        ok("повторный просмотр: полный разбор (ошибки, критерии ФИПИ, статистика, балл) из сохранённых данных")

        assert (await post("/api/essay_check/load", {"id": alien})).status == 403, "чужая проверка не должна читаться"
        for body in ({"id": 999999}, {"id": "x"}, {"id": True}, {}):
            assert (await post("/api/essay_check/load", body)).status == 400, body
        ok("просмотр: чужая проверка → 403, неизвестный/кривой id → 400")

        # --- 6е. лимит сохранённых проверок ---
        mem.execute("delete from essay_checks where tg_id=42"); mem.commit()
        real_max = bot.EC_MAX_SAVED
        bot.EC_MAX_SAVED = 3
        try:
            ids = []
            for i in range(4):
                ids.append((await (await post("/api/essay_check/save",
                            {"kind": "ege", "student_id": None, "result": RES})).json())["id"])
            left = [x["id"] for x in (await (await post("/api/essay_check/history", {"student_id": None})).json())["checks"]]
            assert left == ids[:0:-1], (ids, left)          # новые — сверху, самая старая ушла
            assert (await post("/api/essay_check/load", {"id": ids[0]})).status == 400, "самая старая проверка должна уйти из истории"
        finally:
            bot.EC_MAX_SAVED = real_max
        ok("лимит истории: при переполнении самая старая проверка уходит (EC_MAX_SAVED)")

        # --- 6ж. привязка к ученику: удаление ученика убирает и его проверки ---
        sid2 = bot.run("insert into students(name,price) values('Пётр Сидоров',1500)")
        rid = (await (await post("/api/essay_check/save", {"kind": "oge", "student_id": sid2, "result": RES})).json())["id"]
        bot.delete_student(sid2)
        left2 = [x["id"] for x in (await (await post("/api/essay_check/history", {"student_id": None})).json())["checks"]]
        assert rid not in left2 and ids[1] in left2, left2     # проверки ученика ушли, без ученика — остались
        assert (await post("/api/essay_check/load", {"id": rid})).status == 400
        ok("удаление ученика убирает его проверки из истории (delete_student зачищает essay_checks)")

        kimi.API_KEY = REAL_KEY

asyncio.run(main())
print("test_essay_history: OK")
