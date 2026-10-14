"""Раздел «Материалы»: каталог эссе ОГЭ-2027 — файл на месте, маршрут закрыт входом, чипсина есть во фронте.
Статическая проверка (без сети и без запуска бота). Запуск: python3 tests/test_materials.py"""
import os, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def ok(c): print("  ✓", c)
def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f: return f.read()

bot, front = read("bot.py"), read("docs", "index.html")
page = read("materials", "oge-essays-2027.html")

assert page.count('<section class="variant"') == 36 and "Дощинского" in page
ok("materials/oge-essays-2027.html на месте: 36 вариантов")

assert re.search(r'"oge-essays-2027":\s*"oge-essays-2027\.html"', bot)
assert re.search(r'@guarded\s+async def api_material\(', bot), "маршрут материалов должен быть под @guarded (вход обязателен)"
assert 'add_get("/api/materials/{mid}", api_material)' in bot
ok("/api/materials/{id} зарегистрирован и закрыт @guarded")

title = "Каталог эссе для ОГЭ-2027. Русский язык. По сборнику Р.А. Дощинского"
assert title in front and 'tab=="mt"?mat()' in front and "function matOpen" in front
ok("во фронте: чипсина «" + title + "» и открытие каталога")

assert 'src="vishenka_cover.png"' in page and 'src="vishenka.png"' in read("materials", "ege-essays-2027.html") and "vishenka(?:_cover)?" in front
ok("обложки каталогов подменяются встроенным талисманом (отдельного png нет)")
page2 = read("materials", "ege-essays-2027.html")
assert page2.count('<section class="variant"') == 36 and 'ege-essays-2027' in bot
assert "Каталог сочинений ЕГЭ-2027. Русский язык. По сборнику Р.А. Дощинского" in front
ok("второй материал: каталог сочинений ЕГЭ-2027 (36 вариантов) — файл, маршрут и чипсина на месте")
print("OK")
