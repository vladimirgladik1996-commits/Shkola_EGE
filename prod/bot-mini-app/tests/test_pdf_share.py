"""Подписные ссылки на PDF + заголовки для Telegram.WebApp.downloadFile.
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
mem.execute("insert into students(name,price) values('Тест Ученик',1500)")
mem.commit()

def sign(raw):
    """Подпись initData тем же ключом, что и бот (конкретное значение токена не важно)."""
    return hmac.new(hmac.new(b"WebAppData", bot.TOKEN.encode(), hashlib.sha256).digest(), raw.encode(), hashlib.sha256).hexdigest()

def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    d["hash"] = sign("\n".join(f"{k}={v}" for k, v in sorted(d.items())))
    return urlencode(d)
H = {"X-Init": init(42, "Gladik_N")}
WEEK = "2026-10-12"

def sig_for(uid, start, exp):
    return hmac.new(bot.TOKEN.encode(), f"pdf:{uid}:{start}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]

async def main():
    async with TestClient(TestServer(bot.make_app())) as c:
        # 1. без авторизации и без подписной ссылки — закрыто
        assert (await c.get(f"/api/week.pdf?start={WEEK}")).status == 403
        assert (await c.get(f"/api/week.pdf?start={WEEK}&tok=abc.def.ghi")).status == 403
        ok("PDF без авторизации и с мусорным tok → 403")

        # 2. подписная ссылка выдаётся только вошедшему
        assert (await c.get(f"/api/pdf_link?start={WEEK}")).status == 403
        r = await c.get(f"/api/pdf_link?start={WEEK}", headers=H)
        assert r.status == 200
        url = (await r.json())["url"]
        assert url.startswith(f"/api/week.pdf?start={WEEK}&tok=")
        ok("ссылка выдаётся только вошедшему; формат URL корректен")

        # 3. по ссылке PDF отдаётся БЕЗ заголовков авторизации, с обязательными для downloadFile заголовками
        r = await c.get(url)
        assert r.status == 200 and r.content_type == "application/pdf"
        assert r.headers["Content-Disposition"] == 'attachment; filename="raspisanie_2026-10-12.pdf"'
        assert r.headers["Access-Control-Allow-Origin"] == "https://web.telegram.org"
        ok("GET по ссылке без заголовков → 200; Content-Disposition и ACAO на месте")

        # 4. подделка: другая неделя, изменённый uid, битая подпись, истёкший срок
        exp, uid, sig = url.split("tok=")[1].split(".")
        past = int(time.time()) - 10
        bad = [f"/api/week.pdf?start=2026-10-19&tok={exp}.{uid}.{sig}",                 # другая неделя
               f"/api/week.pdf?start={WEEK}&tok={exp}.43.{sig}",                        # uid подменён, подпись прежняя
               f"/api/week.pdf?start={WEEK}&tok={exp}.{uid}.{'0' * 32}",                # битая подпись
               f"/api/week.pdf?start={WEEK}&tok={past}.{uid}.{sig_for(uid, WEEK, past)}"]  # верно подписанный, но просроченный
        for u in bad:
            assert (await c.get(u)).status == 403, u
        ok("чужая неделя, подмена uid, битая подпись, просроченный срок → 403")

        # 5. подписная ссылка не работает на другие ручки
        assert (await c.get(f"/api/data?tok={url.split('tok=')[1]}")).status == 403
        ok("tok действует только для /api/week.pdf")

        # 6. обычный вход (X-Init) продолжает работать
        r = await c.get(f"/api/week.pdf?start={WEEK}", headers=H)
        assert r.status == 200 and r.headers["Content-Disposition"].startswith("attachment;")
        ok("обычный вход через X-Init работает как раньше")

print("PDF SHARE:")
asyncio.run(main())
print("ALL PDF SHARE TESTS PASSED")
