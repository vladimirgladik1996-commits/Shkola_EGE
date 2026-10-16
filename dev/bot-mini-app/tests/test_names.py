"""Имена из голоса: сопоставление с базой и канонизация новых имён.

Whisper искажает имена («Анечка», «Иванов Ив», «Маринаа») — код обязан подбирать ближайшее
имя базы (включая уменьшительно-ласкательные формы) и приводить имена новых учеников
к полным русским формам (Ваня → Иван). Плюс контракт промтов: разборщику сказано,
что текст распознан голосом, и как подбирать имена.
Запуск: python3 tests/test_names.py
"""
import json, os, re, sqlite3, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID", "KIMI_API_KEY", "KIMI_BASE_URL", "KIMI_MODEL",
          "WHISPER_API_KEY", "WHISPER_BASE_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import assist, bot, whisper
def ok(c): print("  ✓", c)

mem = sqlite3.connect(":memory:", check_same_thread=False); mem.row_factory = sqlite3.Row
bot.init_db(mem); bot.db = mem
mem.execute("insert into students(name,price,grade,subject) values('Анна Иванова',1500,'11','математика')")
mem.execute("insert into students(name,price,grade,subject) values('Марина Соколова',1000,'9','физика')")
mem.commit()

# --- имя нового ученика: как продиктовано, без разворачивания кратких форм ------------
assert assist.canon_name("Ваня Петров") == "Ваня Петров"        # краткое остаётся кратким
assert assist.canon_name("света кузнецова") == "Света Кузнецова"
assert assist.canon_name("Катя") == "Катя"                      # без «Екатерина»
assert assist.canon_name("Серёжа Волков") == "Серёжа Волков"
assert assist.canon_name("А. С. Пушкин") == "А. С. Пушкин"       # инициалы не трогаем
assert assist.canon_name("Марина Соколова") == "Марина Соколова"
ok("имя нового ученика записывается как продиктовано: краткое — кратким, меняется только регистр")

# --- подбор имени по базе: точное, мягкое, изуродованное голосом --------------
assert assist.find_student("Анна")["name"] == "Анна Иванова"
assert assist.find_student("анна иванова")["name"] == "Анна Иванова"
assert assist.find_student("Анечка")["name"] == "Анна Иванова"       # уменьшительная форма
assert assist.find_student("Марина Соколоваа")["name"] == "Марина Соколова"   # лишняя буква от распознавания
assert assist.find_student(mem.execute("select id from students where name='Марина Соколова'").fetchone()[0])["name"] == "Марина Соколова"
ok("имя из голоса подобрано к базе: уменьшительная форма и опечатка распознавания")

# --- неясное имя → честный переспрос, а не выдуманный ученик ------------------
try:
    assist.find_student("Борис")
    assert False, "должен был бросить AssistError"
except assist.AssistError as e:
    assert "Назовите имя" in str(e)
ok("незнакомое имя → переспрос имени, ничего не выдумывается")

# --- оценка похожести ---------------------------------------------------------
best, second = assist.name_match("Анечка", ["Анна Иванова", "Марина Соколова"])
assert best[1] == "Анна Иванова" and best[0] >= 0.72
best, _ = assist.name_match("ваня", ["Иван Петров"])
assert best[1] == "Иван Петров" and best[0] >= 0.72     # «ваня» разворачивается в «иван»
ok("оценка похожести учитывает и сокращения, и уменьшительно-ласкательные формы")

# --- контракт промтов: что уходит модели при разборе --------------------------
src = open(os.path.join(ROOT, "assist.py"), encoding="utf-8").read()
assert "распознан голосом" in src and "Whisper" in src
ok("разборщику сказано, что текст пришёл из распознавания голоса")
assert "уменьшительно-ласкательные" in src and "списка УЧЕНИКИ" in src
ok("правила подбора имён по базе (сокращения, уменьшительные формы) в промте")
assert "НЕ разворачивай" in src and "краткое остаётся кратким" in src
ok("краткое имя нового ученика не разворачивается в полное — остаётся как продиктовано")
assert "ближайшее распространённое российское имя" in src and "похожие на ошибку распознавания аудио" in src
ok("правятся и переспрашиваются только странные имена — похожие на ошибку распознавания")
assert '"check"' in src and "next_check" in src
ok("модель может переспросить неуверенно распознанное (check), поток это обрабатывает")
assert re.search(r"def classify\(text, \*.*, voice=False\)", src)
ok("флаг «текст из голоса» доходит до промта (classify(voice=…))")

# --- подсказка Whisper: имена учеников ---------------------------------------
assert "def stt_prompt" in open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
assert "prompt" in open(os.path.join(ROOT, "whisper.py"), encoding="utf-8").read().split("def transcribe")[1][:600]
p = bot.stt_prompt()
assert "Анна Иванова" in p and "Марина Соколова" in p and len(p) <= 500
ok("Whisper перед распознаванием получает подсказку с именами учеников")
print("OK")
