"""Ассистент: разбор задачи (Kimi) → уточнение → подтверждение → исполнение в базе.
Голос → текст (Whisper) → общий конвейер. Общие вопросы: быстрый ответ дешёвой моделью или
глубокий разбор kimi-k3. Сеть подменена фейковыми серверами, база — в памяти.
Исполняется только подтверждённое — проверяется явно."""
import asyncio, base64, hashlib, hmac, json, os, sqlite3, sys, time
from datetime import date, timedelta
from urllib.parse import urlencode
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOK = "123456:TESTTOKEN"
os.environ.update(BOT_TOKEN=TOK, AUTH_LOGIN="Admin", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID", "KIMI_API_KEY", "KIMI_BASE_URL", "KIMI_MODEL",
          "WHISPER_API_KEY", "WHISPER_BASE_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import assist, auth, bot, kimi, whisper
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
def ok(c): print("  ✓", c)

# --- база в памяти: двое учеников и одно занятие «сегодня+1 в 10:00» (его трогать нельзя) ---
mem = sqlite3.connect(":memory:", check_same_thread=False); mem.row_factory = sqlite3.Row
bot.init_db(mem); bot.db = mem; auth.init(mem); auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp())); mem.commit()
TODAY = date.today()
DAY1 = TODAY.isoformat()                       # «сегодня» — для проверки нормализации дат
DAY2 = (TODAY + timedelta(days=1)).isoformat() # день занятого занятия
mem.execute("insert into students(name,price,grade,subject) values('Анна Иванова',1500,'11','математика')", ())
mem.execute("insert into students(name,price,grade,subject) values('Борис Смирнов',1000,'9','физика')", ())
ANNA = mem.execute("select id from students where name='Анна Иванова'").fetchone()[0]
mem.execute("insert into lessons(student_id,day,time,dur) values(?,?,?,60)", (ANNA, DAY2, "10:00"))
mem.commit()

# --- фейковые API Kimi и Whisper ---------------------------------------
QUEUE = []          # что «модель» отвечает по очереди
CALLS = []          # (тип, заголовки, тело)
STT_TEXT = ["добавь занятие Анне завтра в 15:00"]

async def fake_kimi(request):
    body = await request.json(); CALLS.append(("chat", dict(request.headers), body))
    content = QUEUE.pop(0) if QUEUE else "{}"
    return web.json_response({"choices": [{"message": {"content": content}}]})

async def fake_stt(request):
    CALLS.append(("stt", dict(request.headers), dict(await request.post())))
    return web.json_response({"text": STT_TEXT[0]})

fk = web.Application()
fk.router.add_post("/chat/completions", fake_kimi)
fk.router.add_post("/audio/transcriptions", fake_stt)

def jtask(**kw):
    QUEUE.append(json.dumps(kw, ensure_ascii=False))

def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)
H = {"X-Init": init(42, "Gladik_N")}
AUD = base64.b64encode(b"\x1aE\xdf\xa3fake-webm").decode()

def lessons(day=None, t=None):
    return [tuple(r) for r in mem.execute("select student_id,day,time from lessons where (? is null or day=?) and (? is null or time=?)", (day, day, t, t))]

def payments():
    return [tuple(r) for r in mem.execute("select student_id,amount from payments")]

