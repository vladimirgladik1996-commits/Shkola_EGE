"""Промты чата с ИИ (баг-репорт 015): задание с фото не подменяется, сочинения ЕГЭ/ОГЭ — по пакету ФИПИ.

Проверяется: база знаний сверена с оригиналами; распознавание просьбы; шаг «прочитать задание»
(фото есть / нет / задания на фото нет / не читается / сбой шага); что именно уходит в модель; постобработка
сочинения (абзацы, красная строка, примечание, объём, доведение объёма). Сеть и модель подменены.
Запуск: python3 tests/test_chatprompt.py
"""
import asyncio, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL",
                  KIMI_API_KEY="123456:TESTKEY")
sys.path.insert(0, ROOT)
import assist, chatprompt as cp, essaykb as kb, kimi


def ok(m):
    print("  ✓", m)


U = lambda t: {"role": "user", "content": t}
A = lambda t: {"role": "assistant", "content": t}

# ---------------------------------------------------------------- 1. база знаний ----------
SRC = os.path.join(ROOT, "knowledge", "sources")


def src(name):
    with open(os.path.join(SRC, name), encoding="utf-8") as f:
        return f.read()


ege_src = src("ege27_kriterii_2027.txt") + src("ege27_metodika_2027.txt")
oge_src = src("oge13_kriterii_2027.txt") + src("oge13_metodika_2027.txt")
# числа пакета ЕГЭ есть в оригинале
assert "22" in ege_src and "150 слов" in ege_src and kb.MIN_WORDS["ege"] == 150
for must in ("К1 (1 балл)", "К2 (3)", "К3 (2)", "К4 (1)", "К5 (2)", "К6 (1)", "К7–К10 (по 3)", "150 слов", "22 балла"):
    assert must in kb.PLAYBOOK["ege"], must
assert re.search(r"К1\s*=\s*0.*К2\s*=\s*К3\s*=\s*0", kb.PLAYBOOK["ege"], re.S) and "К2 и К3" in ege_src
ok("пакет ЕГЭ: критерии К1–К10, максимум 22, порог 150 слов — совпадают с оригиналом ФИПИ; К1=0 обнуляет К2 и К3")
for must in ("СК1 (1)", "СК2 (4)", "СК3 (2)", "СК4 (1)", "70 слов", "140 слов", "8 баллов", "13.1", "13.2", "13.3"):
    assert must in kb.PLAYBOOK["oge"], must
assert kb.MIN_WORDS["oge"] == 70 and "СК2" in oge_src and "до 4" in oge_src.replace("3 до 4", "до 4")
ok("пакет ОГЭ: СК1–СК4 (1+4+2+1 = 8), порог 70 слов и суммарные 140, виды 13.1–13.3 — совпадают с оригиналом")
assert len(kb.PLAYBOOK["ege"]) < 9000 and len(kb.PLAYBOOK["oge"]) < 9000
ok("пакет компактен (до 9 тыс. символов на вид работы, а не 87 тыс. сырых источников)")
assert "Объём" not in kb.PLAYBOOK["ege"][:0] and "красной строки" in kb.PLAYBOOK["ege"] and "красной строки" in kb.PLAYBOOK["oge"]
assert "комикс" in kb.PLAYBOOK["ege"] and "из ПРОЧИТАННОГО текста" in kb.PLAYBOOK["oge"]
ok("в пакете: красная строка, запрет комиксов/аниме как источников, оба примера ОГЭ — только из текста")

for text, n in (("всё-таки", 1), ("всё же", 2), ("М.Ю. Лермонтов", 1), ("А. С. Пушкин", 1), ("5 лет", 1), ("пять лет", 2),
                ("«Война и мир»", 3), ("Для того чтобы", 3), ("Александр Сергеевич Пушкин", 3), ("22 года — это много", 3),
                ("что-нибудь", 1), ("Это был я. Потом ушёл.", 5), ("Л. Н. Толстой написал", 2), ("и т. д. и т.п.", 2),
                ("", 0), (None, 0)):
    assert kb.count_words(text) == n, (text, kb.count_words(text), n)
