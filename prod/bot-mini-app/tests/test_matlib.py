"""Материалы для Вишенки (matlib): индекс каталогов, поиск и контекст для модели.

Проверяется требование «сначала материалы, потом интернет»:
  • запрос про вариант («перепиши эссе к варианту ОГЭ 4…») получает контекст с тестом варианта
    и текущим сочинением/эссе из раздела «Материалы»;
  • виджет Вишенки открывает страницу варианта, уточняет каталог (ОГЭ / ОГЭ «до 80 слов» / ЕГЭ),
    если совпадений несколько, и ищет по номерам, авторам и тексту страниц.
Без сети и без запуска бота. Запуск: python3 tests/test_matlib.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import matlib


def ok(c):
    print("  ✓", c)


# --- каталоги: три, по 36 вариантов, у каждого автор и строка поиска ---
assert [c["id"] for c in matlib.CATS] == ["oge-essays-2027", "oge-essays-2027-vishenka", "ege-essays-2027"]
for c in matlib.CATS:
    vs = matlib.variants(c["id"])
    assert [v["n"] for v in vs] == list(range(1, 37)) and all(v["sub"] and v["q"] for v in vs), c["id"]
    assert "Дощинск" in c["t"] or "Дощинского" in c["t"], c["id"]
ok("три каталога (ОГЭ, ОГЭ «до 80 слов», ЕГЭ) по 36 вариантов — у каждого автор текста и вопросы")

# --- чат: материалы подставляются ДО поиска в интернете (текст + задания + текущее сочинение) ---
ctx, top = matlib.context("перепиши эссе к варианту ОГЭ 4 из сборника дощинского")
assert {m["cat"] for m in top} == {"oge-essays-2027", "oge-essays-2027-vishenka"} and {m["n"] for m in top} == {4}
assert "МАТЕРИАЛЫ ИЗ РАЗДЕЛА" in ctx and "Исходный текст" in ctx
assert "эссе" in ctx.lower() and "ОГЭ-2027" in ctx
ok("«перепиши эссе к варианту ОГЭ 4» → в модель идут оба каталога: тест варианта и текущие эссе")

ctx, top = matlib.context("сделай сочинение ЕГЭ к варианту 30 по другой структуре")
assert [(m["cat"], m["n"]) for m in top] == [("ege-essays-2027", 30)]
assert "Исходный текст" in ctx or "Текст для анализа" in ctx
assert "сочинение" in ctx.lower() or "Сочинение" in ctx
ok("«сочинение ЕГЭ к варианту 30» → материалы варианта 30 (текст задания + текущее сочинение)")

ctx, top = matlib.context("перепиши эссе к варианту ОГЭ 4 до 80 слов")
assert [(m["cat"], m["n"]) for m in top] == [("oge-essays-2027-vishenka", 4)] and "до 80 слов" in ctx
ok("уточнение «до 80 слов» выбирает каталог Вишенки")

ctx, top = matlib.context("какая погода в Москве")
assert ctx == "" and top == []
ok("посторонний запрос не тащит материалы — модель отвечает без них")

ctx, top = matlib.context("перепиши сочинение к варианту 77 ОГЭ")
assert not top and "ничего не нашлось" in ctx
ok("в материалах ничего нет → честная пометка «проверено, ничего не нашлось» (интернет — после проверки)")

# --- виджет Вишенки: открыть / уточнить / поиск по авторам и тексту ---
r = matlib.resolve("найди эссе ОГЭ к варианту 4")
assert r and r["kind"] == "choose" and {c["cat"] for c in r["cands"]} == {"oge-essays-2027", "oge-essays-2027-vishenka"}
assert "какой нужен" in r["reply"].lower() or "какой" in r["reply"].lower()
pick = matlib.choose("до 80 слов", r["cands"])
assert pick["kind"] == "open" and pick["cat"] == "oge-essays-2027-vishenka" and pick["n"] == 4
ok("«найти эссе ОГЭ к варианту 4» → уточняет каталог (два сборника), ответ «до 80 слов» открывает вариант")

r = matlib.resolve("открой вариант 4 ОГЭ до 80 слов")
assert r["kind"] == "open" and r["cat"] == "oge-essays-2027-vishenka" and r["n"] == 4
r = matlib.resolve("открой вариант 7 ЕГЭ")
assert r["kind"] == "open" and r["cat"] == "ege-essays-2027" and r["n"] == 7
ok("однозначный запрос открывает нужную страницу варианта прямо на экране")

r = matlib.resolve("какие варианты по Чехову")
assert r["kind"] == "answer" and "вариант" in r["reply"] and ("Чехов" in r["reply"] or "чехов" in r["reply"].lower())
r = matlib.resolve("у какого автора вариант 2")
assert r["kind"] == "answer" and "вариант 2" in r["reply"]
ok("поиск по авторам и номерам работает и отвечает текстом")

hits = matlib.search("какие знаки препинания помогают автору передать чувства героев")
assert hits and hits[0]["n"] == 1
ok("поиск по тексту заданий находит нужный вариант")

r = matlib.resolve("сократи эссе 13.2 вариант 4 ОГЭ до 80 слов")
assert r["kind"] == "task" and "МАТЕРИАЛЫ" in r["context"]
ok("задача по варианту идёт с контекстом материалов (модель изучает текст и эссе до ответа)")

assert matlib.resolve("добавь занятие Ивану на завтра") is None
assert matlib.resolve("запиши оплату 2000") is None
ok("команды кабинета не перехватываются разбором материалов")

print("test_matlib: OK")
