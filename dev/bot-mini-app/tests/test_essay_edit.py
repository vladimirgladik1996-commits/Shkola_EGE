"""Правка распознанного текста словами Вишенке (essayedit + POST /api/essay_check/edit). Кими и Whisper подменены.
Запуск: python3 tests/test_essay_edit.py"""
import asyncio, base64, hashlib, hmac, json, os, sqlite3, sys, time
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def ok(c): print("  ✓", c)
os.environ.update(BOT_TOKEN="***", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import auth, bot, essayedit, kimi, whisper
from aiohttp.test_utils import TestClient, TestServer

# ---------- 1. normalize ----------
n = essayedit.normalize
assert n({"op": "replace", "replacement": "праздник", "say": "Поняла: заменю «празник» на «праздник»."}, "празник") == \
    {"op": "replace", "replacement": "праздник", "say": "Поняла: заменю «празник» на «праздник»."}
r = n({"op": "replace", "replacement": "праздник"}, "празник")
assert r["op"] == "replace" and "праздник" in r["say"]
assert n({"op": "replace", "replacement": "", "say": "Уберу."}, "лишнее")["replacement"] == ""
assert n({"op": "replace", "replacement": "", "say": ""}, "лишнее")["say"].startswith("Поняла: уберу")
ok("normalize: замена принимается; пустая замена = удаление; без say подставляется понятная фраза")
for bad in (None, [], "текст", {"op": "replace"}, {"op": "replace", "replacement": 5}, {"op": "delete", "replacement": "x"},
            {"op": "replace", "replacement": "я" * 1600}):
    r = n(bad, "слово"); assert r["op"] == "unclear" and r["replacement"] == "" and r["say"], bad
r = n({"op": "unclear", "say": "На какое слово заменить?"}, "слово"); assert r == {"op": "unclear", "replacement": "", "say": "На какое слово заменить?"}
r = n({"op": "replace", "replacement": "  слово  ", "say": "x"}, "слово"); assert r["op"] == "unclear" and "уже написано" in r["say"]
r = n({"op": "replace", "replacement": "сло\x00во\x07 ещё", "say": "ок"}, "слово"); assert r["replacement"] == "слово ещё"
ok("normalize: мусор/не тот op/слишком длинное → unclear; «ничего не меняется» → вопрос; управляющие символы вычищаются")
assert essayedit.clean("  а  \t б\x00\n", 50) == "а б"
assert essayedit.clean(5, 10) == "" and len(essayedit.clean("я" * 999, 10)) == 10
ok("clean: пробелы схлопываются, не-строка → '', длина режется")

# ---------- 2. interpret ----------
seen = []
async def fake_ask_json(messages, **kw):
    seen.append(messages[-1]["content"]); return state["answer"]
state = {"answer": {"op": "replace", "replacement": "праздник", "say": "Поняла."}}
orig = kimi.ask_json; kimi.ask_json = fake_ask_json
try:
    r = asyncio.run(essayedit.interpret("празник", "Я люблю", "и книги", "замени на праздник, это неправильно распознано"))
    assert r["op"] == "replace" and r["replacement"] == "праздник"
    p = seen[-1]
    for s in ("«празник»", "«Я люблю»", "«и книги»", "замени на праздник, это неправильно распознано", "буква в букву", "не исправляй орфографию"):
        assert s in p, s
    async def boom(messages, **kw): raise RuntimeError("down")
    kimi.ask_json = boom
    r = asyncio.run(essayedit.interpret("празник", "", "", "замени")); assert r["op"] == "unclear"
finally:
    kimi.ask_json = orig
ok("interpret: в промт попадают фрагмент, контекст и слова ученика, запрет «исправлять»; сбой модели → unclear")

# ---------- 3. API ----------
mem = sqlite3.connect(":memory:", check_same_thread=False); bot.db.backup(mem); mem.row_factory = sqlite3.Row
bot.db = mem; auth.init(mem); auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)"); mem.execute("insert into auth_pins values('hungerrr', 77)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp()))
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(77,?,?)", (time.time() + 99999, auth._fp())); mem.commit()
def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", bot.TOKEN.encode(), hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)
H, H2 = {"X-Init": init(42, "Gladik_N")}, {"X-Init": init(77, "hungerrr")}
URL = "/api/essay_check/edit"
AUDIO = {"audio": base64.b64encode(b"\x1aE\xdf\xa3fake-webm").decode(), "mime": "audio/webm", "dur": 2.5}
kimi.API_KEY = "k"; whisper.API_KEY = "w"
calls = {"stt": [], "ask": []}
async def fake_transcribe(audio, mime="audio/webm", prompt="", dur=None):
    calls["stt"].append((len(audio), mime, prompt, dur)); return state["heard"]
async def fake_ask(messages, **kw):
    calls["ask"].append(messages[-1]["content"]); return state["answer"]
