"""Регресс бага «виджет просит доступ к микрофону при КАЖДОМ нажатии кнопки «Голосовая команда»».

Корень бага: страница вызывала getUserMedia на каждую запись. WebView (Telegram и части
прошивок Android) показывает системный диалог на КАЖДЫЙ запрос микрофона — разрешение,
однажды подтверждённое, не запоминается, и кнопка спрашивала доступ заново.

Теперь микрофон берётся один раз на сеанс: общий поток micTake()/micDrop() переиспользуется
всеми записями — и голосовыми командами Вишенки, и микрофоном чата. Системный диалог —
только при первом обращении; дальше запись стартует без вопросов.

Инварианты:
  1. Ровно ОДИН вызов getUserMedia на МИКРОФОН — внутри micTake(); кнопки сами его не зовут.
     Камера «Снять камерой» — отдельный поток видео со своим одним вызовом в camTake(): разрешение
     просят один раз на сеанс, поток переиспользуется всеми съёмками (Telegram спрашивает доступ
     на КАЖДЫЙ getUserMedia) и освобождается при закрытии страницы (camDrop ← pagehide).
     Эти два инварианта проверяются с точностью до двух мест.
  2. micTake() переиспользует живой поток (проверка готовности треков) и пересоздаёт его,
     только если треки умерли.
  3. Остановка записи НЕ останавливает общий поток (иначе следующее нажатие снова спросит
     разрешение) — getTracks() есть только в micDrop().
  4. Поток освобождается честно: закрытие страницы (pagehide), сворачивание приложения
     (мост __lpPause) и «забытая в фоне» страница (таймер в visibilitychange).
  5. Контракт с обёрткой APK сохранён: micStatus/askMic до getUserMedia, повторные попытки
     и коды ошибок — как требует test_apk_mic.py.

Запуск: python3 tests/test_mic_permission.py
"""
import os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()


def ok(c):
    print("  ✓", c)


def fn(name):
    """Тело функции name(...) — до следующего определения/комментария верхнего уровня."""
    m = re.search(r"(?:async )?function %s\([^)]*\)\{" % name, html)
    assert m, f"функция {name}() не найдена"
    rest = html[m.end():]
    end = re.search(r"\n(?=(?:async )?function |window\.|document\.|/\*)", rest)
    return rest[:end.start()] if end else rest


# --- 1. один вызов getUserMedia на микрофон (второй — на камеру «Снять камерой») ---
calls = re.findall(r"navigator\.mediaDevices\.getUserMedia\(", html)
assert len(calls) == 2, f"getUserMedia вызывается из {len(calls)} мест — ожидается ровно два: микрофон (micTake) и камера (camTake)"
assert "getUserMedia" in fn("micTake") and "getUserMedia" in fn("camTake"), \
    "getUserMedia должен жить только в micTake() (микрофон) и camTake() (камера) — кнопки сами его не зовут"
ok("getUserMedia вызывается ровно в двух местах: micTake() — микрофон, camTake() — камера")
assert re.search(r"pagehide.{0,40}camDrop|camDrop.{0,40}pagehide", html), "поток камеры не освобождается при закрытии страницы (pagehide → camDrop)"
ok("поток камеры сеансовый: разрешение просят один раз, освобождение — при закрытии страницы")

assert "function micTake(" in html and "function micDrop(" in html, "нет общего микрофона сеанса (micTake/micDrop)"
ok("микрофон сеанса выделен: micTake() — взять/переиспользовать, micDrop() — освободить")

take = fn("micTake")
assert "getUserMedia" in take, "micTake не берёт поток у системы"
assert "readyState" in take, "micTake не проверяет живость потока — мёртвый трек не должен выдавать «запись идёт»"
assert "getTracks" not in take, "micTake не должен освобождать поток при переиспользовании"
ok("micTake переиспользует живой поток; пересоздаёт — только если треки умерли")

for f in ("voiceToggle", "chMic"):
    assert "micTake" in fn(f), f"{f} не берёт общий микрофон сеанса"
    assert ".getUserMedia(" not in fn(f), f"{f} снова зовёт getUserMedia напрямую — вернётся диалог при каждом нажатии"
ok("кнопки виджета и микрофон чата работают через общий поток, а не через свой getUserMedia")

# --- 2. остановка записи не убивает общий поток ------------------------------
drop = fn("micDrop")
assert "getTracks" in drop, "micDrop не освобождает поток — микрофон останется занятым навсегда"
assert len(re.findall(r"\.getTracks\(\)", html)) == 2 and "getTracks" in fn("camDrop"), \
    "потоки должны останавливаться только в micDrop() (микрофон) и camDrop() (камера) — иначе запись гасит общий микрофон, и разрешение спросят снова"
ok("getTracks() живёт только в micDrop() и camDrop(): конец записи ≠ конец микрофона")

for f in ("voiceStop", "chRecKill", "chSendRec"):
    assert "getTracks" not in fn(f), f"{f} останавливает общий поток — следующая запись снова спросит разрешение"
ok("остановка/отмена записи (виджет и чат) не трогает общий поток")

# --- 3. поток освобождается честно ------------------------------------------
assert re.search(r"window\.__lpPause=.*?micDrop", html, re.S), "__lpPause не освобождает микрофон при сворачивании приложения"
assert re.search(r"pagehide[^)]*micDrop|micDrop[^)]*pagehide", html), "pagehide не освобождает микрофон при закрытии страницы"
assert re.search(r"visibilitychange.*?setTimeout\(\s*micDrop", html, re.S), \
    "забытая в фоне страница держит микрофон вечно — нужен таймер освобождения"
ok("микрофон освобождается: сворачивание приложения, закрытие страницы, долгий фон (таймер)")

# --- 4. контракт с обёрткой APK сохранён ------------------------------------
assert "A.micStatus" in html and "A.askMic" in html, "состояние микрофона не спрашивается до записи"
assert re.search(r"for\s*\(let k=0;k<tries;k\+\+\)", html), "нет повторных попыток взятия потока"
assert "(код: " in html, "ошибки микрофона должны показывать точный код"
ok("контракт APK на месте: micStatus/askMic до записи, повторные попытки, коды ошибок")

# --- 5. две записи одновременно не бывает -----------------------------------
assert re.search(r"function voiceToggle\(\)\{.{0,400}?chStop\(\)", html, re.S), \
    "виджет не гасит запись чата перед своей записью — два рекордера на одном потоке"
assert re.search(r"function chMic\(\)\{.{0,400}?voiceStop\(\)", html, re.S), \
    "микрофон чата не гасит запись виджета перед своей записью"
ok("запись всегда одна: виджет и чат взаимно гасят чужую запись")

print("OK")
