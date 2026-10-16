"""Регресс бага «Вишенка не добавляет ученика»: три вопроса подряд (имя → класс → цена),
все ответы распознаны верно, а в конце — «не хватает данных», и ученик не сохраняется.

Корень бага: контекст диалога держала не серверная часть, а модель. Достаточно было модели
один раз ответить классом числом (grade: 5 вместо "5") или не повторить уже названные поля —
и параметры терялись: разбор не находил класс, а список «уже спрошено» не давал переспросить.

Три инварианта, которые теперь держит сервер:
  1. Накопление: названные параметры сохраняются в состоянии диалога; следующий ход
     ДОПОЛНЯЕТ их, даже если модель вернула только новый параметр (контекст не в модели).
  2. Числа там, где ждут текст: класс, пришедший числом (5), — это «5», а не потеря.
     Цена текстом («3000 руб») и числом с плавающей точкой (3000.0) тоже разбираются.
  3. Нет тупика «не хватает данных»: неразобранное значение переспрашивается,
     даже если этот вопрос уже звучал.

Сценарии: мини-апп (голос → /api/voice → подтверждение), бот (handle_chat → ai:ok)
и переспрос цены, которую не получилось разобрать. Сеть подменена, база — в памяти.
Запуск: python3 tests/test_assist_context.py
"""
import asyncio, json, os, sqlite3, sys, time
import hashlib, hmac
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOK = "123456:TESTTOKEN"
os.environ.update(BOT_TOKEN=TOK, PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID", "KIMI_API_KEY", "KIMI_BASE_URL", "KIMI_MODEL",
          "WHISPER_API_KEY", "WHISPER_BASE_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import assist, auth, bot, kimi, whisper
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def ok(c):
    print("  ✓", c)


# --- база в памяти ----------------------------------------------------------
mem = sqlite3.connect(":memory:", check_same_thread=False)
mem.row_factory = sqlite3.Row
bot.init_db(mem)
bot.db = mem
auth.init(mem)
auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp()))
mem.commit()

# --- фейковая модель: отвечает по очереди QUEUE ------------------------------
QUEUE = []


async def fake_kimi(request):
    await request.json()
    content = QUEUE.pop(0) if QUEUE else "{}"
    return web.json_response({"choices": [{"message": {"content": content}}]})


fk = web.Application()
fk.router.add_post("/chat/completions", fake_kimi)


def jtask(**kw):
    QUEUE.append(json.dumps(kw, ensure_ascii=False))


def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest(),
                         chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)


H = {"X-Init": init(42, "Gladik_N")}
CTX = {"tab": "s", "screen": None, "screen_args": []}   # вкладка «Ученики» — там доступен add_student


def student(name):
    r = mem.execute("select name,grade,subject,price from students where name=?", (name,)).fetchone()
    return dict(r) if r else None


