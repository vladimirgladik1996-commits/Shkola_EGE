"""Контракт раздела «ИИ-проверка»: пункт меню, роутер и каркас экранов (пуш 3 из PUSH-PLAN.md).
Строки проверяются по docs/index.html — как фронт-контракты в test_chat_share.
Запуск: python3 tests/test_essay_check.py"""
import os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def ok(c): print("  ✓", c)

html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()

# --- 1. пункт меню и роутер ---
assert 'data-t="ai"' in html, "нет пункта меню «ИИ-проверка»"
assert re.search(r'ORDER=\["ch","c","p","s","ai","mt"\]', html), "«ai» выпал из ORDER"
assert 'ai:"ИИ-проверка"' in html, "нет имени раздела в NAMES"
ok("меню и роутер: пункт «ИИ-проверка» (data-t=ai) на месте")

# --- 2. render() ведёт tab=ai на каркас, а не на заглушку «Появится позже» ---
assert 'tab=="ai"?aiCheck()' in html, "render() не зовёт aiCheck для tab=ai"
assert "function aiCheck(" in html and "function ecShot(" in html, "нет функций каркаса"
ok("render(): tab=ai → aiCheck() — заглушка обойдена")

# --- 3. экран «Старт»: оффер и два выбора ---
for s in ("Сфотографируйте работу", "Сочинение для ЕГЭ", "Эссе для ОГЭ", "по критериям ФИПИ"):
    assert s in html, f"нет строки старта: {s}"
ok("экран «Старт»: оффер и два выбора (ЕГЭ / ОГЭ) на месте")

# --- 4. экран «Съёмка»: кадр, подсказки, наборы «Работа» / «Задание» ---
for s in ("Положите лист ровно", "Снимите условие задания",
          "Снимайте страницы по порядку", "Работа · 3", "Задание · 1"):
    assert s in html, f"нет строки съёмки: {s}"
ok("экран «Съёмка»: кадр, подсказки и наборы «Работа»/«Задание» на месте")

# --- 5. стили в собственном неймспейсе (урок коллизии .cf из коммита 4d5fa3e) ---
assert ".ec-hero{" in html and ".ec-shut{" in html and ".ec-chip{" in html, "нет стилей ec-"
ok("стили раздела — в неймспейсе ec-, коллизий с существующими классами нет")

print("test_essay_check: OK")
