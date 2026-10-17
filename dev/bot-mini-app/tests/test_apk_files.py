"""Вложения в «Чате с ИИ»: галерея, камера и файлы через WebView-обёртку APK.

Регресс бага «Снять камерой не открывает камеру»: onShowFileChooser решал, есть ли камера,
через cam.resolveActivity(...) != null — а на Android 11+ без <queries> в манифесте
resolveActivity() для implicit-intent камеры всегда возвращает null (package visibility),
и код молча уходил в галерею. Теперь камера запускается напрямую (сбой — только через
исключение, и обязательно с видимым сообщением), а манифест разрешает видеть пакеты камеры.

Плюс контракт остальных опций: accept-типы страницы приводятся к MIME (иначе «Файл» не мог
прикрепить PDF/Word), множественный выбор не ломается, снимок кладётся в файл приложения
через CaptureProvider и возвращается в страницу.
Проверяются обе копии (dev и prod) — они обязаны быть идентичны (см. test_release_contract).
Запуск: python3 tests/test_apk_files.py
"""
import os, re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def ok(c):
    print("  ✓", c)


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


for side in ("dev", "prod"):
    base = os.path.join(REPO, side, "apk")
    java = read(os.path.join(base, "src", "ru", "kabinet", "tutor", "MainActivity.java"))
    prov = read(os.path.join(base, "src", "ru", "kabinet", "tutor", "CaptureProvider.java"))
    man = read(os.path.join(base, "AndroidManifest.xml"))
    html = read(os.path.join(REPO, side, "bot-mini-app", "docs", "index.html"))

    # --- камера ---
    assert "<queries>" in man and "android.media.action.IMAGE_CAPTURE" in man, \
        f"{side}: манифест без <queries> для камеры — на Android 11+ resolveActivity() вернёт null и камера не откроется"
    assert "isCaptureEnabled" in java and "MediaStore.ACTION_IMAGE_CAPTURE" in java, \
        f"{side}: «Снять камерой» не запускает системную камеру"
    assert not re.search(r"resolveActivity\([^)]*\)\s*!=\s*null", java), \
        f"{side}: камера снова спрятана за resolveActivity() — на Android 11+ это всегда null"
    assert "Камера недоступна" in java, \
        f"{side}: сбой камеры происходит молча — пользователь должен видеть причину"
    assert "CaptureProvider.uriFor" in java and "MediaStore.EXTRA_OUTPUT" in java, \
        f"{side}: снимок должен сохраняться в файл приложения, а не теряться"
    ok(f"{side}: камера запускается напрямую, сбой виден пользователю, снимок идёт в файл приложения")

    # --- провайдер файла снимка ---
    assert "class CaptureProvider" in prov and "openFile" in prov, f"{side}: нет CaptureProvider для снимка"
    assert re.search(r'android:name="(?:ru\.kabinet\.tutor)?\.?CaptureProvider"', man), \
        f"{side}: CaptureProvider не прописан в манифесте"
    ok(f"{side}: CaptureProvider на месте и прописан в манифесте")

    # --- выбор файлов: галерея и «Файл» ---
    assert "fixChooserTypes" in java and "extToMime" in java, \
        f"{side}: accept-типы не приводятся к MIME — «Файл» не найдёшь в системном выборщике"
    assert 'i.setType("image/*")' not in java, \
        f"{side}: тип выборщика снова принудительно image/* — PDF/Word/TXT не прикрепить"
    assert "Intent.EXTRA_MIME_TYPES" in java and "onActivityResult" in java and "captureFile" in java, \
        f"{side}: нет MIME-фильтров или снимок не возвращается в страницу"
    ok(f"{side}: галерея и «Файл» получают свои MIME-типы, снимок возвращается в страницу")

    # --- страница отдаёт все три опции ---
    for inp in ("aic-ph", "aic-cam", "aic-fi"):
        assert f'id="{inp}"' in html, f"{side}: на странице нет ввода {inp}"
    assert 'capture="environment"' in html, f"{side}: у камеры нет capture — WebView не откроет камеру"
    ok(f"{side}: «Фото из галереи», «Снять камерой», «Файл» — все три опции встроены")

print("OK")
