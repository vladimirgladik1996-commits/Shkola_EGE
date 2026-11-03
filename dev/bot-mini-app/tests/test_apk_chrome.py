"""APK выглядит как мини-апп в Telegram: системные панели в цветах приложения + отступ сверху.

В Telegram над страницей — шапка Telegram (56dp), её цвет и цвет нижней панели задаются
tg.setHeaderColor / setBottomBarColor (= --bg страницы). В APK этого нет, поэтому оболочка делает то же сама:
  • статус-бар и нижняя панель телефона красятся в #FBF7E9, значки тёмные (MainActivity.paintBars);
  • WebView лежит ниже полосы TG_BAR_DP = 12dp того же цвета (root.setPadding): как в Telegram (там шапка 56dp), но на 44dp выше —
    гамбургер и название страницы чуть ниже, чем в старом APK, и заметно выше, чем при 56;
  • страница узнаёт новую оболочку по Android.shell()>=2 (html.apk2: шапка кабинета 56, как в Telegram)
    и сама отдаёт цвет в панели через Android.setBarColor (brand()).
Старая оболочка (без shell()) продолжает работать: html.apk{--hd:64px} остаётся для неё.
Проверяются обе копии (dev и prod).
Запуск: python3 tests/test_apk_chrome.py
"""
import os, re
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
def ok(c): print("  ✓", c)

for side in ("dev", "prod"):
    java = open(os.path.join(REPO, side, "apk", "src", "ru", "kabinet", "tutor", "MainActivity.java"), encoding="utf-8").read()
    html = open(os.path.join(REPO, side, "bot-mini-app", "docs", "index.html"), encoding="utf-8").read()

    # --- оболочка ---
    assert "0xFFFBF7E9" in java, f"{side}: цвет панелей должен быть --bg страницы (#FBF7E9)"
    assert "setStatusBarColor" in java and "setNavigationBarColor" in java, f"{side}: статус-бар и нижняя панель не красятся"
    assert "SYSTEM_UI_FLAG_LIGHT_STATUS_BAR" in java and "APPEARANCE_LIGHT_STATUS_BARS" in java, \
        f"{side}: на светлом фоне значки статус-бара должны быть тёмными (API 23–29 и 30+)"
    assert "SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR" in java and "APPEARANCE_LIGHT_NAVIGATION_BARS" in java, \
        f"{side}: на светлом фоне значки нижней панели должны быть тёмными"
    assert re.search(r"Build\.VERSION\.SDK_INT\s*<\s*26", java), \
        f"{side}: на Android 7.x светлая нижняя панель без тёмных значков нечитаема — нужен запасной чёрный цвет"
    assert re.search(r"TG_BAR_DP\s*=\s*12\b", java), f"{side}: отступ сверху — 12dp (между старым APK и шапкой Telegram 56dp)"
    assert re.search(r"root\.setPadding\(0,\s*dp\(TG_BAR_DP\),\s*0,\s*0\)", java), \
        f"{side}: WebView должен лежать ниже полосы под шапку Telegram"
    assert re.search(r"paintBars\(APP_BG\);", java) and java.count("paintBars(APP_BG)") >= 2, \
        f"{side}: панели красятся и на экране выбора адреса, и в WebView"
    assert "public int shell()" in java and "SHELL_VERSION = 2" in java, f"{side}: страница не узнает новую оболочку"
    assert "public void setBarColor(" in java and "Color.parseColor(css)" in java, f"{side}: нет Android.setBarColor"
    assert "shellRoot.setBackgroundColor(barColor)" in java, f"{side}: фон полосы сверху должен стать цветом страницы после прелоадера"
    ok(f"{side}: панели телефона в цвет страницы, WebView под полосой 12dp, мост shell()/setBarColor на месте")

    # --- страница ---
    assert 'classList.add("apk2")' in html and "Android.shell()>=2" in html, f"{side}: страница не ставит html.apk2"
    assert re.search(r"html\.apk\.apk2\{--hd:56px\}", html), f"{side}: шапка кабинета в новой оболочке — 56px, как в Telegram"
    assert re.search(r"html\.apk\{--hd:64px\}", html), f"{side}: старая оболочка (без shell()) должна выглядеть как раньше"
    assert "window.Android.setBarColor(c)" in html, f"{side}: brand() не отдаёт цвет панелям APK"
    brand = html[html.index("function brand()"):html.index("brand();if(tg.onEvent)")]
    assert "tg.setHeaderColor(c)" in brand and "setBarColor" in brand, f"{side}: brand() красит и Telegram, и APK"
    ok(f"{side}: страница различает оболочку v2 и красит панели APK через brand()")

print("OK")
