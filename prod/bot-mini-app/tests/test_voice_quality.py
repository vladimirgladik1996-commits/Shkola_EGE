"""Качество голосового ввода: поиск ученика без архивных дублей, тишина ≠ команда, переспросы только по
недостающему, время «17.00», без вопроса о длительности. Сеть подменена, база — в памяти."""
import asyncio, os, sqlite3, sys
from datetime import date, timedelta
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID", "KIMI_API_KEY", "WHISPER_API_KEY", "WHISPER_BASE_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import assist, bot, whisper, kimi, json
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
def ok(c): print("  ✓", c)

mem = sqlite3.connect(":memory:", check_same_thread=False); mem.row_factory = sqlite3.Row
bot.init_db(mem); bot.db = mem
def add(name, deleted=0, price=1500):
    cur = mem.execute("insert into students(name,price,grade,subject,deleted) values(?,?,'9','русский',?)", (name, price, deleted))
    mem.commit(); return cur.lastrowid

# ================= 1. «Таких вариантов много» при одном человеке =================
ANNA = add("Анна Иванова")
ARCH = [add("Анна Иванова", deleted=1) for _ in range(3)]          # три архивные копии того же имени
assert assist.find_student("Анна")["id"] == ANNA
assert assist.find_student("анна иванова")["id"] == ANNA and assist.find_student("Анну Иванову")["id"] == ANNA
ok("одна действующая «Анна Иванова» + три архивные копии → находится действующая, без «нашёл несколько похожих»")
add("Анна Петрова")
try:
    assist.find_student("Анна"); assert False
except assist.AssistError as e:
    msg = str(e); assert "несколько похожих" in msg and msg.count("Анна Иванова") == 1 and "Анна Петрова" in msg
ok("два РАЗНЫХ человека «Анна» — это неоднозначность, но имя в списке не повторяется")
B1, B2 = add("Борис Смирнов"), add("Борис Смирнов")
assert assist.find_student("Борис")["id"] == B2
ok("две действующие записи с одним именем — не неоднозначность: берётся самая свежая")
GONE = add("Виктор Орлов", deleted=1)
for q in ("Виктор", "Виктор Орлов", "Витя Орлов", str(GONE)):
    try:
        assist.find_student(q); assert False, q
    except assist.AssistError as e:
        assert "не нашёл" in str(e) or "нет" in str(e), (q, str(e))
ok("удалённый ученик не находится вообще: ни по имени, ни по искажённому имени, ни по номеру (запасного поиска по удалённым нет)")
assist.find_student("Борис Смирнов")   # действующий находится без ошибок
assert assist.find_student(str(ANNA))["id"] == ANNA and assist.find_student(ANNA)["id"] == ANNA
ok("действующих находит и по номеру")
snap = bot.voice_snapshot()
assert "в архиве" not in snap and snap.count("Анна Иванова") == 1 and "Виктор Орлов" not in snap and f"#{GONE} " not in snap
assert f"#{ARCH[0]} " not in snap and f"#{ANNA} " in snap
ok("снимок кабинета для модели: архивных учеников и копий нет вовсе")

# ================= 2. Тишина → выдуманный текст =================
SIL = {"text": "Продолжение следует...", "segments": [{"text": "Продолжение следует...", "no_speech_prob": 0.93, "avg_logprob": -1.4, "compression_ratio": 1.0}]}
assert whisper.clean(SIL, 3.0) == ""
assert whisper.clean({"text": "Спасибо.", "segments": [{"text": "Спасибо.", "no_speech_prob": 0.7, "avg_logprob": -1.5}]}) == ""
assert whisper.clean({"text": "", "segments": []}, 2) == "" and whisper.clean({"text": "   "}) == ""
ok("тишина: сегменты с высоким no_speech_prob и пустой текст → «речи нет»")
SPEECH = {"text": "Добавь занятие Анне завтра в пятнадцать", "segments": [{"text": "Добавь занятие Анне завтра в пятнадцать", "no_speech_prob": 0.02, "avg_logprob": -0.2, "compression_ratio": 1.3}]}
assert whisper.clean(SPEECH, 4.0) == "Добавь занятие Анне завтра в пятнадцать"
assert whisper.clean({"text": "ок"}, 0.4) == "ок"                       # короткая реплика и короткая запись — не режем
ok("настоящая речь проходит без изменений")
assert whisper.clean({"text": "а" * 120}, 2.0) == ""                    # 60 символов/с — человек так не говорит
assert whisper.clean({"text": "а" * 100, "duration": 2.0}) == ""        # длительность можно взять и из ответа сервиса
assert whisper.clean({"text": "а" * 100}, 10.0) != ""
ok("физически невозможная скорость (> 25 символов/с по длительности записи) отсекается")
LOOP = {"text": "Да да да да да да", "segments": [{"text": "Да да да да да да", "no_speech_prob": 0.1, "avg_logprob": -0.4, "compression_ratio": 3.1}]}
assert whisper.clean(LOOP, 5) == ""
MIX = {"text": "привет Продолжение следует", "segments": [
    {"text": "привет", "no_speech_prob": 0.05, "avg_logprob": -0.3, "compression_ratio": 1.1},
    {"text": "Продолжение следует", "no_speech_prob": 0.95, "avg_logprob": -1.7, "compression_ratio": 1.1}]}