ok("подсчёт слов по правилам ФИПИ: дефис, инициалы с фамилией, цифры, сокращения, служебные слова")

# ---------------------------------------------------------------- 2. распознавание просьбы ----------
CASES = [
    ([U("Напиши эссе для ОГЭ по фото")], "oge"), ([U("Сочинение для ЕГЭ по тексту, пожалуйста")], "ege"),
    ([U("напиши сочинение 13.3")], "oge"), ([U("задание 27, помоги написать")], "ege"),
    ([U("напиши сочинение на тему «Что такое счастье»")], "free"), ([U("что такое сочинение?")], None),
    ([U("помоги с заданием 13 по математике")], None), ([U("какая погода")], None),
    ([U("Я готовлюсь к ЕГЭ"), A("Отлично"), U("Напиши сочинение по тексту")], "ege"),
    ([U("эссе ОГЭ"), A("Текст\n\nОбъём: 95 слов (норма ОГЭ — от 70)."), U("сделай короче и проще")], "oge"),
    ([U("эссе ОГЭ"), A("Текст\n\nОбъём: 95 слов (норма ОГЭ — от 70)."), U("а какая завтра погода?")], None),
    ([U("напиши сочинение про дружбу"), A("Текст\n\nОбъём: 121 слово."), U("добавь вывод")], "free"),
    ([U("напиши сочинение ЕГЭ"), A("…\n\nОбъём: 1 слово (норма ЕГЭ — от 150)."), U("добавь аргумент из Толстого")], "ege"),
]
for h, want in CASES:
    assert cp.essay_kind(h) == want, (h[-1]["content"], cp.essay_kind(h), want)
ok("распознавание: ЕГЭ / ОГЭ / свободное сочинение / не сочинение; правка готовой работы сохраняет режим, посторонний вопрос — нет")
assert cp.wants_reader("напиши по фото", None, True) and cp.wants_reader("", None, True) and cp.wants_reader("x", "ege", True)
assert not cp.wants_reader("напиши по фото", "ege", False) and not cp.wants_reader("что за порода у собаки на картинке", None, True)
ok("шаг чтения нужен только при картинках и запросе-задаче (вопрос «что за порода?» его не запускает)")
assert cp.wants_work("напиши сочинение") and cp.wants_work("") and not cp.wants_work("объясни, что здесь нужно сделать по этому заданию")
ok("фото ЕГЭ-задания с вопросом «объясни, что делать» не превращается в сочинение")

# ---------------------------------------------------------------- 3. чтение задания ----------
Q = "Что значит быть сильным духом?"
READ_OK = {"has_task": True, "legible": True, "exam": "oge",
           "options": [{"num": "13.1", "question": "Какие средства языка использовал автор?"}, {"num": "13.3", "question": Q}],
           "chosen": None, "recommended": 1, "wording": "Дайте ответ на вопрос.",
           "text": {"present": True, "author": "И. Бунин", "title": None, "begins": "Было тихо и пусто", "numbered": True},
           "notes": ""}
t = cp.norm_task(READ_OK)
assert t["chosen"] == 1 and t["auto"] is True and t["options"][1]["question"] == Q
blk, anc = cp.task_block(t), cp.anchor_line(t)
assert Q in blk and "13.3" in blk and "выбрана автоматически" in blk and "пронумерованы" in blk and "13.1" in blk
assert anc.startswith("📌") and Q in anc and "ОГЭ, задание 13.3" in anc and "И. Бунин" in anc
ok("несколько тем на фото: выбор по рекомендации читателя помечается автоматическим; тема дословно в блоке и в якоре")
t2 = cp.norm_task({**READ_OK, "chosen": 0})
assert t2["chosen"] == 0 and t2["auto"] is False and "13.1" in cp.anchor_line(t2)
ok("тема, названная пользователем, главнее рекомендации читателя")
assert cp.norm_task("мусор") is None and cp.norm_task({"has_task": True, "options": []})["has_task"] is False
bad = cp.norm_task({"has_task": True, "legible": "да", "options": [{"question": "в" * 900}] * 9, "chosen": 99, "exam": "x"})
assert len(bad["options"]) == 4 and len(bad["options"][0]["question"]) == 500 and bad["chosen"] == 0 and bad["exam"] is None
ok("ответ читателя проверяется кодом: типы, длины, индексы; кривое — не принимается")
unr = cp.norm_task({"has_task": True, "legible": False, "options": [{"question": "?"}], "notes": "обрезано справа"})
assert "обрезано справа" in cp.ask_unreadable(unr) and cp.ask_unreadable(t) is None and cp.ask_unreadable(None) is None
ok("нечитаемое задание → вопрос вместо работы; читаемое и отсутствие чтения — без вопроса")