async def main():
    async with TestClient(TestServer(fk)) as c:
        kimi.BASE = whisper.BASE = str(c.make_url("")).rstrip("/")
        kimi.API_KEY = "TESTKEY"
        whisper.API_KEY = "STTKEY"

        async with TestClient(TestServer(bot.make_app())) as wc:
            post = lambda p, j: wc.post(p, json=j, headers=H)

            # --- 1. мини-апп: класс приходит ЧИСЛОМ, модель не повторяет имя ---
            #     Ровно то, что видел пользователь: имя → «класс?» → «цена?» → «данных недостаточно».
            jtask(kind="app", action="add_student", params={"name": "Ваня Иванов"}, question="Какой класс?")
            j = await (await post("/api/voice", {"ctx": CTX, "text": "Ваня Иванов"})).json()
            assert j["stage"] == "clarify" and "класс" in j["reply"].lower(), j
            ok("шаг 1: имя принято, виджет спрашивает класс")

            jtask(kind="app", action="add_student", params={"name": "Ваня Иванов", "grade": 5},
                  question="Какая цена за занятие?")
            j = await (await post("/api/voice", {"ctx": CTX, "text": "5"})).json()
            assert j["stage"] == "clarify" and "цен" in j["reply"].lower(), j
            assert "Не хватает данных" not in j["reply"], j
            ok("шаг 2: класс числом (5) не потерян — вопрос ровно про цену")

            jtask(kind="app", action="add_student",
                  params={"name": "Ваня Иванов", "grade": 5, "price": "3000 руб"})
            j = await (await post("/api/voice", {"ctx": CTX, "text": "три тысячи"})).json()
            assert j["stage"] == "confirm", (j, "после цены должно быть подтверждение, а не «не хватает данных»")
            assert "Ваня Иванов" in j["confirm"] and "3 000" in j["confirm"], j
            ok("шаг 3: цена текстом разобрана → подтверждение с полными данными")

            assert student("Ваня Иванов") is None, "до подтверждения ученик не создаётся"
            j = await (await post("/api/voice_confirm", {})).json()
            assert j["ok"] and j["action"] == "add_student", j
            s = student("Ваня Иванов")
            assert s and s["grade"] == "5" and s["price"] == 3000, s
            ok("«Применить» → ученик сохранён: Ваня Иванов, 5, 3000")

            # --- 2. модель вообще не повторяет старые параметры — контекст держит сервер ---
            jtask(kind="app", action="add_student", params={"name": "Света Кузнецова"}, question="Какой класс?")
            r = await assist.handle_chat(201, "добавь ученика Света Кузнецова")
            assert "класс" in r["reply"].lower(), r

            jtask(kind="app", action="add_student", params={"grade": "9"}, question="Какая цена за занятие?")
            r = await assist.handle_chat(201, "девятый")
            assert "цен" in r["reply"].lower() and "Света" not in r["reply"], r
            assert "Не хватает данных" not in r["reply"] and "зовут" not in r["reply"].lower(), r
            ok("модель не повторила имя — сервер помнит его сам и спрашивает цену, а не имя заново")

            jtask(kind="app", action="add_student", params={"price": 2500})
            r = await assist.handle_chat(201, "две тысячи пятьсот")
            assert "Света Кузнецова" in r["reply"] and "2 500" in r["reply"], r
            assert "Применить" in str(r["buttons"]), r
            r = await assist.handle_reply(201, "ai:ok")
            s = student("Света Кузнецова")
            assert s and s["grade"] == "9" and s["price"] == 2500, s
            ok("три ответа по одному параметру собрались в полную карточку → ученик сохранён")

            # --- 3. неразобранное значение переспрашивается, а не «не хватает данных» ---
            jtask(kind="app", action="add_student", params={"name": "Пётр Сидоров"}, question="Какой класс?")
            r = await assist.handle_chat(202, "Пётр Сидоров")
            jtask(kind="app", action="add_student", params={"name": "Пётр Сидоров", "grade": 11},
                  question="Какая цена за занятие?")
            r = await assist.handle_chat(202, "одиннадцатый")
            jtask(kind="app", action="add_student",
                  params={"name": "Пётр Сидоров", "grade": 11, "price": "тысяча двести"})
            r = await assist.handle_chat(202, "тысяча двести")
            assert "Не хватает данных" not in r["reply"], r
            assert "цен" in r["reply"].lower(), r
            ok("цену словами не разобрать → её переспрашивают, диалог не упирается в тупик")

            jtask(kind="app", action="add_student",
                  params={"name": "Пётр Сидоров", "grade": 11, "price": 1200})
            r = await assist.handle_chat(202, "1200")
            assert "Применить" in str(r["buttons"]), r
            await assist.handle_reply(202, "ai:cancel")
            assert student("Пётр Сидоров") is None
            ok("после переспроса цена принята; отмена — ученик не создан")

        # --- 4. нормализация: числа в текстовых полях и текст в числах ----------
        assert assist.norm_text(5, 20) == "5" and assist.norm_text(5.0, 20) == "5"
        assert assist.norm_text(" 9 ", 20) == "9" and assist.norm_text(True, 20) is None
        ok("класс числом (5, 5.0) превращается в текст «5»; true — нет")
        assert assist.norm_int(3000.0, 0, 10 ** 9) == 3000
        assert assist.norm_int("3000 руб", 0, 10 ** 9) == 3000
        assert assist.norm_int("3 000", 0, 10 ** 9) == 3000
        assert assist.norm_int("тысяча двести", 0, 10 ** 9) is None
        assert assist.norm_int(True, 0, 10 ** 9) is None
        ok("цена принимается числом (3000.0) и текстом («3000 руб», «3 000»); слова и true — нет")
        assert assist.norm_day(20261017) == "2026-10-17"
        ok("дата числом (20261017) тоже разбирается — как и положено, в YYYY-MM-DD")

    print("OK")


asyncio.run(main())
