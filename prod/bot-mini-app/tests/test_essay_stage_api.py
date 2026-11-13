"""/api/essay_check/run в два этапа: stage="ocr" (только распознавание → текст на проверку ученику) и stage="analyze"
(ошибки и критерии ФИПИ по ПОПРАВЛЕННОМУ тексту). Без stage — как раньше. API — живым тестом через TestClient.
Запуск: python3 tests/test_essay_stage_api.py"""
import asyncio, base64, copy, hashlib, hmac, io, json, os, sqlite3, sys, time
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def ok(c): print("  ✓", c)
os.environ.update(BOT_TOKEN="***", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import auth, bot, essaycheck, kimi, yavision
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

essaycheck.MIN_WORDS = {"ege": 0, "oge": 0}
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
b = io.BytesIO(); Image.new("RGB", (8, 8), "#fff").save(b, "JPEG"); JPG = base64.b64encode(b.getvalue()).decode()

T0 = "Автор считает что празник важен."
def pg(text, specs):
    lw, cur = [], 0
    for t, x1, x2 in specs:
        p = text.find(t, cur); cur = p + len(t)
        lw.append({"t": t, "x1": x1, "y1": 10, "x2": x2, "y2": 20, "li": 0, "s": p, "n": len(t)})
    line = {"t": " ".join(w["t"] for w in lw), "x1": lw[0]["x1"], "y1": 10, "x2": lw[-1]["x2"], "y2": 20}
    return {"text": text, "lw": lw, "words": [line] + [{k: w[k] for k in ("t", "x1", "y1", "x2", "y2")} for w in lw]}
PAGE = pg(T0, [("Автор", 5, 15), ("считает", 17, 30), ("что", 32, 38), ("празник", 40, 58), ("важен.", 60, 72)])

state = {"recognize": 0, "msgs": [], "reports": [], "empty": False}
async def fake_recognize(imgs, model="handwritten", **kw):
    state["recognize"] += 1
    return [{"text": "", "words": [], "lw": []}] if state["empty"] else [copy.deepcopy(PAGE) for _ in imgs]
async def fake_stream(msgs, **kw):
    state["msgs"].append(msgs[-1]["content"])
    yield "think", "думаю"
    yield "text", json.dumps({"errors": [{"type": "Пунктуация", "found": "считает что", "correct": "считает, что", "why": "запятая", "src": ""}],
                              "criteria": [{"name": "К1", "got": 1, "max": 1, "why": "ок", "tip": ""}], "recommendations": []}, ensure_ascii=False)
async def fake_criteria(kind, fallback=""): return fallback
yavision.recognize_many, kimi.ask_stream, essaycheck.fipicache.criteria = fake_recognize, fake_stream, fake_criteria
yavision.API_KEY = kimi.API_KEY = "k"
os.environ["EC_WORD_VERIFY"] = "0"                                          # перепроверка слов (ocrverify) не нужна этому тесту
bot.ec_report_start = lambda tg, uid, r, kind: state["reports"].append((uid, r["text"]))

def session(uid=42):
    cid = "sess-%d-%d" % (uid, len(bot.EC_CHECKS))
    bot.EC_CHECKS[cid] = [uid, {"w": [JPG], "t": []}, time.time()]
    return cid

async def run(c, body, h=H):
    r = await c.post("/api/essay_check/run", json=body, headers=h)
    return r.status, [json.loads(l) for l in (await r.text()).splitlines() if l.strip()]

def last_saved(uid=42):
    rows = mem.execute("select result from essay_last where tg_id=?", (uid,)).fetchall()
    return json.loads(rows[0]["result"]) if rows else None

async def main():
    async with TestClient(TestServer(bot.make_app())) as c:
        # ---------- этап 1 ----------
        cid = session()
        st, evs = await run(c, {"id": cid, "kind": "ege", "stage": "ocr"})
        ts = [e["t"] for e in evs if e["t"] != "hb"]
        assert st == 200 and ts[-1] == "ocr" and set(ts[:-1]) == {"status"} and "done" not in ts and "fail" not in ts, ts
        assert evs[-1]["v"] == {"text": T0}, evs[-1]
        assert bot.EC_CHECKS[cid][1]["ocr"]["essay"] == T0 and bot.EC_CHECKS[cid][1]["ocr"]["pages"][0]["lw"]
        assert state["recognize"] == 1 and state["msgs"] == [] and last_saved() is None and state["reports"] == []
        assert cid + ":ocr" not in bot.EC_JOBS and cid not in bot.EC_JOBS, "журнал этапа 1 убран после отдачи итога"
        ok('stage="ocr": только распознавание — статусы и {"t":"ocr","v":{"text"}}; Кими не вызывается, «последняя проверка» и отчёт не создаются')

        # ---------- этап 2 с правкой ----------
        EDIT = "Автор считает что праздник важен."
        st, evs = await run(c, {"id": cid, "kind": "ege", "stage": "analyze", "text": EDIT})
        ts = [e["t"] for e in evs if e["t"] != "hb"]
        assert st == 200 and ts[0] == "status" and ts[-1] == "done" and "ocr" not in ts, ts
        assert state["recognize"] == 1, "OCR на этапе 2 не повторяется"
        res = evs[-1]["result"]
        assert res["text"] == EDIT and "празник" not in state["msgs"][-1] and "праздник" in state["msgs"][-1]
        e1 = res["errors"][0]
        assert e1["found"] == "считает что" and e1.get("x") is not None and e1.get("page") == 0, e1
        assert last_saved()["text"] == EDIT and state["reports"] == [(42, EDIT)]
        ok('stage="analyze": Кими видит поправленный текст, OCR не повторяется, ошибка привязана к фото, сохраняется «последняя проверка» и стартует отчёт')

        # ---------- этап 2: переподключение и сбои ----------
        st, evs = await run(c, {"id": cid, "kind": "ege", "stage": "analyze", "text": "  "})
        assert evs == [{"t": "fail", "why": "empty"}]
        st, evs = await run(c, {"id": cid, "kind": "ege", "stage": "analyze"})
        assert evs == [{"t": "fail", "why": "empty"}]
        cid2 = session()
        st, evs = await run(c, {"id": cid2, "kind": "ege", "stage": "analyze", "text": EDIT})
        assert evs == [{"t": "fail", "why": "gone"}], evs
        ok("analyze: пустой текст → fail empty; нет данных этапа 1 (перезапуск сервиса) → fail gone — клиент начнёт заново")
        st, _ = await run(c, {"id": cid, "kind": "ege", "stage": "analyze", "text": EDIT}, H2)
        assert st == 403
        st, _ = await run(c, {"id": cid, "kind": "ege", "stage": "ocr"}, H2)
        assert st == 403
        ok("чужой сеанс обоих этапов → 403")

        # ---------- этап 1: нет текста ----------
        state["empty"] = True
        cid3 = session()
        st, evs = await run(c, {"id": cid3, "kind": "ege", "stage": "ocr"})
        assert evs[-1]["t"] == "done" and evs[-1]["result"]["why"] == "empty" and "ocr" not in bot.EC_CHECKS[cid3][1]
        assert last_saved()["text"] == EDIT, "неудачное распознавание не затирает «последнюю проверку»"
        state["empty"] = False
        ok('stage="ocr" без текста: done с причиной (как раньше), данных для этапа 2 нет, «последняя проверка» не тронута')

        # ---------- без stage: прежний сквозной путь ----------
        state.update(recognize=0, msgs=[], reports=[])
        cid4 = session()
        st, evs = await run(c, {"id": cid4, "kind": "ege"})
        ts = [e["t"] for e in evs if e["t"] != "hb"]
        assert ts[-1] == "done" and ts.count("status") >= 3 and "ocr" not in ts, ts
        assert evs[-1]["result"]["text"] == T0 and state["recognize"] == 1 and "ocr" not in bot.EC_CHECKS[cid4][1]
        assert last_saved()["text"] == T0 and len(state["reports"]) == 1
        ok("без stage: распознавание и проверка одним потоком, как раньше")

        # ---------- повторное «Текст проверен» после правки: правка поверх правки ----------
        cid5 = session()
        await run(c, {"id": cid5, "kind": "ege", "stage": "ocr"})
        st, evs = await run(c, {"id": cid5, "kind": "ege", "stage": "analyze", "text": "Автор считает, что праздник очень важен."})
        res = evs[-1]["result"]
        assert res["text"] == "Автор считает, что праздник очень важен."
        ok("analyze: несколько правок разом (пунктуация, слово, вставка) принимаются")
asyncio.run(main())
print("test_essay_stage_api: все проверки прошли")