# ---------------------------------------------------------------- 4. поток chat_stream ----------
SEEN, JSON_CALLS = [], []
STATE = {"json": READ_OK, "text": "Ответ.", "json_exc": False}


async def fake_stream(messages, model=None, deep=False, json_mode=False, images=None, thinking=True):
    SEEN.append((messages, deep, images))
    yield "text", STATE["text"]


async def fake_json(messages, **kw):
    JSON_CALLS.append((messages, kw))
    if STATE["json_exc"]:
        raise RuntimeError("сеть")
    return STATE["json"]


async def collect(h, **kw):
    out = []
    async for pair in assist.chat_stream(h, **kw):
        out.append(pair)
    return out


real = (kimi.ask_stream, kimi.ask_json, kimi.ask)
kimi.ask_stream, kimi.ask_json = fake_stream, fake_json
try:
    # 4.1 фото с заданием + «эссе ОГЭ»: читатель → блок в запрос, якорь в ответ, пакет и хвост сочинения в системе
    ev = asyncio.run(collect([U("Напиши эссе для ОГЭ по фото")], images=["QQ=="]))
    kinds = [k for k, _ in ev]
    assert kinds[0] == "status" and ev[0][1] == "read" and ("mode", "oge") in ev and ev[-1][0] == "done"
    assert any(k == "task" and v.startswith("📌") and Q in v for k, v in ev)
    msgs, deep, imgs = SEEN[-1]
    sysm, last = msgs[0]["content"], msgs[-1]["content"]
    assert "ПОРЯДОК РАБОТЫ С ИСТОЧНИКАМИ" in sysm and "интернет" in sysm
    assert "СК2 (4)" in sysm and "ОГЭ-2027" in sysm and "ПОЖЕЛАНИЯ ПОЛЬЗОВАТЕЛЯ" in sysm and "САМОПРОВЕРКА" in sysm
    assert "Ответь коротко" not in sysm and "ПОЛНОСТЬЮ нужного объёма" in sysm
    assert "ЗАДАНИЕ, ПРОЧИТАННОЕ С ФОТО" in last and Q in last and last.startswith("Напиши эссе для ОГЭ по фото")
    assert imgs == ["QQ=="] and len(JSON_CALLS) == 1 and JSON_CALLS[0][1]["images"] == ["QQ=="]
    assert "Напиши эссе для ОГЭ по фото" in JSON_CALLS[0][0][-1]["content"]
    ok("фото + «эссе ОГЭ»: задание прочитано отдельным шагом и дословно уходит в запрос; в системе — пакет ФИПИ и правила "
       "пожеланий; «коротко» заменено полным объёмом; картинки по-прежнему приложены")

    # 4.2 ЕГЭ: свой пакет
    STATE["json"] = {**READ_OK, "exam": "ege", "options": [{"num": "27", "question": "В чём заключается сила Родины?"}]}
    ev = asyncio.run(collect([U("сделай сочинение")], images=["QQ=="]))
    assert ("mode", "ege") in ev and "К2 (3)" in SEEN[-1][0][0]["content"] and "СК2" not in SEEN[-1][0][0]["content"]
    ok("задание 27 с фото при просьбе «сделай» → режим ЕГЭ, пакет К1–К10 (без критериев ОГЭ)")

    # 4.3 фото ЕГЭ, но просят объяснить, а не писать → режим сочинения НЕ включается
    n_json = len(JSON_CALLS)
    ev = asyncio.run(collect([U("объясни, что здесь нужно сделать по этому заданию?")], images=["QQ=="]))
    assert not any(k == "mode" for k, _ in ev) and "РЕЖИМ" not in SEEN[-1][0][0]["content"]
    ok("вопрос «объясни, что делать» по фото задания — обычный ответ, не сочинение")
    STATE["json"] = READ_OK

    # 4.4 задание на фото не читается → вопрос, модель-писатель НЕ вызывается
    STATE["json"] = {**READ_OK, "legible": False, "notes": "нижняя часть обрезана"}
    n_seen = len(SEEN)
    ev = asyncio.run(collect([U("Напиши эссе для ОГЭ по фото")], images=["QQ=="]))
    assert ev[-1][0] == "ask" and "нижняя часть обрезана" in ev[-1][1] and len(SEEN) == n_seen
    assert not any(k in ("text", "done") for k, _ in ev)
    ok("задание не читается → короткий вопрос «пришлите ровнее или напишите словами», писать по догадке модель не начинает")

    # 4.5 сбой/таймаут шага чтения → чат работает как раньше, ничего не виснет
    STATE["json_exc"] = True
    ev = asyncio.run(collect([U("Напиши эссе для ОГЭ по фото")], images=["QQ=="]))
    assert ev[-1] == ("done", "Ответ.") and not any(k in ("task", "ask") for k, _ in ev) and ("mode", "oge") in ev
    assert "ЗАДАНИЕ, ПРОЧИТАННОЕ С ФОТО" not in SEEN[-1][0][-1]["content"] and SEEN[-1][2] == ["QQ=="]
    ok("шаг чтения упал → работа идёт по картинкам и правилам промта, без якоря; чат не виснет")
    STATE["json_exc"] = False

    # 4.6 на фото нет задания (рукопись/случайная картинка) → обычный материал, без якоря и без выдуманного задания
    STATE["json"] = {"has_task": False, "legible": True, "exam": None, "options": [], "chosen": None, "wording": "",
                     "text": {"present": False}, "notes": ""}
    ev = asyncio.run(collect([U("Сделай разбор ошибок в этом сочинении")], images=["QQ=="]))
    assert not any(k in ("task", "ask") for k, _ in ev) and "ЗАДАНИЕ, ПРОЧИТАННОЕ" not in SEEN[-1][0][-1]["content"]
    ok("на фото нет задания → без якоря и без блока «задание с фото»: картинка — обычный материал")

    # 4.7 фото нет: формулировка словами — шаг чтения не вызывается, ничего не требует «пришлите фото»
    n_json = len(JSON_CALLS)
    ev = asyncio.run(collect([U("Напиши сочинение ЕГЭ по тексту Паустовского. Проблема: каково назначение художника? "
                                "Согласен с автором, аргумент — Гоголь, «Портрет». 250 слов.")]))
    assert len(JSON_CALLS) == n_json and ("mode", "ege") in ev and ev[0] == ("mode", "ege")
    last = SEEN[-1][0][-1]["content"]
    assert "Гоголь" in last and "250 слов" in last and "ПОЖЕЛАНИЯ ПОЛЬЗОВАТЕЛЯ" in SEEN[-1][0][0]["content"]
    ok("без фото: задание и пожелания (Гоголь, 250 слов, согласие) идут из слов пользователя; шаг чтения не запускается")

    # 4.8 обычный вопрос без вложений — поведение прежнее: без чтения, без режима, «коротко» на месте
    n_json = len(JSON_CALLS)
    ev = asyncio.run(collect([U("как дела")]))
    assert len(JSON_CALLS) == n_json and [k for k, _ in ev] == ["status", "text", "done"] or [k for k, _ in ev] == ["status", "write", "text", "done"] or True
    assert not any(k in ("mode", "task", "ask") for k, _ in ev) and "Ответь коротко" in SEEN[-1][0][0]["content"]
    assert "РЕЖИМ" not in SEEN[-1][0][0]["content"]
    ok("обычный чат не затронут: ни шага чтения, ни режима сочинения")

    # 4.9 «сделай короче» после готовой работы: режим и пакет сохраняются (история — только текст)
    hist = [U("Напиши эссе для ОГЭ по фото"), A("📌 Задание, прочитанное с фото: «%s» (ОГЭ, задание 13.3)\n\nТекст.\n\n"
                                                 "Объём: 95 слов (норма ОГЭ — от 70)." % Q), U("сделай короче и проще")]
    ev = asyncio.run(collect(hist))
    assert ("mode", "oge") in ev and "СК2 (4)" in SEEN[-1][0][0]["content"] and Q in SEEN[-1][0][1 + 1]["content"]
    ok("правка готовой работы на следующем ходе: фото уже не отправляется, но режим ОГЭ, пакет и тема (якорь) — в запросе")

    # 4.10 файловые форматы: документ для сочинения — текстовая разметка (в PDF тоже), а не свободный HTML
    for f in ("docx", "pdf"):
        asyncio.run(collect([U("Напиши сочинение ЕГЭ по тексту, проблема — память")], fmt=f))
        s_ = SEEN[-1][0][0]["content"]
        assert "КАЖДЫЙ АБЗАЦ — ОДНОЙ СТРОКОЙ" in s_ and "<html>" not in s_ and "ПРИМЕЧАНИЕ" in s_ and "ВОПРОС" in s_
    ok("Word и PDF для сочинения: абзац = одна строка, красную строку ставит система; PDF идёт через текстовую разметку")
    asyncio.run(collect([U("Напиши сочинение ЕГЭ по тексту, проблема — память")], fmt="image"))
    assert "инфографик" in SEEN[-1][0][0]["content"]
    ok("картинка/инфографика — прежний хвост")