async def main():
    async with TestClient(TestServer(fk)) as c:
        kimi.BASE = whisper.BASE = str(c.make_url("")).rstrip("/")
        kimi.API_KEY = "TESTKEY"; whisper.API_KEY = "STTKEY"

        # --- 1. неполная задача → уточнение → подтверждение → исполнение ----
        jtask(kind="app", action="add_lesson", params={"student": "Анна"}, question="Когда?")
        r = await assist.handle_chat(101, "запиши Анну на занятие")
        assert "На какой день" in r["reply"] and r["buttons"] and lessons(DAY1, "12:00") == []
        ok("задача без даты → бот спрашивает недостающее, ничего не делает")

        jtask(kind="app", action="add_lesson", params={"student": "Анна Иванова", "day": DAY1, "time": "12:00"})
        r = await assist.handle_chat(101, "сегодня в 12:00")
        assert "Выполнить" in str(r["buttons"]) and "Анна Иванова" in r["reply"] and "12:00" in r["reply"]
        assert lessons(DAY1, "12:00") == [], "до подтверждения занятие не создаётся"
        ok("параметры собраны → показано, что именно будет сделано (ещё без изменений)")

        r = await assist.handle_reply(101, "ai:ok")
        assert "Занятие добавлено" in r["reply"] and lessons(DAY1, "12:00") == [(ANNA, DAY1, "12:00")]
        ok("после «Выполнить» занятие записано в базу")

        # --- 2. оплата: сначала отмена, потом подтверждение -----------------
        jtask(kind="app", action="add_payment", params={"student": ANNA, "amount": 1500})
        r = await assist.handle_chat(102, "запиши оплату 1500 от Анны")
        assert "Записать оплату" in r["reply"]
        r = await assist.handle_reply(102, "ai:cancel")
        assert payments() == [] and "отменил" in r["reply"].lower()
        ok("отмена → ни одного изменения в базе")

        jtask(kind="app", action="add_payment", params={"student": "Борис", "amount": "2000"})
        r = await assist.handle_chat(102, "Борис внес 2000")
        assert "Борис Смирнов" in r["reply"] and "2 000" in r["reply"]
        r = await assist.handle_reply(102, "ai:ok")
        assert payments() == [(ANNA and mem.execute("select id from students where name='Борис Смирнов'").fetchone()[0], 2000)]
        ok("оплата проведена после подтверждения; имя и сумма нормализованы")

        # --- 3. занятое время → честная ошибка, база не меняется ------------
        jtask(kind="app", action="add_lesson", params={"student": "Борис", "day": DAY2, "time": "10:00"})
        await assist.handle_chat(103, "поставь Борису завтра в 10:00")
        r = await assist.handle_reply(103, "ai:ok")
        assert "занято" in r["reply"] and lessons(DAY2, "10:00") == [(ANNA, DAY2, "10:00")]
        ok("конфликт по времени ловится до базы, чужое занятие не перезаписано")

        # --- 4. общий вопрос: быстрый / глубокий ----------------------------
        jtask(kind="chat", action="none", params={}, question="")
        r = await assist.handle_chat(104, "напиши сочинение про весну")
        assert [b[1] for b in r["buttons"]] == ["ai:fast", "ai:deep"]
        ok("не задача для кабинета → предложены «быстрый» и «глубокий» режимы")

        QUEUE.append("Короткий ответ.")
        r = await assist.handle_reply(104, "ai:fast")
        assert r["reply"] == "Короткий ответ." and CALLS[-1][2]["model"] == "kimi-k2.6"
        ok("быстрый режим → дешёвая модель kimi-k2.6")

        jtask(kind="chat", action="none", params={}, question="")
        await assist.handle_chat(104, "разбери задачу по физике подробно")
        QUEUE.append("Длинный разбор.")
        r = await assist.handle_reply(104, "ai:deep")
        b = CALLS[-1][2]
        assert r["reply"] == "Длинный разбор." and b["model"] == "kimi-k3" and b["reasoning_effort"] == "max"
        ok("глубокий режим → kimi-k3 с reasoning_effort=max")

        # --- 5. мини-апп: команда со страницы → подтверждение → действие ----
        async with TestClient(TestServer(bot.make_app())) as wc:
            post = lambda p, j: wc.post(p, json=j, headers=H)
            ctx = {"tab": "c", "screen": None, "screen_args": []}
            jtask(kind="app", action="add_lesson",
                  params={"student": "Анна", "day": DAY1, "time": "15:00", "dur": 30})
            r = await post("/api/voice", {"ctx": ctx, "text": "поставь Анне занятие сегодня в 15:00 на полчаса"})
            j = await r.json()
            assert r.status == 200 and j["stage"] == "confirm" and "Анна" in j["confirm"] and "15:00" in j["confirm"]
            assert lessons(DAY1, "15:00") == []
            ok("мини-апп: команда со страницы расписания → предложение действия, ещё без изменений")

            r = await post("/api/voice_confirm", {})
            j = await r.json()
            assert j["ok"] and "Занятие добавлено" in j["reply"] and lessons(DAY1, "15:00") != []
            ok("подтверждение в мини-аппе → занятие записано")

            # отмена: предложение действия отменяется без изменений
            jtask(kind="app", action="add_lesson", params={"student": "Анна", "day": DAY1, "time": "16:00"})
            await post("/api/voice", {"ctx": ctx, "text": "ещё одно занятие в 16:00"})
            r = await post("/api/voice_cancel", {})
            j = await r.json()
            assert j["ok"] and lessons(DAY1, "16:00") == []
            ok("«Отмена» в мини-аппе → ничего не изменилось")

            # --- 6. голос: запись → Whisper → общий конвейер ----------------
            jtask(kind="app", action="add_lesson",
                  params={"student": "Борис", "day": DAY1, "time": "18:00"})
            r = await post("/api/voice", {"ctx": ctx, "audio": AUD, "mime": "audio/webm", "dur": 2})
            j = await r.json()
            assert j["stage"] == "confirm" and "добавь занятие" in j["heard"] and "🎤" not in j["reply"]
            assert CALLS[-1][1] and CALLS[-2][0] == "stt"   # сначала STT, потом разбор
            r = await post("/api/voice_confirm", {})
            j = await r.json()
            assert j["ok"] and j["action"] == "add_lesson" and j["info"]["day"] == DAY1 and j["info"]["time"] == "18:00" and j["info"]["sid"]
            assert lessons(DAY1, "18:00") != []
            ok("голос из мини-аппа: Whisper → разбор → подтверждение → занятие записано")

        # --- 7. без ключей всё деградирует вежливо -------------------------
        kimi.API_KEY = ""
        r = await assist.handle_chat(105, "что у нас по расписанию?")
        assert "KIMI_API_KEY" in r["reply"]
        kimi.API_KEY = "TESTKEY"; whisper.API_KEY = ""
        async with TestClient(TestServer(bot.make_app())) as wc:
            r = await wc.post("/api/voice", json={"ctx": {"tab": "p", "screen": "payment_new", "screen_args": [5]},
                                                  "audio": AUD, "mime": "audio/webm"}, headers=H)
            j = await r.json()
            assert j["stage"] == "stub" and "Оплаты" in j["reply"] and "WHISPER_API_KEY" in j["reply"]
        ok("без KIMI_API_KEY и без WHISPER_API_KEY — вежливые заглушки, ничего не падает")
asyncio.run(main())
print("OK")
