"""Карточка Word-ответа: скачивание/открытие файла и «Поделиться» через tg.shareMessage.
Сеть Telegram подменяется (FakeBot), база — в пимяти. Регрессия главного бага:
подписные ссылки должны работать для tg_id > 2 млрд (новые аккаунты Telegram)."""
import asyncio, hashlib, hmac, json, os, re, sqlite3, sys, time
from urllib.parse import urlencode, unquote
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import auth, bot
from aiogram.types import InlineQueryResultDocument
from aiohttp.test_utils import TestClient, TestServer
def ok(c): print("  ✓", c)

mem = sqlite3.connect(":memory:", check_same_thread=False); bot.db.backup(mem); mem.row_factory = sqlite3.Row
bot.db = mem; auth.init(mem); auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp())); mem.commit()

def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", bot.TOKEN.encode(), hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)
H = {"X-Init": init(42, "Gladik_N")}
BIG = 4267559270                      # реальный tg_id: больше старой границы 2 000 000 000
DOCX = b"PK\x03\x04-tiny-docx-bytes"
MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

class FakeBot:
    def __init__(self): self.prepared, self.fail = [], None
    async def save_prepared_inline_message(self, user_id, result, **kw):
        if self.fail: raise self.fail
        self.prepared.append((user_id, result, kw))
        class P: id = "prepared-id-1"
        return P()

