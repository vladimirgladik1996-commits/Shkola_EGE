"""Сквозной тест /api/chat (баг-репорт 015): фото с заданием → задание не подменяется и не теряется на следующих
ходах; сочинение ЕГЭ/ОГЭ приходит с абзацами, красной строкой, примечанием и объёмом; Word — только работа.
Сеть Telegram и модель подменены. Запуск: python3 tests/test_chat_task_http.py
"""
import asyncio, base64, hashlib, hmac, io, json, os, sqlite3, sys, time
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL",
                  KIMI_API_KEY="123456:TESTKEY")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import assist, auth, bot, chatfiles, chatprompt, kimi
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image


def ok(m):
    print("  ✓", m)


Q = "Что значит быть сильным духом?"
READ = {"has_task": True, "legible": True, "exam": "oge",
        "options": [{"num": "13.3", "question": Q}], "chosen": None, "recommended": None,
        "wording": "Дайте ответ на вопрос.", "text": {"present": True, "author": "Б. Екимов", "begins": "Шёл дождь", "numbered": True},
        "notes": ""}
ESSAY = ("Б. Екимов в тексте размышляет о силе духа. Сильный духом человек не сдаётся перед трудностями и помогает другим, "
         "считаю я.\nВ предложениях 5–7 мальчик терпит боль, но не жалуется, и это подтверждает мысль о стойкости, "
         "потому что он продолжает путь. Этот пример показывает, что сила духа проявляется в поступках.\n"
         "Ещё одно доказательство нахожу в предложениях 15–17: бабушка спасает чужого ребёнка, не думая о себе. "
         "Этот пример подтверждает, что сильный дух неотделим от доброты.\n"
         "Таким образом, быть сильным духом — значит стойко переносить беды и оставаться человечным.\n"
         "ПРИМЕЧАНИЕ: выбрана тема 13.3")
STATE = {"json": READ, "text": ESSAY, "seen": [], "json_calls": 0}


async def fake_stream(messages, model=None, deep=False, json_mode=False, images=None, thinking=True):
    STATE["seen"].append((messages, images))
    yield "text", STATE["text"]


async def fake_json(messages, **kw):
    STATE["json_calls"] += 1
    return STATE["json"]


kimi.ask_stream, kimi.ask_json = fake_stream, fake_json


async def fake_tg(tg_bot, uid, text):
    STATE["tg"] = text
    return False


async def fake_check(task, fmt, shots):
    STATE["checked_task"] = task
    return True, ""


bot.tg_chat_answer, assist.check_render = fake_tg, fake_check
FILES = {}
real_put = bot.chat_file_put


def put_spy(uid, data, name, mime, tg_fid=None):
    FILES[name] = data
    return real_put(uid, data, name, mime, tg_fid)


bot.chat_file_put = put_spy

mem = sqlite3.connect(":memory:", check_same_thread=False)
bot.db.backup(mem)
mem.row_factory = sqlite3.Row
bot.db = mem
auth.init(mem)
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp()))
mem.commit()


def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest(), chk.encode(),
                         hashlib.sha256).hexdigest()
    return urlencode(d)


H = {"X-Init": init(42, "Gladik_N")}
buf = io.BytesIO()
Image.new("RGB", (400, 300), (255, 255, 255)).save(buf, "PNG")
PHOTO = {"name": "task.png", "mime": "image/png", "data": base64.b64encode(buf.getvalue()).decode()}


async def chat(c, body):
    STATE["seen"].clear()
    r = await c.post("/api/chat", json=body, headers=H)
    assert r.status == 200, r.status
    return [json.loads(l) for l in (await r.text()).splitlines() if l.strip() and '"hb"' not in l]


