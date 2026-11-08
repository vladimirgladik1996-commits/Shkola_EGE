"""Мышление Вишенки — строго на русском.

Reasoning_content kimi-k2.x/kimi-k3 по умолчанию ведётся по-английски даже на русском
вопросе, а язык размышлений НЕ задаётся параметром Moonshot API (в kimi.build_payload
только thinking.type и reasoning_effort) — единственный рычаг: жёсткая инструкция в
промте. Она должна стоять во ВСЕХ промтах, чьё мышление видит пользователь:
  • CHAT_SYS_APP  — «Чат с ИИ» (мини-апп и АПК — одна страница, стрим «💜 Вишенка думает»);
  • CHAT_SYS      — бот Telegram, глубокий режим (мышление релеится в ответ);
  • FRAG_SYS      — «Спросить у Вишенки» (выделенный фрагмент);
  • CRITERIA_PROMPT — «ИИ-проверка» (мышление Кими стримится в прелоадер от лица Вишенки).
ERRORS_PROMPT намеренно без этой строки: его thinking выключен и наружу не идёт.
Проверяются обе копии (dev и prod) — их идентичность держит test_release_contract.
Запуск: python3 tests/test_think_ru.py
"""
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # bot-mini-app
BASE = os.path.dirname(HERE)                                          # dev|prod
REPO = os.path.dirname(BASE)                                          # корень репозитория

THINK_RU = "Мысли и внутренние рассуждения (thinking) веди СТРОГО на русском языке"


def ok(c):
    print("  ✓", c)


def read(*p):
    with open(os.path.join(REPO, *p), encoding="utf-8") as f:
        return f.read()


for side in ("dev", "prod"):
    assist = read(side, "bot-mini-app", "assist.py")
    check = read(side, "bot-mini-app", "essaycheck.py")
    blocks = {
        "CHAT_SYS": assist[assist.index("CHAT_SYS = ("):assist.index("async def answer_chat")],
        "FRAG_SYS": assist[assist.index("FRAG_SYS = ("):assist.index("FRAG_CAT =")],
        "CHAT_SYS_APP": assist[assist.index("CHAT_SYS_APP = ("):assist.index("CHAT_MAX_MSGS")],
        "CRITERIA_PROMPT": check[check.index("CRITERIA_PROMPT"):check.index("_TASK_RE")],
    }
    for name, block in blocks.items():
        assert THINK_RU in block, "%s: %s — размышления не закреплены на русском" % (side, name)
    ok("%s: CHAT_SYS, CHAT_SYS_APP, FRAG_SYS, CRITERIA_PROMPT — «мыслить строго на русском»" % side)
    assert THINK_RU not in check[check.index("ERRORS_PROMPT"):check.index("CRITERIA_PROMPT")], \
        "%s: ERRORS_PROMPT без стрима мышления — строка там не нужна" % side
    ok("%s: ERRORS_PROMPT не тронут (мышление там выключено и не показывается)" % side)
print("test_think_ru: OK")
