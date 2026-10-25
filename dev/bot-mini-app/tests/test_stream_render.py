"""Контракт стрима: экран не должен «сбиваться» и белееть на длинных ответах модели.

Два бага из bug-reports/008-stream-blank-screen.md имели один корень: стрим перерисовывал экран
ЦЕЛИКОМ на каждой дельте модели, а накапливаемый текст ничем не ограничен. Для PDF/Word/картинки
модель отдаёт в шаг «пишет ответ» целый HTML-документ (десятки КБ), для «ИИ-проверки» — десятки КБ
мышления; квадратичный рост работы на чанк вешал WebView, экран белеел, хотя файл спокойно уходил
в Telegram.

Тест держит три защитных механизма на месте:
  1) перерисовка стрима не чаще кадра анимации (chUpdSoon / ecUpdProcNow),
  2) потолок текста в шаге «пишет ответ» (CH_WRITE_MAX) и мышления проверки (EC_THINK_MAX),
  3) исключение в шаблоне чата не роняет и не замораживает чат.

Запуск: python3 tests/test_stream_render.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

HTML = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()


def ok(c):
    print("  ✓", c)


# ---------- чат с ИИ ----------
assert "function chUpdSoon(" in HTML, "нет chUpdSoon: перерисовка стрима не ограничена кадром анимации"
assert 'if(tab=="ch")chUpdSoon()},{idle:CH_IDLE})' in HTML, \
    "обработчик событий /api/chat всё ещё зовёт chUpd(true) на каждой дельте"
assert "chUpdPending" in HTML and "requestAnimationFrame" in HTML
ok("чат: перерисовка стрима не чаще одного кадра анимации (chUpdSoon)")

assert "const CH_WRITE_MAX=" in HTML, "нет потолка текста в шаге «пишет ответ»"
assert "w.t=(w.t+ev.v).slice(-CH_WRITE_MAX)" in HTML, "текстовый ответ копится в шаге без потолка"
assert 'chStepCur("write",fmt).t+=ev.v' not in HTML, \
    "сырой текст (для файлов — целый HTML-документ) по-прежнему складывается в DOM без потолка"
ok("чат: шаг «пишет ответ» ограничен потолком, HTML-документ не рендерится в пузырь")

m = re.search(r"function chUpd\(toEnd\)\{(.*?)\nfunction ", HTML, re.S)
assert m, "не нашли функцию chUpd"
assert "try{l.innerHTML=chList()}catch(e){return}" in m.group(1), \
    "chList() вызывается без try/catch — исключение в шаблоне роняет/замораживает чат"
ok("чат: исключение в шаблоне не роняет и не замораживает чат (try/catch в chUpd)")

# ---------- ИИ-проверка ----------
assert "const EC_THINK_MAX=" in HTML, "нет потолка мышления в «ИИ-проверке»"
assert "ecThink=(ecThink+ev.v).slice(-EC_THINK_MAX)" in HTML, "мышление проверки копится без потолка"
assert "ecThink+=ev.v" not in HTML
ok("ИИ-проверка: мышление ограничено потолком EC_THINK_MAX (DOM не растёт вместе с ответом)")

assert "function ecUpdProcNow(" in HTML, "нет ecUpdProcNow: перерисовка экрана не ограничена кадром"
assert "ecUpdPending" in HTML
head = re.search(r"function ecUpdProc\(\)\{(.*?)function ecUpdProcNow", HTML, re.S)
assert head and "requestAnimationFrame" in head.group(1), "ecUpdProc не откладывает перерисовку на кадр"
ok("ИИ-проверка: перерисовка экрана не чаще одного кадра анимации (ecUpdProcNow)")

# логика экрана проверки должна сохраниться один в один
assert 'if(ecThink&&!t){render();t=$("#ec-think")}' in HTML, "потеряна перерисовка экрана при первой мысли"
assert "if(t){t.textContent=ecThink;t.scrollTop=t.scrollHeight}" in HTML, "потерян живой вывод мышления"
assert 'if(g)g.innerHTML=ecLogHtml()' in HTML and 'if(b)b.style.width=ecBarW()+"%"' in HTML, \
    "потеряны этапы/полоса прогресса экрана проверки"
ok("ИИ-проверка: исходная логика экрана (этапы, мышление, полоса) не изменена")

print("\nALL STREAM RENDER TESTS PASSED")
