"""«Поделиться PDF» из мини-аппа: WebView Telegram не умеет системный шаринг файлов, поэтому бот сам шлёт PDF недели в чат
с пользователем (оттуда его пересылают контакту). Плюс CORS для standalone-приложения (заголовок X-Session)."""
import asyncio, hashlib, hmac, json, logging, os, sqlite3, sys, time
from urllib.parse import urlencode
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", AUTH_LOGIN="Admin", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import auth, bot
from aiogram.exceptions import TelegramBadRequest
from aiohttp.test_utils import TestClient, TestServer
def ok(c): print("  ✓", c)

mem = sqlite3.connect(":memory:", check_same_thread=False); bot.db.backup(mem); mem.row_factory = sqlite3.Row
bot.db = mem; auth.init(mem); auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp())); mem.commit()
def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)
H = {"X-Init": init(42, "Gladik_N")}

class FakeBot:
    def __init__(self): self.sent, self.fail = [], None
    async def send_document(self, chat_id, document, caption=None, **kw):
        if self.fail: raise self.fail
        self.sent.append((chat_id, document.filename, bytes(document.data), caption))

async def main():
    fb = FakeBot(); app = bot.make_app(); app["bot"] = fb
    async with TestClient(TestServer(app)) as c:
        post = lambda j, h=H: c.post("/api/week/send", json=j, headers=h)
        sid = mem.execute("insert into students(name,price) values('Анна Петрова',2000)").lastrowid
        mem.execute("insert into lessons(student_id,day,time,dur) values(?,?,?,60)", (sid, "2026-10-13", "10:00")); mem.commit()

        assert (await c.post("/api/week/send", json={"start": "2026-10-12"})).status == 403
        assert (await post({"start": "2026-10-12"}, {"X-Init": init(7, "evil_user")})).status == 403 and not fb.sent
        ok("без подписи и у постороннего → 403, ничего не отправлено")

        r = await post({"start": "2026-10-14"}); assert r.status == 200 and (await r.json())["ok"], r.status
        chat, name, data, cap = fb.sent[-1]
        assert chat == 42 and name == "raspisanie_2026-10-12.pdf" and data[:5] == b"%PDF-" and len(data) > 1500, (chat, name, len(data))
        assert "12.10" in cap and "18.10.2026" in cap
        ok("PDF недели уходит в чат владельца (42): имя raspisanie_<понедельник>.pdf, настоящий PDF, подпись с датами")

        for i, b in enumerate([{}, {"start": "20261012"}, {"start": 5}, {"start": "2026-13-45"}, [1], {"start": None}]):
            n = len(fb.sent); r = await post(b); assert r.status == 400 and len(fb.sent) == n, (i, r.status)
        ok("некорректная неделя → 400, ничего не отправляется")

        fb.fail = TelegramBadRequest(method=None, message="chat not found")
        r = await post({"start": "2026-10-12"}); assert r.status == 502 and (await r.json())["error"] == "send_failed", r.status
        fb.fail = None
        ok("Telegram отказал (чат не найден/бот заблокирован) → 502 send_failed, а не «внутренняя ошибка»")

        async with TestClient(TestServer(bot.make_app())) as c2:                 # приложение без app["bot"]
            assert (await c2.post("/api/week/send", json={"start": "2026-10-12"}, headers=H)).status == 503
        ok("бот не подключён → 503")

        rows = [tuple(r) for r in mem.execute("select tg_id, action, target from audit_log where action='/api/week/send'")]
        assert rows and all(r[0] == 42 for r in rows)
        ok("отправка попадает в журнал действий")

        os.environ["ALLOW_ORIGIN"] = "https://tutor.github.io"
        r = await c.options("/api/week/send", headers={"Origin": "https://tutor.github.io", "Access-Control-Request-Method": "POST",
                                                      "Access-Control-Request-Headers": "x-session,content-type"})
        allowed = {h.strip().lower() for h in r.headers.get("Access-Control-Allow-Headers", "").split(",")}
        assert {"x-init", "x-session", "content-type"} <= allowed, allowed
        r = await c.options("/api/week.pdf", headers={"Origin": "https://tutor.github.io", "Access-Control-Request-Headers": "x-session"})
        assert "x-session" in r.headers.get("Access-Control-Allow-Headers", "").lower()
        ok("CORS: X-Session разрешён — standalone-приложение с другого домена проходит preflight")
        del os.environ["ALLOW_ORIGIN"]
asyncio.run(main())
print("OK")