assert whisper.clean(MIX, 6) == "привет"
ok("зацикленный повтор отсекается; из смеси речи и «хвоста-галлюцинации» остаётся только речь")

SEEN = []
async def fake_stt(request):
    SEEN.append(dict(await request.post()))
    return web.json_response(RESP[0])
RESP = [SIL]
QUEUE, KCALLS = [], []
async def fake_kimi(request):
    await request.json(); KCALLS.append(1)
    return web.json_response({"choices": [{"message": {"content": QUEUE.pop(0) if QUEUE else "{}"}}]})
def jtask(**kw): QUEUE.append(json.dumps(kw, ensure_ascii=False))
fk = web.Application(); fk.router.add_post("/audio/transcriptions", fake_stt); fk.router.add_post("/chat/completions", fake_kimi)

async def main():
    async with TestClient(TestServer(fk)) as c:
        whisper.BASE = kimi.BASE = str(c.make_url("")).rstrip("/"); whisper.API_KEY = "k"; kimi.API_KEY = "KEY"
        RESP[0] = SIL
        assert await whisper.transcribe(b"\x1aE\xdf\xa3x", "audio/webm", prompt="Ученики: Анна.", dur=3.0) == ""
        f = SEEN[-1]
        assert f["temperature"] == "0" and f["response_format"] == "verbose_json"
        assert "ничего не придумывай" in f["prompt"] and "Ученики: Анна." in f["prompt"] and len(f["prompt"]) <= 500
        ok("запрос к Whisper: temperature=0, verbose_json, подсказка «ничего не придумывай» + имена учеников; тишина → \"\"")
        RESP[0] = SPEECH
        assert await whisper.transcribe(b"\x1aE\xdf\xa3x", "audio/webm", dur=4.0) == SPEECH["text"]
        RESP[0] = {"text": "просто текст без сегментов"}                  # сервис не вернул segments — работаем по тексту
        assert await whisper.transcribe(b"\x1aE\xdf\xa3x", "audio/webm") == "просто текст без сегментов"
        RESP[0] = {"error": "x"}
        assert await whisper.transcribe(b"\x1aE\xdf\xa3x", "audio/webm") is None
        ok("речь → текст; нет segments → по тексту; кривой ответ → None (сбой), а не «тишина»")

        # виджет: тишина — честный ответ, сбой — прежнее сообщение; в разбор команды ничего не уходит
        CTX = {"tab": "p", "tab_name": "Оплаты", "screen": None}
        called = []
        async def fake_page(*a, **k): called.append(a); return {"stage": "answer", "reply": "x", "confirm": None}
        real_tr, real_page = whisper.transcribe, assist.handle_page
        assist.handle_page = fake_page
        try:
            async def tr_silent(*a, **k): return ""
            async def tr_fail(*a, **k): return None
            async def tr_ok(*a, **k): return "запиши оплату Анне три тысячи"
            whisper.transcribe = tr_silent
            r = await bot.voice_handle(1, CTX, b"x", "audio/webm", "", dur=2)
            assert r["reply"] == whisper.NO_SPEECH_MSG and "молчали" in r["reply"] and not called
            whisper.transcribe = tr_fail
            r = await bot.voice_handle(1, CTX, b"x", "audio/webm", "", dur=2)
            assert "Не получилось распознать" in r["reply"] and not called
            whisper.transcribe = tr_ok
            r = await bot.voice_handle(1, CTX, b"x", "audio/webm", "", dur=2)
            assert r["heard"] == "запиши оплату Анне три тысячи" and called
        finally:
            whisper.transcribe, assist.handle_page = real_tr, real_page
        ok("виджет: тишина → «Не услышал речи в записи — похоже, вы молчали», сбой → прежняя заглушка, речь → в разбор")
        assert bot.voice_dur({"dur": 3.2}) == 3.2 and bot.voice_dur({"dur": "x"}) is None and bot.voice_dur({"dur": -1}) is None and bot.voice_dur({}) is None
        ok("длительность записи от мини-аппа читается и проверяется")
        await flows(c)
