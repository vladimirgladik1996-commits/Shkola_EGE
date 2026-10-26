"""Файлы «Чата с ИИ» переживают рестарт сервиса и открываются просмотрщиком.

Регресс (баг-репорт 013, часть 2): тап по карточке PDF вёл в никуда по двум причинам
вне фронта. Файлы жили ТОЛЬКО в словаре процесса (CHAT_FILES), а Render перезапускает
сервис каждые 1–2 часа: ссылка, подписанная на сутки, вела на 404 «Файл устарел» уже
через полчаса-час. И отдавался файл всегда с Content-Disposition: attachment — браузер
молча скачивал PDF вместо того, чтобы показать его просмотрщиком.

Проверяем оба пункта: файл обязан находиться после «рестарта» (сброса памяти процесса),
а PDF — открываться просмотрщиком, а не уходить в скачивание.
"""
import asyncio, hashlib, hmac, json, os, sqlite3, sys, time
from urllib.parse import urlencode
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
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
H_OTHER = {"X-Init": init(77, "Other_N")}
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


async def main():
    app = bot.make_app()
    auth.setup(app)
    async with TestClient(TestServer(app)) as c:
        # 0. кладём файл и получаем подписанную ссылку — как это делает api_chat
        url = bot.chat_file_put(42, PDF, "Отчёт.pdf", "application/pdf")
        assert url.startswith("/api/chat_file/") and "tok=" in url, url
        ok("chat_file_put() отдаёт подписанную ссылку")

        # 1. ссылка работает «сразу» — файл ещё в памяти процесса
        r = await c.get(url); b = await r.read()
        assert r.status == 200 and b == PDF, (r.status, len(b))
        ok("свежая ссылка → 200, байты на месте")

        # 2. РЕСТАРТ СЕРВИСА: память процесса пуста, диск цел.
        #    Именно это делает Render каждые 1–2 часа — раньше ссылка тут же умирала.
        bot.CHAT_FILES.clear()
        r = await c.get(url); b = await r.read()
        assert r.status == 200 and b == PDF, (
            f"файл НЕ пережил рестарт: status={r.status}, байт={len(b)}. "
            "Ссылка подписана на сутки, а файл лежит только в словаре процесса — "
            "нужно персистентное хранение (см. DATA_DIR/tutor.db, там же живёт и база)")
        ok("файл пережил «рестарт» (память процесса пуста, диск цел) → 200")

        # 3. PDF открывается ПРОСМОТРОМ, а не уходит в скачивание
        r = await c.get(url)
        cd = r.headers.get("Content-Disposition", "")
        assert r.status == 200 and "attachment" not in cd, (
            f"PDF отдаётся как attachment ({cd!r}) — браузер молча скачает файл "
            "вместо того, чтобы показать его просмотрщиком")
        ok(f"без ?dl=1 → просмотр: Content-Disposition={cd!r}")

        # 4. ...но «Скачать» обязан по-прежнему скачивать: ?dl=1
        r = await c.get(url + "&dl=1")
        cd = r.headers.get("Content-Disposition", "")
        assert r.status == 200 and cd.startswith("attachment"), (r.status, cd)
        ok(f"с ?dl=1 → скачивание: Content-Disposition={cd!r}")

        # 5. Word/PDF-документы, которые просмотрщик не показывает, — attachment в обоих режимах
        u = bot.chat_file_put(42, b"PK\x03\x04docx-bytes", "План урока.docx",
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        bot.CHAT_FILES.clear()
        for suffix in ("", "&dl=1"):
            r = await c.get(u + suffix)
            assert r.status == 200 and r.headers.get("Content-Disposition", "").startswith("attachment"), \
                (suffix, r.status, r.headers.get("Content-Disposition"))
        ok("docx → attachment в обоих режимах (просмотрщик его не показывает)")

        # 6. картинка — просмотрщик умеет показывать, значит inline
        u = bot.chat_file_put(42, b"\x89PNG\r\n\x1a\npng", "фото.png", "image/png")
        bot.CHAT_FILES.clear()
        r = await c.get(u)
        assert r.status == 200 and "attachment" not in r.headers.get("Content-Disposition", ""), \
            r.headers.get("Content-Disposition")
        ok("png → просмотр (inline)")

        # 7. чужой файл не отдаём: у ссылки чужой tg_id
        u2 = bot.chat_file_put(77, b"secret", "чужой.pdf", "application/pdf")
        bot.CHAT_FILES.clear()
        r = await c.get(u2, headers=H); b = await r.read()   # авторизован как 42, файл — от 77
        assert r.status == 403, (r.status, b)
        ok("файл чужого tg_id → 403, даже с действующей подписью")

        # 8. подпись обязательна: без ?tok= или с мусором — отказ, не 200
        u3 = bot.chat_file_put(42, PDF, "x.pdf", "application/pdf")
        bot.CHAT_FILES.clear()
        bare = u3.split("?")[0]
        assert (await c.get(bare)).status in (401, 403), "ссылка без подписи не должна открываться"
        assert (await c.get(bare + "?tok=мусор")).status in (401, 403), "мусорная подпись не должна проходить"
        ok("без подписи / с мусорной подписью → отказ")

print("CHAT FILE PERSIST:")
asyncio.run(main())
print("ALL CHAT FILE PERSIST TESTS PASSED")