whisper.transcribe, kimi.ask_json = fake_transcribe, fake_ask
state["heard"] = "замени это слово на праздник, оно неправильно распознано"
bot.EC_CHECKS["with-ocr"] = [42, {"w": [], "t": [], "ocr": {"essay": "x"}}, time.time()]
bot.EC_CHECKS["no-ocr"] = [42, {"w": [], "t": []}, time.time()]
BODY = {"id": "with-ocr", "fragment": "празник", "before": "Я люблю", "after": "и книги"}

async def main():
    async with TestClient(TestServer(bot.make_app())) as c:
        post = lambda j, h=H: c.post(URL, json=j, headers=h)
        # текстом
        r = await post({**BODY, "text": "замени на праздник"}); j = await r.json()
        assert r.status == 200 and j == {"ok": True, "op": "replace", "replacement": "праздник", "say": "Поняла."}, j
        assert not calls["stt"] and "замени на праздник" in calls["ask"][-1]
        ok("текстом: Вишенка возвращает замену фрагмента, Whisper не нужен")
        # голосом
        r = await post({**BODY, **AUDIO}); j = await r.json()
        assert r.status == 200 and j["op"] == "replace" and j["replacement"] == "праздник" and j["heard"] == state["heard"], j
        assert calls["stt"][-1][0] > 0 and calls["stt"][-1][1] == "audio/webm" and "празник" in calls["stt"][-1][2] and calls["stt"][-1][3] == 2.5
        assert state["heard"] in calls["ask"][-1]
        ok("голосом: запись → Whisper (с подсказкой-фрагментом) → разбор; расшифровка возвращается в heard")
        # не понял
        state["answer"] = {"op": "unclear", "replacement": "", "say": "На какое слово заменить?"}
        j = await (await post({**BODY, "text": "поправь"})).json()
        assert j["op"] == "unclear" and j["replacement"] == "" and j["say"] == "На какое слово заменить?"
        state["answer"] = None
        j = await (await post({**BODY, "text": "поправь"})).json()
        assert j["op"] == "unclear" and j["say"]
        state["answer"] = {"op": "replace", "replacement": "праздник", "say": "Поняла."}
        ok("неясная просьба и сбой модели → unclear (ничего не меняется)")
        # нет речи / не распозналось / нет ключей
        state["heard"] = ""
        j = await (await post({**BODY, **AUDIO})).json(); assert j["stage"] == "stub" and j["reply"] == whisper.NO_SPEECH_MSG
        state["heard"] = None
        j = await (await post({**BODY, **AUDIO})).json(); assert j["stage"] == "stub" and "Не получилось распознать" in j["reply"]
        state["heard"] = "ок"
        whisper.API_KEY = ""
        j = await (await post({**BODY, **AUDIO})).json(); assert j["stage"] == "stub" and "WHISPER_API_KEY" in j["reply"]
        j = await (await post({**BODY, "text": "замени"})).json(); assert j["op"] == "replace", "текстом Whisper не нужен"
        whisper.API_KEY = "w"; kimi.API_KEY = ""
        j = await (await post({**BODY, "text": "замени"})).json(); assert j["stage"] == "stub" and "KIMI_API_KEY" in j["reply"]
        kimi.API_KEY = "k"
        ok("нет речи в записи / запись не распозналась / нет WHISPER_API_KEY / нет KIMI_API_KEY → понятное сообщение, не выдумка")
        # доступ и валидация
        assert (await c.post(URL, json={**BODY, "text": "x"})).status == 403
        assert (await post({**BODY, "text": "x"}, H2)).status == 403
        before = len(calls["ask"])
        for b in ({**BODY, "id": "no-ocr", "text": "x"}, {**BODY, "id": "нет-такого", "text": "x"}, {**BODY, "id": 5, "text": "x"},
                  {**BODY, "fragment": "", "text": "x"}, {**BODY, "fragment": "   ", "text": "x"}, {**BODY, "fragment": 5, "text": "x"},
                  {**BODY}, {**BODY, "text": 5}, {**BODY, "text": "я" * 601}, {**BODY, "audio": "###", "mime": "audio/webm"},
                  {**BODY, "audio": AUDIO["audio"], "mime": "text/plain"}, [], {"text": "x"}):
            r = await post(b); assert r.status == 400, (str(b)[:80], r.status)
        assert len(calls["ask"]) == before, "на отказах к модели не обращаемся"
        ok("без входа/чужой сеанс → 403; нет этапа 1, пустой фрагмент, нет ни голоса, ни текста, мусор → 400 без обращения к модели")
        # длинный контекст режется, а не отказывает
        r = await post({**BODY, "before": "а" * 5000, "after": "б" * 5000, "text": "замени"}); assert r.status == 200
        assert len(calls["ask"][-1]) < 3500
        ok("слишком длинный контекст обрезается, запрос не раздувается")
        # маршрут
        assert {r.method for res in bot.make_app().router.resources() if res.canonical == URL for r in res} == {"POST"}
asyncio.run(main())
print("test_essay_edit: все проверки прошли")