async def main():
    I = chatprompt.INDENT
    async with TestClient(TestServer(bot.make_app())) as c:
        # 1. фото с заданием + «эссе ОГЭ», текстовый ответ
        ev = await chat(c, {"messages": [{"role": "user", "text": "Напиши эссе для ОГЭ по фото"}], "files": [PHOTO]})
        assert ev[0] == {"t": "status", "v": "read"}
        texts = [e["v"] for e in ev if e["t"] == "text"]
        assert texts[0].startswith("📌 Задание, прочитанное с фото") and Q in texts[0]
        done = ev[-1]
        assert done["t"] == "done"
        rep = done["reply"]
        paras = rep.split("\n\n")
        assert paras[0].startswith("📌") and Q in paras[0] and "ОГЭ, задание 13.3" in paras[0]
        assert all(p.startswith(I) for p in paras[1:5]) and len(paras) == 7, paras
        assert paras[5] == "Примечание: выбрана тема 13.3" and paras[6].startswith("Объём: ") and paras[6].endswith("(норма ОГЭ — от 70).")
        assert "ПРИМЕЧАНИЕ" not in rep and STATE["tg"] == rep
        ok("фото + «эссе ОГЭ»: первым в ответе — «📌 Задание с фото» (видно сразу), затем 4 абзаца с красной строкой, "
           "примечание и объём по правилам ФИПИ; в Telegram уходит то же")
        msgs, imgs = STATE["seen"][0]
        assert Q in msgs[-1]["content"] and imgs and "СК2 (4)" in msgs[0]["content"]

        # 2. следующий ход: фото НЕ отправляется (клиент шлёт только текст истории) — тема и режим сохранены
        hist = [{"role": "user", "text": "Напиши эссе для ОГЭ по фото [вложения: task.png]"},
                {"role": "assistant", "text": rep}, {"role": "user", "text": "сделай короче и проще"}]
        n_json = STATE["json_calls"]
        ev = await chat(c, {"messages": hist})
        msgs, imgs = STATE["seen"][0]
        assert not imgs and STATE["json_calls"] == n_json
        assert "СК2 (4)" in msgs[0]["content"] and "ПОЖЕЛАНИЯ ПОЛЬЗОВАТЕЛЯ" in msgs[0]["content"]
        assert any(Q in m["content"] for m in msgs[1:-1]) and msgs[-1]["content"] == "сделай короче и проще"
        assert ev[-1]["reply"].rstrip().endswith("(норма ОГЭ — от 70).") and not any(e["t"] == "text" and e["v"].startswith("📌") for e in ev)
        ok("следующий ход без фото («сделай короче»): тема из якоря в истории, пакет ОГЭ и правила пожеланий на месте, "
           "фото не нужно, повторного чтения нет")

        # 3. задание на фото не читается → вопрос, модель-писатель не вызывалась
        STATE["json"] = {**READ, "legible": False, "notes": "фото размыто"}
        ev = await chat(c, {"messages": [{"role": "user", "text": "Напиши эссе для ОГЭ по фото"}], "files": [PHOTO]})
        assert not STATE["seen"] and ev[-1]["t"] == "done" and ev[-1]["reply"].startswith("Не удалось уверенно прочитать")
        assert "фото размыто" in ev[-1]["reply"] and not any(e["t"] == "text" for e in ev)
        ok("размытое фото: пользователь получает просьбу переснять или написать словами; сочинение «по догадке» не пишется")
        STATE["json"] = READ

        # 4. слишком короткая работа ЕГЭ → одна доработка по объёму
        STATE["text"] = "Автор считает, что память важна."
        calls = []

        async def grow(messages, **kw):
            calls.append(messages)
            return "Автор считает, что память важна. " + " ".join(["Это"] * 0 + ["слово"] * 170)
        kimi.ask = grow
        ev = await chat(c, {"messages": [{"role": "user", "text": "Напиши сочинение ЕГЭ по тексту, проблема памяти"}]})
        assert len(calls) == 1 and "Объём: 175 слов (норма ЕГЭ — от 150)." in ev[-1]["reply"], ev[-1]["reply"][-120:]
        ok("ЕГЭ вышло в 5 слов (0 баллов) → автоматически доведено до нормы; пользователь видит итоговый объём")
        kimi.ask = None

        # 5. без текста: модель спрашивает — это вопрос без префикса, не «работа»
        STATE["text"] = "ВОПРОС: Пришлите фото страницы с исходным текстом."
        ev = await chat(c, {"messages": [{"role": "user", "text": "Напиши сочинение ЕГЭ"}]})
        assert ev[-1]["reply"] == "Пришлите фото страницы с исходным текстом." and "Объём" not in ev[-1]["reply"]
        ok("нет исходного текста → в чат приходит вопрос без «ВОПРОС:» и без подписи объёма")

        # 6. Word: в документе только работа; якорь, примечание и объём — в сообщении чата
        STATE["text"] = "Сочинение по тексту Б. Екимова\n" + ESSAY.replace("\n", "\n\n")
        STATE["json"] = READ
        ev = await chat(c, {"messages": [{"role": "user", "text": "Напиши эссе для ОГЭ по фото"}], "files": [PHOTO],
                            "format": "docx", "deep": True})
        done = ev[-1]
        assert done["t"] == "done" and done.get("file", {}).get("name", "").endswith(".docx"), done
        assert done["reply"].startswith("Готово: документ сформирован")
        assert "📌 Задание, прочитанное с фото" in done["reply"] and "Примечание: выбрана тема 13.3" in done["reply"]
        assert "Объём: " in done["reply"] and "(норма ОГЭ — от 70)" in done["reply"]
        data = next(v for k, v in FILES.items() if k.endswith(".docx"))
        text = chatfiles.docx_text(data)
        assert "Екимов" in text and "сильным духом" in text and "ПРИМЕЧАНИЕ" not in text and "📌" not in text
        assert Q in STATE["checked_task"]
        import zipfile, re
        xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode()
        assert len(re.findall(r'w:firstLine="709"', xml)) >= 4
        ok("Word: в документе заголовок и абзацы с красной строкой 1,25 см; примечание и якорь — в чате; проверка файла "
           "видит и тему с фото")

        # 7. не сочинение: чат без режима, без шага чтения, без подписи
        STATE["text"] = "Привет!"
        n_json = STATE["json_calls"]
        ev = await chat(c, {"messages": [{"role": "user", "text": "привет, как дела"}]})
        assert ev[-1]["reply"] == "Привет!" and STATE["json_calls"] == n_json
        ok("обычный вопрос: ответ без изменений — ни режима сочинения, ни шага чтения")


asyncio.run(main())
print("test_chat_task_http: OK")