async def main():
    fb = FakeBot(); app = bot.make_app(); app["bot"] = fb
    async with TestClient(TestServer(app)) as c:
        # --- 1. ссылка отдаёт файл без заголовков (так его зовут tg.openLink / tg.downloadFile) ---
        furl = bot.chat_file_put(42, DOCX, "План урока.docx", MIME)
        r = await c.get(furl)
        assert r.status == 200 and await r.read() == DOCX, r.status
        assert r.headers["Content-Type"] == MIME
        assert "filename*=UTF-8''" in r.headers["Content-Disposition"] and "План урока.docx" in unquote(r.headers["Content-Disposition"])
        assert r.headers.get("Access-Control-Allow-Origin") == "https://web.telegram.org"   # обязательно для tg.downloadFile
        ok("файл отдаётся по ссылке без авторизации: байты, mime, русское имя, CORS для downloadFile")

        # --- 2. регрессия: tg_id > 2 млрд (новые аккаунты Telegram) ---
        fb2 = bot.chat_file_put(BIG, DOCX, "Итог.docx", MIME)
        r = await c.get(fb2)
        assert r.status == 200 and await r.read() == DOCX, r.status
        ok(f"ссылка работает для tg_id {BIG} (старая граница 2 млрд её отбрасывала)")

        # --- 3. мусорная/чужая подпись не проходит ---
        assert (await c.get(furl.rsplit("=", 1)[0] + "=deadbeef")).status == 403
        ok("подделанная подпись → 403")

        # --- 4. «Поделиться»: savePreparedInlineMessage → tg.shareMessage (файл, не ссылка) ---
        assert (await c.post("/api/chat_file/prepare", json={"tok": furl.split("/")[3].split("?")[0]})).status == 403
        assert (await c.post("/api/chat_file/prepare", json={"tok": furl.split("/")[3].split("?")[0]},
                             headers={"X-Init": init(7, "evil_user")})).status == 403 and not fb.prepared
        ok("подготовка пересылки без входа и посторонним → 403, ничего не готовится")

        tok = furl.split("/")[3].split("?")[0]
        r = await c.post("/api/chat_file/prepare", json={"tok": tok}, headers=H)
        assert r.status == 200 and (await r.json())["id"] == "prepared-id-1", r.status
        uid, res, kw = fb.prepared[-1]
        assert uid == 42 and isinstance(res, InlineQueryResultDocument), type(res)
        assert res.title == "План урока.docx" and res.mime_type == "application/zip"   # docx=zip: Telegram пропускает только pdf/zip
        assert res.document_url.startswith("http") and res.document_url.endswith(furl)
        assert kw.get("allow_user_chats") and kw.get("allow_group_chats")
        ok("владеleц получает id подготовленного сообщения: document_url — наш подписной файл, имя и mime верные")

        # --- 5. контракт фронтенда: «Поделиться» передаёт ТОЛЬКО файл — любые ссылки запрещены ---
        html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
        assert "tg.shareMessage" in html and "/api/chat_file/prepare" in html, "нет tg.shareMessage/prepare"
        assert 'api("/api/chat_file/send"' not in html, "старый bot-copy путь остался"
        assert "выберите контакт" not in html, "мусорный тост «файл уже в боте» остался"
        # баг 1: t.me/share/url и navigator.share({url}) отправляли подписную ссылку вместо файла — вырезаны полностью
        assert "t.me/share/url" not in html and "chShLink" not in html, \
            "ссылочный шаринг остался — получатель снова получит ссылку вместо файла"
        assert "navigator.share({title" not in html, "шаринг URL вместо файла остался"
        assert "navigator.share({files" in html, "нет шаринга самим файлом (Web Share API)"
        assert "A.openFile" in html, "нет Android.openFile — «Скачать»/тап не открывают файл"
        # запрещённые сообщения о том, что файл якобы «в чате с ботом» — не возвращаются никогда
        for bad in ("Файл уже в чате с ботом", "откройте его в чате с ботом", "перешлите его из чата с ботом",
                    "PDF в чате", "Открыть чат", "файл в диалог с ботом"):
            assert bad not in html, f"запрещённое сообщение вернулось: {bad}"
        ok("фронт: шарится только файл (Android.shareFile / tg.shareMessage / navigator.share({files})); ссылок и запрещённых сообщений нет")

        # --- 6. баги 2+3: «Скачать» и тап по файлу/стикеру сходятся в одну функцию «скачать и открыть» ---
        assert re.search(r"function chDl\(i,j\)\{chOpen\(i,j\)\}", html), "кнопка «Скачать» не зовёт chOpen"
        assert re.search(r"const chFileTap=\(i,j,e\)=>\{[^}]*chOpen\(i,j\)", html), "тап по файлу/стикеру не зовёт chOpen"
        assert "return chFileTap(" in html, "стикер и карточка документа должны быть кликабельными"
        assert "tg.openLink" in html, "Telegram-путь «скачать+открыть» должен открывать файл системным способом"
        assert "/api/chat_file/rebuild" in html and "chFresh" in html, \
            "нет оживления старых карточек — файлы из истории чата остаются мёртвыми"
        ok("фронт: «Скачать» и тап по файлу/стикеру — одна функция chOpen (скачать + открыть); старые карточки оживают")

        # --- 7. старые карточки оживают: файл пересобирается из текста ответа, ссылка свежая ---
        r = await c.post("/api/chat_file/rebuild", json={"text": "# План урока\nРазминка 5 минут", "name": "План урока.docx"}, headers=H)
        d = await r.json()
        assert r.status == 200 and d["url"].startswith("/api/chat_file/"), (r.status, d)
        rr = await c.get(d["url"])
        b = await rr.read()
        assert rr.status == 200 and b[:2] == b"PK" and "План урока.docx" in unquote(rr.headers["Content-Disposition"])
        ok("старая карточка оживает: файл собирается заново из текста ответа и отдаётся по свежей ссылке")
        assert (await c.post("/api/chat_file/rebuild", json={"text": "", "name": "x.docx"}, headers=H)).status == 400
        assert (await c.post("/api/chat_file/rebuild", json={"text": "# Т\nтекст"})).status == 403
        ok("пересборка: пустой текст и запрос без входа отклоняются")



        from aiogram.exceptions import TelegramBadRequest
        fb.fail = TelegramBadRequest(method=None, message="bad")
        r = await c.post("/api/chat_file/prepare", json={"tok": tok}, headers=H)
        assert r.status == 502 and (await r.json())["error"] == "prepare_failed", r.status
        ok("Telegram отказал → 502 prepare_failed (фронт переходит к скачиванию файла — без ссылок)")
print("test_chat_share: OK")
asyncio.run(main())
