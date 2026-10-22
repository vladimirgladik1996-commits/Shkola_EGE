"""Форматы ответа в Telegram-боте: «Долгий ответ на сложный вопрос» → выбор формата
(текст / Word / PDF / картинка — тот же набор, что в разделе «Чат с ИИ»), файл собирается тем же
конвейером (chatfiles → проверка Кими) и уходит в чат документом/картинкой. «Быстрый ответ на
простой вопрос» всегда отвечает текстом — формат не предлагается. Модель и Telegram подменены.

Запуск: python3 tests/test_format_answer.py
"""
import asyncio, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID", "KIMI_API_KEY", "KIMI_BASE_URL", "KIMI_MODEL",
          "WHISPER_API_KEY", "WHISPER_BASE_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import assist, bot, kimi


def ok(c):
    print("  ✓", c)


class FakeMsg:
    message_id = 1


class FakeBot:
    """Запоминает сообщения (ThinkRelay шлёт и удаляет мысли через него)."""
    def __init__(self):
        self.sent = []
    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text); return FakeMsg()
    async def edit_message_text(self, text, chat_id=None, message_id=None):
        pass
    async def delete_message(self, chat_id, message_id):
        pass


class FakeChat:
    """c.message из callback_query: ответы, документы и картинки."""
    def __init__(self):
        self.bot = FakeBot()
        self.chat = type("C", (), {"id": 777})()
        self.answers, self.docs, self.photos = [], [], []
    async def answer(self, text, **kw):
        self.answers.append((text, kw.get("reply_markup"))); return FakeMsg()
    async def answer_document(self, f, caption=None):
        self.docs.append((f, caption))
    async def answer_photo(self, f, caption=None):
        self.photos.append((f, caption))


class FakeCB:
    def __init__(self, data, msg, uid=42):
        self.data, self.message = data, msg
        self.from_user = type("U", (), {"id": uid})()
    async def answer(self, *a, **kw):
        pass


REPLY = []   # что «модель» отвечает по очереди


async def fake_stream(messages, model=None, deep=False, json_mode=False, images=None, thinking=True):
    yield "text", REPLY.pop(0)


async def fake_ask(messages, **kw):
    return REPLY.pop(0)


async def fake_ask_json(messages, **kw):
    return {"ok": True, "problems": ""}


def kb_data(markup):
    return [(b.text, b.callback_data) for row in (markup.inline_keyboard if markup else []) for b in row]


async def main():
    real_stream, real_json, real_ask = kimi.ask_stream, kimi.ask_json, kimi.ask
    kimi.ask_stream, kimi.ask_json, kimi.ask = fake_stream, fake_ask_json, fake_ask
    try:
        # --- 0) контракт полноты: файл — полный результат по задаче, с ответами для самопроверки ---
        for tail in (assist.CHAT_DOC_TAIL, assist.CHAT_PDF_TAIL):
            assert "Ответы для самопроверки" in tail and "полный результат" in tail, tail
        ok("промпты файлов требуют полный результат по задаче (для тестов — «Ответы для самопроверки»)")

        # --- 1) «Долгий ответ» → кнопки формата (набор как в разделе «Чат с ИИ») ---
        m = FakeChat()
        assist._keep(42, phase="mode", text="сделай документ")
        await bot.ai_cb(FakeCB("ai:deep", m))
        assert kb_data(m.answers[-1][1]) == [("📄 Текст", "ai:fmt:text"), ("📘 Word-документ", "ai:fmt:docx"),
                                             ("📕 PDF-документ", "ai:fmt:pdf"), ("🖼 Картинка / инфографика", "ai:fmt:image")]
        assert not m.docs and not m.photos
        ok("«Долгий ответ» → выбор формата: текст, Word, PDF, картинка")

        # --- 2) Word: ответ модели уходит .docx ---
        REPLY.append("# Тест\n- м_рковь")
        await bot.ai_cb(FakeCB("ai:fmt:docx", m))
        f, cap = m.docs[-1]
        assert f.data[:2] == b"PK" and f.filename.endswith(".docx") and cap and not m.photos
        ok("Word: модель оформила документ → в чат пришёл .docx (PK-архив)")

        # --- 3) PDF: HTML модели конвертируется в .pdf ---
        m2 = FakeChat()
        assist._keep(42, phase="format", text="сделай PDF", deep=True)
        REPLY.append("<html><head><title>Отчёт</title></head><body><h1>Отчёт</h1><p>Текст</p></body></html>")
        await bot.ai_cb(FakeCB("ai:fmt:pdf", m2))
        f, cap = m2.docs[-1]
        assert f.data[:5] == b"%PDF-" and f.filename.endswith(".pdf") and cap == "Отчёт"
        ok("PDF: HTML модели → .pdf, заголовок в подписи")

        # --- 4) картинка: HTML-инфографика → .png ---
        m3 = FakeChat()
        assist._keep(42, phase="format", text="инфографика", deep=True)
        REPLY.append("<html><head><title>Схема</title></head><body><p>Карточка</p></body></html>")
        await bot.ai_cb(FakeCB("ai:fmt:image", m3))
        f, cap = m3.photos[-1]
        assert f.data[:8] == b"\x89PNG\r\n\x1a\n" and f.filename.endswith(".png") and cap == "Схема"
        ok("Картинка: HTML-инфографика → PNG (конвейер HTML → PDF → PNG)")

        # --- 5) текст: обычной репликой, без файла ---
        m4 = FakeChat()
        assist._keep(42, phase="format", text="вопрос", deep=True)
        REPLY.append("готовый текстовый ответ")
        await bot.ai_cb(FakeCB("ai:fmt:text", m4))
        assert "готовый текстовый ответ" in m4.answers[-1][0] and not m4.docs and not m4.photos
        ok("Текст: ответ репликой, файл не собирается")

        # --- 6) «Быстрый ответ» → всегда текстом, формат не предлагается ---
        m5 = FakeChat()
        assist._keep(43, phase="mode", text="простой вопрос")
        REPLY.append("быстрый ответ")
        await bot.ai_cb(FakeCB("ai:fast", m5, uid=43))
        assert m5.answers[-1][0] == "быстрый ответ" and not m5.docs and not m5.photos
        assert all(a[1] is None for a in m5.answers), m5.answers   # без меню — сразу ответ
        ok("«Быстрый ответ» → сразу текстом, меню формата нет")

        # --- 7) модель ответила не тем форматом → ответ уходит текстом, без падения ---
        m6 = FakeChat()
        assist._keep(44, phase="format", text="инфографика", deep=True)
        REPLY.append("совсем не HTML")
        await bot.ai_cb(FakeCB("ai:fmt:image", m6, uid=44))
        assert not m6.photos and "Не получилось собрать файл" in m6.answers[-1][0] and "совсем не HTML" in m6.answers[-1][0]
        ok("не-HTML вместо картинки → ответ текстом, бот не падает")
    finally:
        kimi.ask_stream, kimi.ask_json, kimi.ask = real_stream, real_json, real_ask

asyncio.run(main())
print("test_format_answer: OK")