# ================= 5. Сквозной сценарий виджета: без дублирующих запросов =================
PAY = lambda: [tuple(r) for r in mem.execute("select student_id,amount from payments")]
async def flows(c):
    if True:
        CTX = lambda tab, name: {"tab": tab, "tab_name": name, "screen": None}
        CHECKS = ["Сумма 4000, верно?", "Вы сказали четыре тысячи рублей?", "Ученик — Анна?", "Подтвердите сумму", "Во сколько?", "Сколько минут длится занятие?"]
        # (а) имя и сумма названы сразу → ОДИН экран подтверждения (кнопки), ни одного вопроса, ни одного повторного запроса
        KCALLS.clear(); jtask(kind="app", action="add_payment", params={"student": "Анна Иванова", "amount": 4000}, check=CHECKS)
        r = await assist.handle_page(301, "Анна, четыре тысячи рублей", CTX("p", "Оплаты"), voice=True)
        assert r["stage"] == "confirm" and r["confirm"], r
        assert r["fields"] == [{"k": "Ученик", "v": "Анна Иванова"}, {"k": "Сумма", "v": bot.money(4000)}], r.get("fields")
        assert len(KCALLS) == 1 and not QUEUE and PAY() == []          # одно обращение к модели; оплата ещё не записана
        res = await assist.confirm_page(301)
        assert res.get("ok") and PAY() == [(ANNA, 4000)], (res, PAY())  # «Применить» — оплата записана ровно один раз
        ok("оплата: «Анна, четыре тысячи рублей» → сразу одно финальное подтверждение (кнопки); переспросов «верно ли сумма» нет; после «Применить» оплата записана один раз")
        # (б) сумма не названа/не понята → единственный вопрос — про сумму; потом сразу подтверждение
        KCALLS.clear(); jtask(kind="app", action="add_payment", params={"student": "Анна Иванова"}, check=["Ученик Анна?"])
        r1 = await assist.handle_page(302, "запиши оплату Анне", CTX("p", "Оплаты"), voice=True)
        assert r1["stage"] == "clarify" and "сумма" in r1["reply"].lower() and not r1.get("fields"), r1
        jtask(kind="app", action="add_payment", params={"student": "Анна Иванова", "amount": 3000}, check=["Сумма 3000, верно?"])
        r2 = await assist.handle_page(302, "три тысячи", CTX("p", "Оплаты"), voice=True)
        assert r2["stage"] == "confirm" and r2["fields"][1]["v"] == bot.money(3000), r2
        assert len(KCALLS) == 2 and not QUEUE
        await assist.cancel_page(302) if asyncio.iscoroutinefunction(assist.cancel_page) else assist.cancel_page(302)
        ok("оплата без суммы: ровно один вопрос — про сумму, затем сразу итоговая карточка (имя не переспрашивается)")
        # (в) занятие: всё названо → без вопросов; даже если модель просит «Во сколько?» и про минуты
        d = (date.today() + timedelta(days=3)).isoformat()
        KCALLS.clear(); jtask(kind="app", action="add_lesson", params={"student": "Анна Иванова", "day": d, "time": "17.00"}, check=CHECKS)
        r = await assist.handle_page(303, "Анне в четверг в семнадцать ноль-ноль", CTX("c", "Расписание"), voice=True)
        assert r["stage"] == "confirm" and r["fields"][2]["v"] == "60 мин", r
        assert len(KCALLS) == 1
        assist.cancel_page(303)
        # (г) ученик: имя, класс, цена названы → без вопросов (предмет не называли — не обязателен)
        KCALLS.clear(); jtask(kind="app", action="add_student", params={"name": "Лёва Тестов", "grade": "8", "price": 900}, check=["Какой класс?", "Цена 900, верно?", "Лёва Тестов?"])
        r = await assist.handle_page(304, "добавь Лёву, восьмой класс, девятьсот", CTX("s", "Ученики"), voice=True)
        assert r["stage"] == "confirm" and [f["k"] for f in r["fields"]] == ["Имя", "Класс", "Предмет", "Цена за урок"] and len(KCALLS) == 1, r
        assist.cancel_page(304)
        ok("занятие и ученик: всё названо сразу → одно подтверждение, ни «Во сколько?», ни «Сколько минут?», ни «Цена 900, верно?»")