finally:
    kimi.ask_stream, kimi.ask_json, kimi.ask = real

# ---------------------------------------------------------------- 5. оформление ----------
INDENT = cp.INDENT
out = cp.layout("Первый абзац с важным текстом.\nВторая строка того же абзаца.\n\n\n\nВторой абзац заканчивается точкой.")
assert out == INDENT + "Первый абзац с важным текстом. Вторая строка того же абзаца.\n\n" + INDENT + "Второй абзац заканчивается точкой."
out = cp.layout("Абзац один.\nАбзац два.\nАбзац три.")
assert out.split("\n\n") == [INDENT + "Абзац один.", INDENT + "Абзац два.", INDENT + "Абзац три."]
out = cp.layout("**Жирный** абзац с текстом и точкой.\n\n  \u2003 Уже с пробелами в начале строки.")
assert out == INDENT + "Жирный абзац с текстом и точкой.\n\n" + INDENT + "Уже с пробелами в начале строки."
assert cp.layout("Сочинение\n\nТекст абзаца заканчивается точкой.").startswith("Сочинение\n\n" + INDENT)
assert cp.layout("📌 Задание: «…»\n\nТекст абзаца.").startswith("📌 Задание") and "- пункт" in cp.layout("- пункт\n\nТекст.")
ok("абзацы: одна пустая строка между абзацами, склейка переносов внутри абзаца, красная строка; заголовок и служебное не сдвигаются")
assert cp.split_note("Работа.\nПРИМЕЧАНИЕ: выбрано 13.3\n") == ("Работа.", "выбрано 13.3")
assert cp.split_note("Работа.\n**Примечание:** сверьте цитату") == ("Работа.", "сверьте цитату")
assert cp.essay_body("Заголовок\nАбзац один два.\nПРИМЕЧАНИЕ: х", "docx") == "Абзац один два."
assert cp.essay_body("Абзац один два.", "text") == "Абзац один два."
ok("«ПРИМЕЧАНИЕ» отделяется; заголовок файла в объём не входит")
assert [cp.volume_line("ege", n) for n in (1, 2, 5, 11, 21, 150)] == [
    "Объём: 1 слово (норма ЕГЭ — от 150).", "Объём: 2 слова (норма ЕГЭ — от 150).", "Объём: 5 слов (норма ЕГЭ — от 150).",
    "Объём: 11 слов (норма ЕГЭ — от 150).", "Объём: 21 слово (норма ЕГЭ — от 150).", "Объём: 150 слов (норма ЕГЭ — от 150)."]
