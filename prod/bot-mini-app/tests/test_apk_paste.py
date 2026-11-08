"""Вставка текста в АПК: долгий тап по полю ввода открывает системное «Вставить/Копировать».

Регресс: web.setOnLongClickListener всегда возвращал true — долгий тап съедался целиком,
WebView никогда не запускал свой режим выделения, и в диалоговое окно «Чата с ИИ» нельзя
было вставить скопированный текст. Стало: палец на textarea/input/contenteditable
определяется на ACTION_DOWN (elementFromPoint; JS асинхронный — ответ готов к моменту
долгого тапа) и такой тап отдаётся WebView (return false) — меню «Вставить/Копировать»
появляется штатно. Служебное меню «Сменить адрес» ведёт себя как раньше: кнопки, шторка
и флаг страницы по-прежнему гасят его, на пустом месте — показывается.

Проверяются обе копии (dev и prod) — их идентичность держит test_release_contract.
Запуск: python3 tests/test_apk_paste.py
"""
import os, re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def ok(c):
    print("  ✓", c)


for side in ("dev", "prod"):
    java = open(os.path.join(REPO, side, "apk", "src", "ru", "kabinet", "tutor", "MainActivity.java"),
                encoding="utf-8").read()
    html = open(os.path.join(REPO, side, "bot-mini-app", "docs", "index.html"), encoding="utf-8").read()

    # --- поле ввода под пальцем определяется заранее, на касании ---
    assert re.search(r"lpField\s*=\s*false;", java), "%s: флаг поля ввода должен сбрасываться на ACTION_DOWN" % side
    assert "isContentEditable" in java and "closest('textarea,input')" in java, \
        "%s: поля ввода (textarea/input/contenteditable) должны определяться по точке касания" % side
    ok("%s: касание определяет поле ввода (textarea/input/contenteditable) заранее" % side)

    # --- долгий тап по полю ввода — WebView: системное «Вставить/Копировать» ---
    assert re.search(r"if \(lpField\) return false;", java), \
        "%s: долгий тап по полю ввода обязан отдаваться WebView — иначе вставить текст нельзя" % side
    ok("%s: долгий тап по полю ввода уходит WebView (меню «Вставить/Копировать» появляется штатно)" % side)

    # --- служебное меню — как раньше: не по кнопкам/шторке, на пустом месте — «Сменить адрес» ---
    block = java[java.index("web.setOnLongClickListener"):java.index("web.loadUrl")]
    assert "window.__lpOnBtn" in block and "#sheet.on" in block and "elementFromPoint" in block, \
        "%s: страховки служебного меню (флаг страницы, шторка, точка касания) должны остаться" % side
    assert "Сменить адрес сервера" in block and re.search(r"return true;\s*\}\);", block), \
        "%s: служебное меню по-прежнему своё и потребляет долгий тап вне полей ввода" % side
    ok("%s: служебное меню «Сменить адрес» ведёт себя как раньше" % side)

    # --- поле ввода чата — textarea: именно его чинит контракт ---
    assert '<textarea id="aic-in"' in html, "%s: поле ввода «Чата с ИИ» должно быть textarea" % side
    ok("%s: диалоговое окно «Чата с ИИ» — textarea (попадает под фикс)" % side)
print("test_apk_paste: OK")
