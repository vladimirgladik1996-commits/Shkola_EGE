"""Мышление Кими в Telegram-чате: беззвучные сообщения «от Вишенки» и их удаление после ответа.

Идея: при долгом разборе пользователь видит, что модель работает, но его не забалтывают —
ход мыслей приходит БЕЗЗВУЧНЫМИ сообщениями, а когда готов ответ, все они УДАЛЯЮТСЯ,
и в переписке остаётся только ответ.

Запуск: python3 tests/test_think_relay.py
"""
import asyncio, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID", "KIMI_API_KEY", "KIMI_BASE_URL",
          "KIMI_MODEL", "KIMI_MODEL_FAST", "KIMI_MODEL_DEEP", "KIMI_EFFORT", "KIMI_MAX_TOKENS",
          "KIMI_MAX_TOKENS_DEEP", "KIMI_MAX_TOKENS_CEILING", "KIMI_THINK_EXTRA",
          "WHISPER_API_KEY", "WHISPER_BASE_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import assist, bot, kimi


def ok(m):
    print("  ✓", m)


class FakeMsg:
    def __init__(self, mid):
        self.message_id = mid


class FakeBot:
    """Запоминает, что было отправлено / отредактировано / удалено."""

    def __init__(self):
        self.sent, self.edited, self.deleted = [], [], []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, "kw": kw, "id": len(self.sent) + 1})
        return FakeMsg(self.sent[-1]["id"])

    async def edit_message_text(self, text, chat_id=None, message_id=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


async def main():
    # --- 1) мышление приходит БЕЗЗВУЧНО и удаляется после готового ответа ---
    b = FakeBot()
    r = bot.ThinkRelay(b, 777)
    r.MIN_GAP = 0.0                                    # в тесте без пауз
    await r.push("Сначала разберу условие. ")
    await r.push("Потом проверю ответ.\n\n")
    await r.flush()
    assert b.sent, "мышление не дошло до чата"
    assert all(s["kw"].get("disable_notification") is True for s in b.sent), b.sent
    assert all(s["chat_id"] == 777 for s in b.sent)
    ok("мышление приходит БЕЗЗВУЧНЫМИ сообщениями (disable_notification=True)")
    assert b.sent[0]["text"].startswith("💭 Вишенка думает") and "разберу условие" in b.sent[0]["text"]
    ok("сообщения подписаны от имени Вишенки: «💭 Вишенка думает…»")
    await r.cleanup()
    assert sorted(mid for _, mid in b.deleted) == sorted(s["id"] for s in b.sent) and b.deleted
    ok("после готового ответа все сообщения мышления УДАЛЯЮТСЯ (%d шт.) — остаётся только ответ" % len(b.deleted))
    assert r.ids == [] and r.buf == ""
    ok("ретранслятор очищен — повторное использование безопасно")

    # --- 2) чат не засоряется: сообщений не больше MAX_MSGS, дальше правится последнее ---
    b = FakeBot()
    r = bot.ThinkRelay(b, 1)
    r.MIN_GAP = 0.0
    for i in range(bot.ThinkRelay.MAX_MSGS + 5):
        await r.push("кусочек размышления %d\n\n" % i)
    await r.flush()
    assert len(b.sent) == bot.ThinkRelay.MAX_MSGS, len(b.sent)
    assert b.edited and b.edited[-1]["message_id"] == bot.ThinkRelay.MAX_MSGS
    ok("сообщений не больше %d: лишние мысли дописываются в последнее, чат не растёт" % bot.ThinkRelay.MAX_MSGS)
    await r.cleanup()
    assert len(b.deleted) == bot.ThinkRelay.MAX_MSGS
    ok("удаляются все — и отправленные, и дописанные")

    # --- 3) мелкие кусочки не плодят сообщения ---
    b = FakeBot()
    r = bot.ThinkRelay(b, 2)
    r.MIN_GAP = 60.0                                   # больше, чем идёт тест
    r.last = time.monotonic()                          # сообщение только что ушло — пауза ещё идёт
    await r.push("мысль 1")
    await r.push("мысль 2")
    assert b.sent == [], b.sent
    await r.flush()
    assert len(b.sent) == 1 and "мысль 1" in b.sent[0]["text"] and "мысль 2" in b.sent[0]["text"]
    ok("мелкие кусочки копятся и уходят одним сообщением, а не спамом")

    # --- 4) answer_chat со стримом: мысль доходит колбэком, ответ собирается ---
    seen = []

    async def fake_stream(messages, model=None, deep=False, json_mode=False, images=None, thinking=True):
        assert deep is True and thinking is True, (deep, thinking)
        yield "think", "думаю "
        yield "think", "ещё"
        yield "text", "Готовый "
        yield "text", "ответ"

    def on_think(d):
        seen.append(d)

    real_stream = kimi.ask_stream
    kimi.ask_stream = fake_stream
    try:
        got = await assist.answer_chat("реши задачу", deep=True, think=on_think)
    finally:
        kimi.ask_stream = real_stream
    assert got == "Готовый ответ", got
    assert seen == ["думаю ", "ещё"], seen
    ok("answer_chat(think=…): ход мыслей уходит колбэку, ответ собирается целиком")

    # --- 5) сбой доставки мыслей не роняет ответ ---
    async def bad_think(d):
        raise RuntimeError("телеграм недоступен")

    kimi.ask_stream = fake_stream
    try:
        got = await assist.answer_chat("реши задачу", deep=True, think=bad_think)
    finally:
        kimi.ask_stream = real_stream
    assert got == "Готовый ответ", got
    ok("сбой доставки мыслей не мешает получить ответ")

    # --- 6) без think — прежний нестримовый путь ---
    async def fake_ask(messages, **kw):
        return "обычный ответ"

    real_ask = kimi.ask
    kimi.ask = fake_ask
    try:
        assert await assist.answer_chat("вопрос", deep=False) == "обычный ответ"
    finally:
        kimi.ask = real_ask
    ok("без колбэка — как раньше, ничего не сломано")

    # --- 7) handle_reply прокидывает колбэк в answer_chat, режим сохраняется ---
    calls = []

    async def fake_ac(text, deep=False, images=None, docs=None, think=None, fmt="text"):
        calls.append({"deep": deep, "think": think, "fmt": fmt})
        return "ок"

    real_ac = assist.answer_chat
    assist.answer_chat = fake_ac
    try:
        cb = lambda d: None
        assist._keep(42, phase="mode", text="вопрос")
        r0 = await assist.handle_reply(42, "ai:deep", think=cb)
        assert calls == [] and [b[1] for b in r0["buttons"]] == ["ai:fmt:text", "ai:fmt:docx", "ai:fmt:pdf", "ai:fmt:image"]
        r1 = await assist.handle_reply(42, "ai:fmt:text", think=cb)
        assert calls[-1]["deep"] is True and calls[-1]["think"] is cb and calls[-1]["fmt"] == "text", calls
        assist._keep(43, phase="mode", text="вопрос")
        r2 = await assist.handle_reply(43, "ai:fast", think=None)
        assert calls[-1]["deep"] is False and calls[-1]["think"] is None, calls
    finally:
        assist.answer_chat = real_ac
        assist.PENDING.pop(42, None)
        assist.PENDING.pop(43, None)
    assert r1["reply"] == "ок" and r2["reply"] == "ок" and "fmt" not in r1
    ok("кнопки формата — после «Долгого»; think и глубокий режим доходят до answer_chat")


asyncio.run(main())
print("test_think_relay: OK")
