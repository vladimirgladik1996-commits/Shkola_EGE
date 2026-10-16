"""Голосовой ввод в чате с ИИ идёт в Whisper, а не в браузерное распознавание речи.

Раньше кнопка микрофона в чате использовала Web Speech API (webkitSpeechRecognition):
в WebView Telegram его либо нет, либо он понимает русский заметно хуже — отсюда «плохо
распознаёт команды». Теперь запись идёт через MediaRecorder и распознаётся на сервере
Whisper'ом (тот же путь, что у голосовых команд Вишенки).

1. Сервер: POST /api/stt — звук → текст через whisper.transcribe, без разбора команды.
2. Мини-апп: кнопка микрофона пишет звук (MediaRecorder / нативная запись обёртки APK)
   и отправляет запись на /api/stt; расшифровка вставляется в поле ввода.
3. Web Speech API — только запасной путь, когда запись звука недоступна.
Запуск: python3 tests/test_chat_mic.py
"""
import os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
bot = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
def ok(c): print("  ✓", c)

# --- сервер: /api/stt -------------------------------------------------------
assert 'add_post("/api/stt"' in bot, "POST /api/stt не зарегистрирован"
ok("POST /api/stt зарегистрирован")
m = re.search(r"async def api_stt\(request\):(.*?)\n(@guarded|async def |# ---)", bot, re.S)
assert m, "api_stt не найден"
body = m.group(1)
assert "voice_audio" in body and "whisper.transcribe" in body, "api_stt не распознаёт звук через Whisper"
ok("api_stt: звук → whisper.transcribe (язык задаётся в whisper.py)")
assert "handle_page" not in body and "confirm_page" not in body, "api_stt не должен разбирать команды"
ok("api_stt только распознаёт — команды не разбирает, текст возвращается клиенту")

# --- мини-апп: запись звука и отправка на /api/stt ---------------------------
mm = re.search(r"function chMicUi\(\)(.*?)\nfunction chGrow", html, re.S)
assert mm, "блок микрофона чата не найден"
blk = mm.group(1)
assert "function chMic()" in blk and "MediaRecorder" in blk and '"/api/stt"' in blk, \
    "микрофон чата не записывает звук и не отправляет его на /api/stt"
ok("кнопка микрофона в чате пишет звук (MediaRecorder) и отправляет на /api/stt")
assert "micTake" in blk and "nativeRec" in blk, "нет защищённого пути записи (общий микрофон micTake + нативная запись обёртки)"
ok("запись переиспользует общий микрофон сеанса (micTake — разрешение просят один раз) + нативную запись APK")
assert "micStatus" in blk and "askMic" in blk, "состояние микрофона не спрашивается до getUserMedia"
ok("обёртка APK спрашивается о микрофоне ДО getUserMedia — без гонок с системным диалогом")
assert "chSendRec" in blk and "chRecKill" in blk and "function chStop()" in blk, \
    "нет остановки записи с расшифровкой (chSendRec) и отмены (chRecKill/chStop)"
ok("запись можно распознать (chSendRec) и отменить (chStop) — сворачивание вкладки гасит микрофон")
assert re.search(r"chSendRec\(\);return\}", html), "кнопка «Отправить» не дожидается расшифровки записи"
ok("«Отправить» во время записи сначала распознаёт её — голос не теряется")

# --- Web Speech API — только запасной путь -----------------------------------
assert "CH_SR" in blk and blk.index("MediaRecorder") < blk.index("new CH_SR"), \
    "распознавание должно идти через Whisper; Web Speech — только запасной путь"
ok("Web Speech API включается только когда запись звука недоступна")

# --- крупная цель нажатия ----------------------------------------------------
sz = re.search(r"#aic-mic\{width:(\d+)px;height:(\d+)px", html)
assert sz and min(int(sz.group(1)), int(sz.group(2))) >= 40, "кнопка микрофона в чате слишком мелкая для нажатия пальцем"
ok("кнопка микрофона не меньше 40 px — по ней легко попасть пальцем")
print("OK")