asyncio.run(main())


# ================= 3. norm_time и вопрос с уже известным ответом =================
for raw, want in (("17.00", "17:00"), ("17 00", "17:00"), ("17-00", "17:00"), ("17,00", "17:00"), ("17:00", "17:00"),
                  ("1700", "17:00"), ("17", "17:00"), ("9:05", "09:05"), ("12.00", "12:00"), (15.0, "15:00"), (15, "15:00")):
    assert assist.norm_time(raw) == want, (raw, assist.norm_time(raw))
for raw in ("25:00", "17.5", "24.00", "17:60", "abc", "", None, "17:00:00"):
    assert assist.norm_time(raw) is None, raw
ok("norm_time принимает 17.00 / 17 00 / 17-00 / 17,00 / 1700 и по-прежнему отбрасывает мусор")
P = {"student": {"id": 1, "name": "Анна"}, "day": "2026-10-22", "time": "17:00", "dur": 60, "regular": False}
assert not assist.check_wanted("Во сколько?", "add_lesson", P)            # время известно (17:00 распознано)
assert not assist.check_wanted("Во сколько?", "add_payment", {"student": P["student"], "amount": 3000})   # поле чужого действия
assert not assist.check_wanted("На какой день?", "add_lesson", P)
assert not assist.check_wanted("Сумма 3000, верно?", "add_payment", {"student": P["student"], "amount": 3000})
assert not assist.check_wanted("Ученик — Анна Иванова?", "add_lesson", P)
assert assist.check_wanted("Во сколько?", "add_lesson", {**P, "time": None})  # данных нет — спрашиваем
assert assist.check_wanted("Какой предмет?", "add_student", {"name": "Ваня", "grade": "9", "price": 0, "subject": None})
assert not assist.check_wanted("Какой предмет?", "add_student", {"name": "Ваня", "grade": "9", "price": 0, "subject": "физика"})
assert not assist.check_wanted("Что именно записать?", "add_lesson", P)        # непривязанный к полю вопрос — это «подтвердите», а подтверждение одно
assert not assist.check_wanted("Вы сказали четыре тысячи рублей, верно?", "add_payment", {"student": P["student"], "amount": 4000})
ok("check_wanted: известное (даже неуверенное), чужое и про длительность/повтор — не спрашиваем; чего нет — спрашиваем")
task = {"action": "add_lesson", "params": P, "check": ["Во сколько?", "Сколько минут длится занятие?", "Занятие регулярное?", "Ученик — Анна Иванова?"]}
assert assist.next_check(task, [], "add_lesson", P) is None
assert assist.next_check({**task, "check": ["Во сколько?"]}, [], "add_lesson", {**P, "time": None}) == "Во сколько?"
assert assist.next_check({**task, "check": ["Во сколько?"]}, ["Во сколько?"], "add_lesson", {**P, "time": None}) is None
ok("next_check: все четыре лишних переспроса отброшены; нужный задаётся один раз")

# ================= 4. Длительность =================
assert "dur" not in assist.ASK and "минут длится" not in " ".join(assist.ASK.values())
pr, need = assist.prepare({"student": "Анна Иванова", "day": (date.today() + timedelta(days=2)).isoformat(), "time": "17.00"}, "add_lesson")
assert pr["dur"] == 60 and pr["time"] == "17:00" and not need and pr["regular"] is False
pr, need = assist.prepare({"student": "Анна Иванова", "day": (date.today() + timedelta(days=2)).isoformat(), "time": "17 00", "dur": 90}, "add_lesson")
assert pr["dur"] == 90 and not need
assert "Длительность" in assist.PROMPT and "НЕ переспрашивай" in assist.PROMPT
assert assist.question_for(["dur"], "add_lesson", []) == "Уточните, пожалуйста."   # вопроса про минуты в коде нет
ok("длительность: вопроса нет нигде, по умолчанию 60 минут, 90 — только если человек сам назвал")
print("OK")
