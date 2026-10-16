"""Виджет Вишенки (голосовой помощник): в окне только голосовое управление.
1. Строка ввода и кнопка «Спросить» — баг; тест не даёт им вернуться.
2. Кнопка голосовой команды анимированно превращается в «✅ Применить» и «❌ Отмена»,
   а сама остаётся кнопкой «Поправить распознанное» под ними — ею можно озвучить корректировку.
3. Расшифровка команды показывается отдельной строкой НАД кнопками — и до, и после превращения."""
import os, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
def ok(c): print("  ✓", c)

m = re.search(r"function chatOpen\(\)\{(.*?)\n/\*", html, re.S)   # тело самой функции; следующий за ней комментарий — уже про другое окно («Спросить у Вишенки»)
assert m, "chatOpen() не найден"
body = m.group(1)

# --- только голос -----------------------------------------------------------
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

# --- одна зона действий: микрофон + «Применить» + «Отмена» ---------------------
act = re.search(r'<div class="act" id="cha">(.*?)</div>', body, re.S)
assert act, "нет контейнера #cha"
btns = re.findall(r"<button\b[^>]*>", act.group(1))
assert len(btns) == 3 and 'id="micb"' in btns[0] and 'id="cok"' in btns[1] and 'id="cno"' in btns[2], btns
assert len(re.findall(r"<button\b", body)) == 3
ok("в окне ровно три кнопки в одном контейнере: микрофон, «Применить», «Отмена»")
assert all("inert" in b for b in btns[1:]) and "inert" not in btns[0]
ok("до команды «Применить»/«Отмена» скрыты и недоступны с клавиатуры (inert)")
assert "Применить" in act.group(1) and "Отмена" in act.group(1)
ok("подписи «✅ Применить» и «❌ Отмена» на месте")

# --- превращение анимированное ---------------------------------------------------
assert re.search(r"\.chs \.act button\{[^}]*transition:[^}]*flex-grow", html)
assert re.search(r"\.chs \.act\.cf \.mic\{[^}]*opacity:1", html) and re.search(r"\.chs \.act\.cf \.ok,\.chs \.act\.cf \.no\{[^}]*opacity:1", html)
ok("переход микрофон → две кнопки: плавное изменение ширины и прозрачности (класс .cf)")
assert re.search(r"\.chs \.act\.cf \.mic\{[^}]*flex:1 1 100%", html) and re.search(r"\.chs \.act\.cf \.mic\{[^}]*order:3", html)
ok("под «Применить»/«Отмена» кнопка микрофона остаётся — отдельной строкой внизу")
assert "prefers-reduced-motion:reduce" in html and re.search(r"reduce\)\{[^\n]*\.chs \.act button\{transition:none", html)
ok("при «уменьшить анимацию» превращение мгновенное")
cb = re.search(r"function chrBtns\(confirm\)\{(.*?)\n(?=function chrHeard)", html, re.S).group(1)
assert 'classList.toggle("cf"' in cb and "inert" in cb
ok("chrBtns переключает .cf и inert (кнопки меняются местами в обе стороны)")
assert '["micb",confirm]' not in cb and "micLabel" in cb
ok("в состоянии подтверждения микрофон не гасится — им можно поправить распознанное")
ml = re.search(r"function micLabel\(rec\)\{(.*?)\n(?=function micUi)", html, re.S).group(1)
assert "Поправить распознанное" in ml and "Сказать, что сделать" in ml and "Голосовая команда" in ml
ok("подпись микрофона меняется по состоянию: команда → поправка → задача фрагмента")

# --- расшифровка над кнопками ------------------------------------------------------
z = re.search(r'<div class="zone">(.*?)</div>\s*<div class="act"', body, re.S)
assert z and 'id="eq"' in z.group(1) and 'id="chh"' in z.group(1) and 'id="chr"' in z.group(1)
assert body.index('class="zone"') < body.index('id="cha"') and body.index('id="chr"') < body.index('id="cha"')
ok("порядок в окне: [эквалайзер | расшифровка | ответ] → кнопки; под кнопками текста нет — ни до, ни после превращения")
assert re.search(r"\.chs \.eq\{[^}]*position:absolute[^}]*inset:0", html)
ok("расшифровка занимает место эквалайзера: он лежит в той же зоне .zone поверх неё и гаснет, когда запись остановлена")
assert re.search(r"function voiceReply\(r\)\{[^}]*chrHeard\(r&&r\.heard", html)
ok("после ответа сервера расшифровка (heard) выводится в строку над кнопками")
assert re.search(r"async function voiceToggle\(\)\{.{0,200}?chrHeard\(\"\"\)", html, re.S)
ok("при новой записи старая расшифровка очищается")