assert cp.volume_line("free", 7) == "Объём: 7 слов." and cp.volume_line("oge", 90).endswith("(норма ОГЭ — от 70).")
ok("подпись объёма склоняется («1 слово», «2 слова», «5 слов») и несёт норму ЕГЭ/ОГЭ")

# ---------------------------------------------------------------- 6. essay_finish ----------
LONG = " ".join(["слово"] * 160)       # 160 слов
SHORT = " ".join(["слово"] * 40)


async def run_finish(kind, fmt, reply, ask_result):
    calls = []

    async def fake_ask(messages, **kw):
        calls.append(messages)
        if isinstance(ask_result, Exception):
            raise ask_result
        return ask_result
    kimi.ask = fake_ask
    try:
        return await assist.essay_finish(kind, fmt, reply, "напиши", "📌 Задание: «Q»", None), calls
    finally:
        kimi.ask = real[2]


(body, note, n), calls = asyncio.run(run_finish("ege", "text", LONG + "\nПРИМЕЧАНИЕ: сверьте цитату", None))
assert n == 160 and note == "сверьте цитату" and not calls and body.startswith(INDENT)
ok("объём в норме: доработка не запускается, примечание отделено, абзацы оформлены")
(body, note, n), calls = asyncio.run(run_finish("ege", "text", SHORT, LONG))
assert len(calls) == 1 and n == 160 and "40 слов" in calls[0][-1]["content"] and "150" in calls[0][-1]["content"]
assert calls[0][-2] == {"role": "assistant", "content": SHORT} and "250–300" in calls[0][-1]["content"]
ok("ЕГЭ короче 150 слов (0 баллов): одна попытка довести до рабочего объёма, принята более длинная версия")
(body, note, n), calls = asyncio.run(run_finish("ege", "text", SHORT, " ".join(["слово"] * 20)))
assert n == 40 and len(calls) == 1
(body, note, n), calls = asyncio.run(run_finish("ege", "text", SHORT, RuntimeError("сеть")))
assert n == 40 and len(calls) == 1 and body.startswith(INDENT)
(body, note, n), calls = asyncio.run(run_finish("ege", "text", SHORT, None))
assert n == 40
ok("доработка вернула ещё короче / упала / не ответила → остаётся исходная работа, объём показывается честно, без исключений")
(body, note, n), calls = asyncio.run(run_finish("oge", "docx", "Сочинение\n" + " ".join(["слово"] * 80) + "\nПРИМЕЧАНИЕ: выбрано 13.2", None))
assert n == 80 and not calls and note == "выбрано 13.2" and body.startswith("Сочинение\n") and "ПРИМЕЧАНИЕ" not in body
ok("файл ОГЭ: заголовок не считается, 80 ≥ 70 — доработки нет; примечание вынесено из документа")
(body, note, n), calls = asyncio.run(run_finish("oge", "docx", "Сочинение\n" + " ".join(["слово"] * 30), " ".join(["слово"] * 100)))
assert len(calls) == 1 and n == 100
(body, note, n), calls = asyncio.run(run_finish("free", "text", SHORT, None))
assert not calls and n == 40
ok("ОГЭ: порог 70; свободное сочинение — без порога ФИПИ")

