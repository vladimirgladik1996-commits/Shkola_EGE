"""Обёртка APK ↔ страница: открытие «Материалов» не должно сворачивать/ронять приложение.
Регресс на два вылета, которые уже случались:
  • навигации вложенных фреймов (материалы, blob-PDF) уходили в Intent.ACTION_VIEW — приложение
    «сворачивалось» в браузер либо падало (ActivityNotFoundException);
  • сброс src/srcdoc фрейма материала — это навигация about:blank, попадающая в ту же обёртку.
Плюс страховки: startActivity с обработчиком отсутствия браузера и переживание смерти рендерера
WebView (без onRenderProcessGone Android просто закрывает приложение).
Проверяются обе копии (dev и prod) — они обязаны быть идентичны (см. test_release_contract)."""
import os, re
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
def ok(c): print("  ✓", c)

for side in ("dev", "prod"):
    java = open(os.path.join(REPO, side, "apk", "src", "ru", "kabinet", "tutor", "MainActivity.java"),
                encoding="utf-8").read()
    html = open(os.path.join(REPO, side, "bot-mini-app", "docs", "index.html"), encoding="utf-8").read()

    # --- обёртка ---
    assert "isForMainFrame" in java, f"{side}: вложенные фреймы уходят наружу — материалы вылетают из приложения"
    assert re.search(r"try\s*\{[^{}]*startActivity\(new Intent\(Intent\.ACTION_VIEW", java), \
        f"{side}: ссылка наружу без try/catch — нет браузера = падение приложения"
    assert '"blob".equals(scheme)' in java and '"about".equals(scheme)' in java and '"data".equals(scheme)' in java, \
        f"{side}: служебные схемы blob:/data:/about: должны оставаться внутри WebView"
    assert "onRenderProcessGone" in java and "RenderProcessGoneDetail" in java, \
        f"{side}: смерть рендерера WebView просто закрывает приложение — нужен onRenderProcessGone"
    ok(f"{side}: обёртка держит фреймы и служебные схемы внутри WebView, ссылки наружу — с обработчиком")

    # --- страница ---
    assert "fr.srcdoc=" in html, f"{side}: вариант должен открываться через srcdoc (blob-URL ломается в WebView)"
    assert "matFr" in html and 'removeAttribute("srcdoc")' not in html, \
        f"{side}: фрейм материала должен пересоздаваться, а не перезагружаться (about:blank-навигация)"
    assert "scrollTo({" not in html, \
        f"{side}: scrollTo({behavior:...}) кидает TypeError в старом WebView — только scrollTo(0,0)"
    assert "demoData" not in html and "Демо-режим" not in html and "EC_MOK" not in html, \
        f"{side}: демо-данные не должны подменять реальные (занятия/оплаты уходили чужим ученикам)"
    assert 'cache:"no-store"' in html, f"{side}: /api/* без cache:no-store — WebView может отдать старые данные"
    ok(f"{side}: страница открывает материалы без навигаций и не подменяет данные демо-режимом")

print("OK")
