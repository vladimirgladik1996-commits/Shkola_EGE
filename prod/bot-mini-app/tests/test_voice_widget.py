"""Виджет Вишенки (голосовой помощник): в окне только кнопка голосовых команд.
Строка ввода и кнопка «Спросить» — баг; тест не даёт им вернуться."""
import os, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
def ok(c): print("  ✓", c)

m = re.search(r"function chatOpen\(\)\{(.*?)\n(?=function chrBtns)", html, re.S)
assert m, "chatOpen() не найден"
body = m.group(1)

assert "voiceToggle()" in body and 'id="micb"' in body
ok("в окне Вишенки есть кнопка голосовых команд")

assert not re.search(r"<input|<textarea|contenteditable", body, re.I)
ok("в окне нет поля ввода")
assert "Спросить" not in body and "textSend" not in body
ok("кнопки «Спросить» нет")
assert not re.search(r"\btextSend\b|getElementById\(.cht.\)|\$\(.#cht.\)", html)
ok("обработчик текстового ввода удалён из мини-аппа")
assert not re.search(r"\.chs\s+\.chi\b", html)
ok("стили строки ввода удалены")

btns = re.findall(r"<button\b[^>]*>", body)
assert len(btns) == 1 and 'id="micb"' in btns[0], btns
ok("в разметке окна ровно одна кнопка — микрофон (✅/❌ подтверждения появляются только после команды)")
print("OK")