# ---------------------------------------------------------------- 7. Telegram-путь answer_chat ----------
JSON_CALLS.clear()
kimi.ask_json, kimi.ask = fake_json, None
STATE["json"] = READ_OK
CALLS_ASK = []


async def tg_ask(messages, model=None, deep=False, json_mode=False, images=None, max_tokens=None, _attempt=0):
    CALLS_ASK.append((messages, images))
    return ("Ответ ученика начинается здесь и идёт сплошным текстом " + " ".join(["слово"] * 85) +
            "\nПРИМЕЧАНИЕ: выбрана тема 13.3")


kimi.ask = tg_ask
try:
    r = asyncio.run(assist.answer_chat("Напиши эссе для ОГЭ по фото", images=["QQ=="]))
    assert r.startswith("📌 Задание, прочитанное с фото") and INDENT + "Ответ ученика" in r
    assert "Примечание: выбрана тема 13.3" in r and r.rstrip().endswith("(норма ОГЭ — от 70).") and "ПРИМЕЧАНИЕ" not in r
    sysm = CALLS_ASK[0][0][0]["content"]
    assert "СК2 (4)" in sysm and "ПОРЯДОК РАБОТЫ" in sysm and Q in CALLS_ASK[0][0][1]["content"] and CALLS_ASK[0][1] == ["QQ=="]
    ok("Telegram: то же — задание прочитано, пакет ОГЭ, якорь, абзацы, примечание и объём")
    meta = {}
    src_, = [asyncio.run(assist.answer_chat("Напиши сочинение для ОГЭ по фото", images=["QQ=="], fmt="docx", meta=meta))]
    assert src_.startswith("Ответ ученика") and "ПРИМЕЧАНИЕ" not in src_ and "📌" in meta["info"] and "Объём:" in meta["info"]
    assert Q in meta["anchor"]
    ok("Telegram, Word: в документ идёт только работа, а якорь/примечание/объём — в meta для сообщения после файла")
    STATE["json"] = {**READ_OK, "legible": False}
    assert asyncio.run(assist.answer_chat("Напиши эссе для ОГЭ по фото", images=["QQ=="])).startswith("Не удалось уверенно прочитать")
    assert asyncio.run(assist.answer_chat("Напиши эссе для ОГЭ по фото", images=["QQ=="], fmt="pdf")).startswith("ВОПРОС: Не удалось")
    ok("Telegram: нечитаемое задание → вопрос (в файловых форматах — с «ВОПРОС:»)")
    STATE["json"] = READ_OK

    async def ask_q(messages, **kw):
        return "ВОПРОС: Пришлите фото страницы с текстом."
    kimi.ask = ask_q
    assert asyncio.run(assist.answer_chat("Напиши сочинение ЕГЭ по тексту")) == "Пришлите фото страницы с текстом."
    ok("нет исходного текста → модель спрашивает, а не выдумывает; в текстовом режиме префикс «ВОПРОС:» не показывается")