# --- «Применить»: виджет сворачивается, на странице играет анимация действия ----------------
vc_ = re.search(r"async function voiceConfirm\(\)\{(.*?)\n(?=/\* «Выполнить»)", html, re.S).group(1)
assert "voice_confirm" in vc_ and "voiceDone(r)" in vc_ and "voiceFold" not in vc_.split("voiceDone")[0]
ok("после ответа сервера «Применить» передаёт результат в voiceDone (окно не закрывается раньше времени)")
assert re.search(r"if\(!\(r&&r\.ok\)\)\{chrSay\(", vc_)
ok("при ошибке окно остаётся открытым и показывает причину")
vf = re.search(r"async function voiceFold\(\)\{(.*?)\n(?=function voiceWeek)", html, re.S).group(1)
assert "REDUCE" in vf and ".animate(" in vf and vf.rstrip().endswith("close_()}")
ok("voiceFold: окно плавно опускается и гаснет, потом закрывается (без анимации при «уменьшить анимацию»)")
vd = re.search(r"async function voiceDone\(r\)\{(.*?)\n(?=function |async function |/\*)", html, re.S).group(1)
assert re.search(r'add_lesson.*?foldInto\(i\.day', vd, re.S) and "move_lesson" in vd
ok("занятие добавлено/перенесено → окно «летит» в ячейку расписания (foldInto)")
assert re.search(r'a=="add_payment".*?tab="p".*?fresh=i\.pid.*?payCard\(i\.pid', vd, re.S)
ok("оплата → вкладка «Оплаты», новая запись вспыхивает в ленте (fresh), затем карточка распределения")
assert re.search(r'a=="cancel_lesson".*?\.animate\(', vd, re.S) and re.search(r'a=="add_student".*?classList\.add\("pop"\)', vd, re.S)
ok("отмена занятия — ячейка гаснет; новый ученик — вспыхивает в списке")
assert "voiceFold()" in vd and vd.rstrip().endswith("await voiceFold();await load()}")
ok("для остальных действий — сворачивание виджета и обновление данных")
# --- прогресс обработки: голос ушёл → сервер думает → ответ ---------------------------------------
assert html.count('id="chp"') == 2, "прогресс-бар должен быть в обоих окнах: команда и «Спросить у Вишенки»"
zp = re.search(r'<div class="zone">.*?id="chp".*?</div>\s*<div class="act"', body, re.S)
assert zp, "прогресс-бар лежит не в зоне статуса — над кнопками" 
ok("прогресс-бар в зоне статуса обоих окон")
pg = re.search(r"function chrProg\(s\)\{(.*?)\n(?=function |let |var |const |async )", html, re.S)
assert pg and '"#chp"' in pg.group(1) and "done" in pg.group(1), "нет chrProg — полосу некем включать и доводить до конца"
vs = re.search(r"async function voiceSend\(blob,ctx,dur\)\{(.*?)\n(?=function |async function |window\.|document\.|/\*)", html, re.S).group(1)
assert re.search(r"chrProg\(1\).*?api\(", vs, re.S), "voiceSend: полоса должна стартовать ДО запроса к серверу"
assert re.search(r"api\(.*?chrProg\(", vs, re.S) and re.search(r"chrProg\(r\?2:0\)", vs), \
    "voiceSend: полоса живёт до ответа сервера — до конца при успехе, гаснет при сбое"
vc_ = re.search(r"async function voiceConfirm\(\)\{(.*?)\n(?=/\* «Выполнить»)", html, re.S).group(1)
assert "chrProg(1)" in vc_ and "chrProg(2)" in vc_ and "chrProg(0)" in vc_, "у «Применить» тоже есть прогресс обработки"
assert re.search(r"async function voiceToggle\(\)\{.{0,200}?chrProg\(0\)", html, re.S), \
    "новая запись должна гасить прогресс предыдущей обработки"
assert re.search(r"\.chs \.pr\{", html) and "prFill" in html and "prDone" in html and "prShine" in html, \
    "стили полосы: рост, доведение до 100% и живой блик"
assert re.search(r"prefers-reduced-motion:reduce\)\{\.chs \.pr", html), \
    "при «уменьшить анимацию» блик должен выключаться, рост полосы — оставаться"
ok("полоса идёт от «Отправляю…» до ответа, у «Применить» — своя; новая запись гасит старую")
print("OK")
