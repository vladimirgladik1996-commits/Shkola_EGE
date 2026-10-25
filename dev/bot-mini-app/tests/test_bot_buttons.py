"""Кнопки Telegram-бота скрыты переключателем BOT_BUTTONS (bug-reports/008-stream-blank-screen.md).

Требование владельца: кнопки неактуальны — спрятать все, но код клавиатур оставить.
Сделано одним перехватом session.make_request, поэтому ни одна клавиатура не просачивается:
reply-клавиатура СНИМАЕТСЯ с экрана (ReplyKeyboardRemove, иначе уже показанная осталась бы висеть),
inline — просто не отправляются. Код всех menu_kb() / *_kb() не тронут.

Запуск: python3 tests/test_bot_buttons.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)

import bot as botmod
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove)


def ok(c):
    print("  ✓", c)


class M:
    """Любой метод aiogram с полем reply_markup (сам объект метода тут не важен)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


# --- по умолчанию кнопки выключены ---
assert botmod.BOT_BUTTONS is False, "кнопки должны быть скрыты по умолчанию (BOT_BUTTONS=0)"
ok("кнопки по умолчанию выключены (BOT_BUTTONS=0)")

# --- reply-клавиатура снимается с экрана ---
kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Рaspisanie")]], resize_keyboard=True)
m = M(reply_markup=kb)
botmod._hide_buttons(m)
assert isinstance(m.reply_markup, ReplyKeyboardRemove), \
    "reply-клавиатура не снята с экрана: уже показанная продолжила бы висеть"
ok("reply-клавиатура снимается с экрана через ReplyKeyboardRemove")

# --- inline-кнопки просто не отправляются ---
ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="x")]])
m = M(reply_markup=ikb)
botmod._hide_buttons(m)
assert m.reply_markup is None, "inline-кнопки не отключены"
ok("inline-кнопки не отправляются (reply_markup=None)")

# --- сообщения без клавиатуры не трогаются ---
m = M(text="prosto soobshchenie")
botmod._hide_buttons(m)
assert not hasattr(m, "reply_markup"), "сообщение без клавиатуры получило лишнее поле"
ok("сообщения без клавиатуры проходят без изменений")

# --- переключатель возвращает кнопки обратно: код клавиатур остался рабочим ---
old = botmod.BOT_BUTTONS
try:
    botmod.BOT_BUTTONS = True
    m = M(reply_markup=ikb)
    botmod._hide_buttons(m)
    assert m.reply_markup is ikb, "BOT_BUTTONS=1 должен возвращать кнопки"
finally:
    botmod.BOT_BUTTONS = old
ok("BOT_BUTTONS=1 возвращает кнопки — код клавиатур остался рабочим")

# --- перехват стоит на ВСЕХ исходящих вызовах API, а не в отдельных местах ---
src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
assert callable(getattr(botmod, "mute_buttons", None)), "нет mute_buttons"
assert "bot.session.make_request = wrapped" in src, "mute_buttons не перехватывает session.make_request"
assert "mute_buttons(bot)" in src, "mute_buttons не подключён к bot в main()"
assert "ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)" in src, "ReplyKeyboardMarkup не импортирован"
ok("перехват стоит в session.make_request — ни один вызов API кнопки не пропустит")

# --- код клавиатур остался на месте (как просили: не удалять, а скрыть) ---
assert "def menu_kb():" in src, "menu_kb() удалён — код клавиатур должен остаться"
assert "def pager(" in src and "InlineKeyboardButton(" in src, "клавиатуры удалены из кода"
ok("код клавиатур не удалён (menu_kb, pager, InlineKeyboardButton на месте)")

print("\nALL BOT BUTTONS TESTS PASSED")
