"""Микрофон в APK ↔ виджет «Вишенка»: контракт обёртки и страницы.

Регресс бага «Микрофон недоступен — проверьте разрешения приложения» при уже
выданном разрешении. Три инварианта, из-за нарушения которых запись падала:

1. Обёртка НЕ держит pending-запрос WebView, пока открыт системный диалог:
   такой конфликт на части устройств отменяет запрос (onPermissionRequestCanceled)
   и getUserMedia падает без объяснений. Запрос страницы закрывается сразу,
   разрешение просится отдельно, страница повторяет нажатие.
2. Страница до getUserMedia спрашивает состояние у обёртки (micStatus/askMic)
   и ведёт в настройки (openSettings), когда микрофон запрещён навсегда.
3. Долгий тап по кнопкам виджета не открывает служебное меню «Сменить адрес
   сервера» (window.__lpOnBtn).
"""
import os, re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # bot-mini-app
BASE = os.path.dirname(HERE)                                          # dev|prod


def ok(c):
    print("  ✓", c)


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


html = read(os.path.join(HERE, "docs", "index.html"))
java = read(os.path.join(BASE, "apk", "src", "ru", "kabinet", "tutor", "MainActivity.java"))
manifest = read(os.path.join(BASE, "apk", "AndroidManifest.xml"))

# --- обёртка ---------------------------------------------------------------
assert "android.permission.RECORD_AUDIO" in manifest, "манифест без RECORD_AUDIO — WebView никогда не даст микрофон"
ok("манифест объявляет RECORD_AUDIO")

for m in ("micStatus", "askMic", "openSettings"):
    assert re.search(r"@JavascriptInterface\s+(public\s+\S+\s+)?%s\s*\(" % m, java), "нет JS-моста %s()" % m
ok("JS-мост: micStatus / askMic / openSettings на месте")

assert not re.search(r"micRequest\s*=\s*request", java), \
    "обёртка снова держит pending-запрос WebView поверх системного диалога — getUserMedia падает без объяснений"
assert re.search(r"request\.deny\(\);\s*askMicPermission\(true\);", java), \
    "при отсутствии разрешения запрос страницы должен закрываться сразу, а разрешение проситься отдельно"
ok("гонка «системный диалог поверх pending-запроса» закрыта: deny сразу, разрешение — отдельно")

assert re.search(r"window\.__lpOnBtn", java), "обёртка не проверяет __lpOnBtn перед служебным меню"
ok("служебное меню «Сменить адрес» не выскакивает по долгому тапу на кнопках")

# --- страница --------------------------------------------------------------
assert "A.micStatus" in html and "A.askMic" in html, "виджет не спрашивает состояние микрофона до getUserMedia"
ok("виджет спрашивает micStatus/askMic ДО getUserMedia — без гонок с системным диалогом")

assert "A.openSettings" in html and "micSettings" in html, \
    "когда микрофон запрещён навсегда, виджет должен вести в настройки приложения"
assert re.search(r"for\s*\(let k=0;k<2;k\+\+\)", html), "нет повторной попытки getUserMedia после выдачи разрешения"
ok("лестница восстановления: настройки при «больше не спрашивать» + одна повторная попытка записи")

assert "(код: " in html, "ошибка getUserMedia должна показывать точный код для диагностики"
ok("точный код ошибки getUserMedia виден пользователю — диагностика с первого раза")

assert "__lpOnBtn" in html and "contextmenu" in html, "страница не помечает нажатия на кнопках для служебного меню"
ok("страница помечает нажатия на кнопках (__lpOnBtn) и глушит contextmenu")

print("ИТОГ: контракт «микрофон APK ↔ виджет» соблюдён")
