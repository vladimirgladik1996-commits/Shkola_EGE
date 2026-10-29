"""«Переслать контакту» для PDF расписания — ровно как «Поделиться» под файлом в «Чате с ИИ»:
/api/week/file регистрирует PDF недели файлом чата, дальше работает тот же /api/chat_file/prepare
(savePreparedInlineMessage → tg.shareMessage). Сеть Telegram подменяется (FakeBot), база — в памяти."""
import asyncio, hashlib, hmac, json, os, re, sqlite3, sys, time
from urllib.parse import urlencode
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import auth, bot
from aiogram.types import InlineQueryResultCachedDocument
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
    def __init__(self): self.prepared, self.uploads = [], []
    async def send_document(self, uid, doc, **kw):        # тихая дозагрузка файла в Telegram (tg_upload_file_id)
        self.uploads.append((uid, doc.filename, bytes(doc.data)))
        class D: file_id = "fid-week"
        class M: document, message_id = D(), 91
        return M()
    async def delete_message(self, uid, mid): return True
    async def save_prepared_inline_message(self, user_id, result, **kw):
        self.prepared.append((user_id, result, kw))
        class P: id = "prepared-week-1"
        return P()

async def main():
    fb = FakeBot(); app = bot.make_app(); app["bot"] = fb
    async with TestClient(TestServer(app)) as c:
        post = lambda j, h=H: c.post("/api/week/file", json=j, headers=h)
        sid = mem.execute("insert into students(name,price) values('Анна Петрова',2000)").lastrowid
        mem.execute("insert into lessons(student_id,day,time,dur) values(?,?,?,60)", (sid, "2026-10-13", "10:00")); mem.commit()

        # 1. без входа и у постороннего — закрыто
        assert (await c.post("/api/week/file", json={"start": "2026-10-12"})).status == 403
        assert (await post({"start": "2026-10-12"}, {"X-Init": init(7, "evil_user")})).status == 403
        ok("без подписи и у постороннего → 403")

        # 2. файл выдан: карточка файла чата с PDF недели (любой день недели → понедельник)
        r = await post({"start": "2026-10-14"}); assert r.status == 200, r.status
        f = (await r.json())["file"]
        assert f["name"] == "raspisanie_2026-10-12.pdf" and f["mime"] == "application/pdf" and f["kind"] == "pdf", f
        m = re.match(r"/api/chat_file/([^/?]+)/raspisanie_2026-10-12\.pdf\?tok=", f["url"]); assert m, f["url"]
        tok = m.group(1)
        ok("выдан файл чата: имя raspisanie_<понедельник>.pdf, mime application/pdf, ссылка /api/chat_file/<tok>/…")

        # 3. по ссылке — настоящий PDF недели (без заголовков авторизации, как зовут downloadFile/APK)
        r = await c.get(f["url"]); data = await r.read()
        assert r.status == 200 and r.content_type == "application/pdf" and data[:5] == b"%PDF-" and len(data) > 1500, r.status
        assert r.headers.get("Access-Control-Allow-Origin") == "https://web.telegram.org"
        ok("по ссылке отдаётся настоящий PDF (с CORS для downloadFile)")

        # 4. пересылка контакту — ТОТ ЖЕ /api/chat_file/prepare, что и у файлов «Чата с ИИ»
        assert (await c.post("/api/chat_file/prepare", json={"tok": tok})).status == 403
        r = await c.post("/api/chat_file/prepare", json={"tok": tok}, headers=H)
        assert r.status == 200 and (await r.json())["id"] == "prepared-week-1", r.status
        uid, res, kw = fb.prepared[-1]
        assert uid == 42 and isinstance(res, InlineQueryResultCachedDocument), type(res)
        assert res.title == "raspisanie_2026-10-12.pdf" and res.document_file_id == "fid-week"
        assert kw.get("allow_user_chats") and kw.get("allow_group_chats")
        assert fb.uploads and fb.uploads[-1][1] == "raspisanie_2026-10-12.pdf" and fb.uploads[-1][2] == data
        ok("prepare → savePreparedInlineMessage: контакт получает САМ PDF (CachedDocument, имя и байты те же)")

        # 5. чужой файл переслать нельзя
        r = await c.post("/api/chat_file/prepare", json={"tok": tok}, headers={"X-Init": init(7, "evil_user")})
        assert r.status == 403
        ok("чужой файл расписания переслать нельзя → 403")

        # 6. мусорная неделя → 400, файл не регистрируется
        n = len(bot.CHAT_FILES)
        for i, b in enumerate([{}, {"start": "20261012"}, {"start": 5}, {"start": "2026-13-45"}, [1], {"start": None}]):
            r = await post(b); assert r.status == 400, (i, r.status)
        assert len(bot.CHAT_FILES) == n
        ok("некорректная неделя → 400, файл не создаётся")

        # 7. запись в журнал действий
        rows = [tuple(r) for r in mem.execute("select tg_id, action from audit_log where action='/api/week/file'")]
        assert rows and all(r[0] == 42 for r in rows)
        ok("выдача файла попадает в журнал действий")

        # 8. без бота пересылать нечем → 503 (страница идёт прежними путями)
        async with TestClient(TestServer(bot.make_app())) as c2:
            assert (await c2.post("/api/week/file", json={"start": "2026-10-12"}, headers=H)).status == 503
        ok("бот не подключён → 503")

        # 9. standalone-вход (uid 0, APK) — Telegram-пересылки нет → 503, APK идёт своим мостом
        tokn = auth.issue_token(auth.STANDALONE_UID)
        r = await c.post("/api/week/file", json={"start": "2026-10-12"}, headers={"X-Session": tokn})
        assert r.status == 503, r.status
        ok("standalone-сессия (uid 0) → 503: пересылать контакту нечем")
asyncio.run(main())
print("OK")
