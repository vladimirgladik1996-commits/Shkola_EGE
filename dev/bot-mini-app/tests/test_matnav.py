"""Голосовая навигация по «Материалам»: «Открой страницу эссе 13.2 для варианта 7 ОГЭ» → какой файл открыть.
Модуль matnav.py без зависимостей — тест не требует ни бота, ни сети. Запуск: python3 tests/test_matnav.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import matnav as m

def ok(c): print("  ✓", c)
OGE, OGE80, EGE = m.OGE, m.OGE80, m.EGE

def go(text, ctx=None, here=True, partial=None):
    f = m.merge(partial, m.parse(text))
    if not m.is_nav(text, m.parse(text), has_prev=bool(partial)):
        return None
    return m.resolve(f, ctx or {}, here=here)

# --- главная фраза: страница находится и открывается
for phrase in ("Открой страницу эссе 13.2 для варианта 7 ОГЭ", "Открой страницу эссе 13.2 для варианта 7 ОГО",
               "открой эссе 13.2 седьмого варианта огэ", "покажи эссе 13 точка 2 вариант семь ОГЭ"):
    assert go(phrase) == ("ok", {"cat": OGE, "n": 7, "part": "p-132"}), phrase
ok("«Открой страницу эссе 13.2 для варианта 7 ОГЭ» (и «ОГО», слова вместо цифр) → ОГЭ, вариант 7, вкладка 13.2")

# --- другие каталоги и разделы
assert go("открой вариант 12 егэ") == ("ok", {"cat": EGE, "n": 12, "part": "p-text"})
assert go("Открой задание 27 для варианта 30 ЕГЭ") == ("ok", {"cat": EGE, "n": 30, "part": "p-essay"})
assert go("открой эссе 13.1 до 80 слов вариант 4") == ("ok", {"cat": OGE80, "n": 4, "part": "p-131"})
assert go("открой двадцать пятый вариант огэ") == ("ok", {"cat": OGE, "n": 25, "part": "p-text"})
assert go("о каталоге егэ") == ("ok", {"cat": EGE, "n": "about", "part": None})
ok("ЕГЭ (текст / задание 27), «до 80 слов», числа словами, «о каталоге»")

# --- контекст страницы: что открыто сейчас
ctx = {"cat": EGE, "variant": 13}
assert go("открой текст", ctx) == ("ok", {"cat": EGE, "n": 13, "part": "p-text"})
assert go("следующий вариант", ctx) == ("ok", {"cat": EGE, "n": 14, "part": "p-text"})
assert go("открой 13.3", {"cat": OGE, "variant": 7}) == ("ok", {"cat": OGE, "n": 7, "part": "p-133"})
assert go("открой вариант 7", {"cat": EGE}) == ("ok", {"cat": EGE, "n": 7, "part": "p-text"})
assert go("открой вариант 7", {"cat": OGE80}) == ("ok", {"cat": OGE80, "n": 7, "part": "p-text"})
assert go("открой эссе 13.2", {"cat": OGE80, "variant": 4}) == ("ok", {"cat": OGE80, "n": 4, "part": "p-132"})
assert go("предыдущий вариант", {"cat": EGE, "variant": 1})[0] == "err"
ok("контекст: «открой текст», «следующий вариант», «открой 13.3», каталог открытого экрана")

# --- уточнения и ошибки: ничего не выдумываем
assert go("открой вариант 7")[0] == "ask"
f = m.merge(m.parse("открой вариант 7"), m.parse("ОГЭ"))
assert m.is_nav("ОГЭ", m.parse("ОГЭ"), has_prev=True) and m.resolve(f, {}) == ("ok", {"cat": OGE, "n": 7, "part": "p-text"})
assert go("открой эссе 13.2")[0] == "ask"                                     # какой вариант?
r = go("открой вариант 40 огэ"); assert r[0] == "err" and r[2] == "n"
r = go("открой задание 27 для варианта 7 ОГЭ"); assert r[0] == "err" and r[2] == "part"
r = go("открой эссе 13.2 варианта 7 егэ"); assert r[0] == "err" and r[2] == "part"
ok("не хватает каталога/варианта — переспрос; вариант 40, «27 в ОГЭ», «13.2 в ЕГЭ» — понятная ошибка")

# --- это не навигация: вопросы по теме и команды кабинета идут обычным путём
for phrase in ("Объясни критерии 13.2 в варианте 7", "как написать эссе 13.2", "добавь занятие завтра в 15:00 Ивану",
               "перепиши сочинение к варианту 7", "сколько оплат за неделю"):
    assert go(phrase) is None, phrase
ok("вопросы и команды кабинета не принимаются за «открой страницу»")

# --- безопасность: цель всегда из белого списка
for phrase in ("открой вариант 7 огэ", "открой эссе 13.1 варианта 36 егэ", "открой ../../etc/passwd вариант 3 огэ"):
    r = go(phrase)
    if r and r[0] == "ok":
        t = r[1]
        assert t["cat"] in m.CATS and (t["n"] == "about" or 1 <= t["n"] <= 36) and (t["part"] is None or t["part"] in m.PARTS), t
ok("каталог — из CATS, вариант 1…36, вкладка — из PARTS")
print("test_matnav: все проверки пройдены")