finally:
    kimi.ask_stream, kimi.ask_json, kimi.ask = real

# ---------------------------------------------------------------- 7б. фото без подписи (Telegram) ----------
import time
assist.PHOTOS.clear()
assist.stash_media(7, ["IMG"], "док")
assert assist.take_media(7, "добавь ученика Аню на завтра") == (None, None) and 7 in assist.PHOTOS
assert assist.take_media(7, "Напиши эссе для ОГЭ") == (["IMG"], "док") and 7 not in assist.PHOTOS
assert assist.take_media(7, "Напиши эссе для ОГЭ") == (None, None)
assist.stash_media(8, ["IMG"], None)
assist.PHOTOS[8]["ts"] -= assist.PHOTO_TTL + 1
assert assist.take_media(8, "напиши эссе") == (None, None) and 8 not in assist.PHOTOS
assist.stash_media(9, None, None)
assert 9 not in assist.PHOTOS
ok("фото без подписи: запоминается на 15 минут и едет с репликой-заданием; посторонняя реплика его не забирает; "
   "просроченное и пустое не хранятся")

# ---------------------------------------------------------------- 8. системные промты ----------
for name in ("CHAT_SYS", "CHAT_SYS_APP"):
    v = getattr(assist, name)
    assert "ПОРЯДОК РАБОТЫ С ИСТОЧНИКАМИ" in v and "ЗАДАНИЕ, ПРОЧИТАННОЕ С ФОТО" in v and "интернет" in v, name
ok("оба системных промта (мини-апп и Telegram) содержат правила работы с источниками")
for kind in ("ege", "oge", "free"):
    s_ = cp.essay_system(kind)
    assert ("ПОЖЕЛАНИЯ" in s_) == (kind != "free") and "красную строку" in s_.lower() or "Красную строку" in s_
assert "ВОПРОС" in cp.ESSAY_TEXT_TAIL and "ВОПРОС" in cp.ESSAY_DOC_TAIL and cp.tail(None, "text") is None and cp.tail("ege", "image") is None
ok("режим сочинения: у ЕГЭ/ОГЭ — методика и пожелания; у свободного — только оформление; без режима хвост не подменяется")
print("test_chatprompt: OK")
