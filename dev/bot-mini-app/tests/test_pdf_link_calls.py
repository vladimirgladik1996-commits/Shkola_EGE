"""Вызовы /api/pdf_link так, как их делает страница «Поделиться PDF».
Регресс: страница зовёт ручку GET-ом с query, а закэшированная в WebView APK старая
страница — POST-ом с телом. Оба варианта должны отдавать подписанную ссылку (не 405).
Сеть Telegram не используется; база — в памяти."""
import asyncio, hashlib, hmac, json, os, sqlite3, sys, time
from urllib.parse import urlencode
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", AUTH_LOGIN="Admin", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import auth, bot
from aiohttp.test_utils import TestClient, TestServer
def ok(c): print("  ✓", c)

mem = sqlite3.connect(":memory:", check_same_thread=False)
mem.row_factory = sqlite3.Row
bot.db = mem
bot.init_db(mem)
auth.init(mem)
auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp()))
mem.commit()

def sign(raw):
    return hmac.new(hmac.new(b"WebAppData", bot.TOKEN.encode(), hashlib.sha256).digest(), raw.encode(), hashlib.sha256).hexdigest()

def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    d["hash"] = sign("\n".join(f"{k}={v}" for k, v in sorted(d.items())))
    return urlencode(d)
H = {"X-Init": init(42, "Gladik_N")}
WEEK = "2026-10-12"

async def main():
    app = bot.make_app()
    auth.setup(app)                      # как в bot.run(): /api/login для standalone-входа (APK)
    async with TestClient(TestServer(app)) as c:
        # 1. как зовёт страница после фикса: GET + query
        r = await c.get(f"/api/pdf_link?start={WEEK}", headers=H)
        j = await r.json()
        assert r.status == 200 and j["url"].startswith(f"/api/week.pdf?start={WEEK}&tok="), (r.status, j)
        ok("GET + query (текущая страница) → 200, ссылка подписана")

        # 2. как зовёт старая закэшированная страница: POST + тело
        r = await c.post("/api/pdf_link", json={"start": WEEK}, headers=H)
        j = await r.json()
        assert r.status == 200 and j["url"].startswith(f"/api/week.pdf?start={WEEK}&tok="), (r.status, j)
        ok("POST + тело (закэшированная страница) → 200, ссылка подписана")

        # 3. обе ссылки отдают PDF без заголовков авторизации — так скачивает DownloadManager в APK
        for shape in ("get", "post"):
            r = await (c.get if shape == "get" else c.post)(f"/api/pdf_link?start={WEEK}" if shape == "get" else "/api/pdf_link",
                                                           **({"headers": H} if shape == "get" else {"json": {"start": WEEK}, "headers": H}))
            u = (await r.json())["url"]
            r2 = await c.get(u)
            assert r2.status == 200 and r2.content_type == "application/pdf", (shape, r2.status)
        ok("скачивание по ссылке без заголовков → 200 application/pdf")

        # 4. мусор вместо недели → 400, а не 500
        assert (await c.get("/api/pdf_link?start=мусор", headers=H)).status == 400
        assert (await c.post("/api/pdf_link", json={"start": "мусор"}, headers=H)).status == 400
        assert (await c.post("/api/pdf_link", data="не json", headers=H)).status == 400
        ok("некорректная неделя / не-JSON тело → 400")

print("PDF LINK CALLS:")
asyncio.run(main())
print("ALL PDF LINK CALLS TESTS PASSED")
