"""APK: «Снять камерой» в чате ИИ и файлы из чата.

Регресс на сломанную съёмку: capture="environment" у <input> открывал системный раздел «Фото»
(галерею) вместо камеры — onShowFileChooser игнорировал isCaptureEnabled(). Здесь же —
  • выбор файла не навязывает image/* («Файл» в чате — PDF/Word/TXT) и умеет несколько файлов;
  • снимок пишется в content:// URI (CaptureProvider) — file:// запрещён с Android 7;
  • файл из чата (docx/pdf/png) скачивается и пересылается: shareFile в JS-мосте, mime не зашит;
  • «Снять камерой» открывает камеру ПРЯМО в приложении (getUserMedia) и умеет несколько
    снимков подряд (chCam/chCamShot/chCamDone), обёртка даёт камере разрешение (camStatus/askCamera);
    системная камера через capture остаётся запасным путём.
Проверяются обе копии (dev и prod). Запуск: python3 tests/test_apk_camera.py
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def ok(c):
    print("  ✓", c)


for side in ("dev", "prod"):
    java = open(os.path.join(REPO, side, "apk", "src", "ru", "kabinet", "tutor", "MainActivity.java"),
                encoding="utf-8").read()
    prov = open(os.path.join(REPO, side, "apk", "src", "ru", "kabinet", "tutor", "CaptureProvider.java"),
                encoding="utf-8").read()
    manifest = open(os.path.join(REPO, side, "apk", "AndroidManifest.xml"), encoding="utf-8").read()
    html = open(os.path.join(REPO, side, "bot-mini-app", "docs", "index.html"), encoding="utf-8").read()

    # --- съёмка камерой: capture → камера, а не «Фото» ---
    chooser = re.search(r"public boolean onShowFileChooser\(.*?\n            \}", java, re.S)
    assert chooser, f"{side}: нет onShowFileChooser"
    body = chooser.group(0)
    assert "isCaptureEnabled()" in body and "launchCamera(" in body, \
        f"{side}: capture=\"environment\" не обрабатывается — «Снять камерой» открывает галерею"
    assert 'setType("image/*")' not in body, \
        f"{side}: выбор файла принудительно сужается до image/* — «Файл» (PDF/Word/TXT) не открыть"
    assert "EXTRA_ALLOW_MULTIPLE" in body, f"{side}: несколько файлов в чат не прикрепить"
    assert "ACTION_IMAGE_CAPTURE" in java and "MediaStore.EXTRA_OUTPUT" in java and "CaptureProvider" in java, \
        f"{side}: снимок должен писаться в content:// URI (без EXTRA_OUTPUT — превьюшка вместо фото)"
    assert "CAMERA_REQ" in re.search(r"protected void onActivityResult\(.*?\n    \}", java, re.S).group(0), \
        f"{side}: результат съёмки не возвращается в <input type=\"file\">"
    ok(f"{side}: «Снять камерой» открывает камеру, снимок возвращается в чат; выбор файла — все форматы")

    # --- провайдер файла снимка: только свои файлы из cacheDir ---
    assert "class CaptureProvider" in prov and "openFile" in prov and "getCacheDir()" in prov \
        and "ParcelFileDescriptor.open" in prov, \
        f"{side}: CaptureProvider должен отдавать только файлы камеры из cacheDir (без путей наружу)"
    assert re.search(r'android:name="(?:ru\.kabinet\.tutor)?\.?CaptureProvider"', manifest) \
        and "grantUriPermissions" in manifest
    assert '<uses-permission android:name="android.permission.CAMERA"' in manifest, \
        f"{side}: без разрешения CAMERA встроенная камера «Снять камерой» (getUserMedia) не работает"
    assert 'android:permission="android.permission.CAMERA"' not in manifest   # ограничение-атрибут на компонентах не ставим
    ok(f"{side}: CaptureProvider отдаёт только файлы камеры из cacheDir (без путей наружу); CAMERA заявлена для встроенной съёмки")

    # --- встроенная камера «Снять камерой»: превью + несколько снимков подряд ---
    assert "function chCam()" in html and "chCamShot" in html and "getUserMedia" in html, \
        f"{side}: «Снять камерой» не открывает камеру в приложении (нет chCam/chCamShot/getUserMedia)"
    assert "chCamDone" in html and "chCamDel" in html and "chCamS" in html, \
        f"{side}: нельзя сделать несколько снимков подряд и прикрепить их к задаче"
    assert "camTake" in html and "camDrop" in html \
        and re.search(r"pagehide.{0,40}camDrop|camDrop.{0,40}pagehide", html), \
        f"{side}: поток камеры не переиспользуется — Telegram спрашивает доступ при каждом нажатии «Снять камерой»"
    assert "CH_CAM_ICON" in html and "camPulse" in html, \
        f"{side}: кнопка затвора без иконки/пульсации"
    assert "RESOURCE_VIDEO_CAPTURE" in java and "askCameraPermission" in java \
        and re.search(r"@JavascriptInterface\s+(public\s+\S+\s+)?camStatus\s*\(", java) \
        and re.search(r"@JavascriptInterface\s+(public\s+\S+\s+)?askCamera\s*\(", java), \
        f"{side}: обёртка не пускает камеру в WebView (нет camStatus/askCamera и выдачи RESOURCE_VIDEO_CAPTURE)"
    ok(f"{side}: «Снять камерой» открывает камеру в приложении, разрешение — один раз, снимков — несколько подряд")

    # --- файлы из чата: скачать и переслать в любом формате ---
    assert "shareFile" in java and 'startShare(url, name, "application/pdf", 0)' in java, \
        f"{side}: JS-мост sharePdf остался без shareFile для docx/png"
    assert re.search(r"private void startShare\(final String url, final String name, final String mime, final int attempt\)", java), \
        f"{side}: startShare должен принимать mime — файлы пересылаются только как PDF"
    assert "s.setType(mime)" in java, f"{side}: окно пересылки шлёт всё как PDF"
    assert "shareFile" in html and "chFileTap" in html
    ok(f"{side}: файл из чата скачивается и пересылается (Telegram и другие контакты) в любом формате")

print("OK")
