"""Регрессия «ИИ-проверка»: ответ модели — НЕДОВЕРЕННЫЕ данные (баг-репорт 004).
Модель может вернуть found числом/объектом («found»: 2024), errors не списком,
координаты inf/nan/bool. Разбор не должен падать: TypeError/AttributeError/
OverflowError рвали пайплайн УЖЕ ПОСЛЕ resp.prepare() — клиент получал
«Invalid character in chunk size: b'HTTP/1.1 400 Bad Request'», а reverse-proxy
Render видел мёртвый бэкенд. Запуск: python3 tests/test_essaycheck_parse.py"""
import itertools, os, random, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import essaycheck

def ok(c): print("  ✓", c)

PAGES = [{"text": "сочинение2024год", "words": [
    {"t": "сочинение", "x1": 10, "y1": 10, "x2": 30, "y2": 14},
    {"t": "2024", "x1": 31, "y1": 10, "x2": 40, "y2": 14},
    {"t": "год", "x1": 41, "y1": 10, "x2": 50, "y2": 14}]}]

# --- 1. _norm принимает что угодно: не-строка → пустой фрагмент, а не AttributeError ---
assert essaycheck._norm(2024) == ""
assert essaycheck._norm({"a": 1}) == ""
assert essaycheck._norm(None) == ""
assert essaycheck._norm(True) == ""
assert essaycheck._norm("Ёжик!") == "ежик"
ok("_norm: не-строки → пустой фрагмент (без AttributeError)")

# --- 2. _lev1: удаление последнего символа находится (был off-by-one: range(lb)) ---
assert essaycheck._lev1("го", "год") is True        # вставка в конец
assert essaycheck._lev1("год", "го") is True        # удаление последнего символа — падало
assert essaycheck._lev1("год", "од") is True        # удаление первого символа
assert essaycheck._lev1("год", "гуд") is True       # замена
assert essaycheck._lev1("год", "готд") is True      # вставка в середину
assert essaycheck._lev1("год", "годы") is True
assert essaycheck._lev1("год", "годки") is False    # расхождение > 1
assert essaycheck._lev1("а", "бв") is False
ok("_lev1: удаление/вставка/замена ≤1 символа, включая последний символ")

# --- 3. _bind_coords: errors не списком и found числом → без TypeError/AttributeError ---
essaycheck._bind_coords({"errors": 5}, PAGES)                 # TypeError: 'int' object is not iterable
essaycheck._bind_coords({"errors": 2.5}, PAGES)
essaycheck._bind_coords({"errors": {"а": 1}}, PAGES)
essaycheck._bind_coords({"errors": [{"found": 2024}, {"found": {"a": 1}}, None, "x"]}, PAGES)
essaycheck._bind_coords({"errors": [{"found": "год"}], "pages": 7}, PAGES)
ok("_bind_coords: errors-не-список и found-число/объект не роняют разбор")

# --- 4. _result: inf/nan/bool в координатах, мусор вместо списков → без OverflowError ---
r = essaycheck._result({"errors": [{"found": "год", "correct": "год", "page": float("inf"),
                                    "x": float("nan"), "y": True}],
                        "criteria": 7, "recommendations": "мусор"}, "текст", "задание")
assert r["errors"][0]["found"] == "год"
assert "page" not in r["errors"][0] and "x" not in r["errors"][0] and "y" not in r["errors"][0]
assert r["criteria"] == [] and r["recommendations"] == []
assert essaycheck._num(float("inf"), 1, 1, 10) == 1
assert essaycheck._num(float("nan"), 2, 1, 10) == 2
assert essaycheck._num("abc", 3, 1, 10) == 3
ok("_result/_num: inf/nan/bool в числах и мусор в списках не роняют разбор")

# --- 5. фаззер разбора: комбинации мусорных полей → ни одного исключения ---
GARBAGE = [None, 5, 2.5, float("inf"), float("nan"), True, "строка", ["x"], {"a": 1}]
for f, p, x, y in itertools.product(GARBAGE, repeat=4):
    a = {"errors": [{"found": f, "page": p, "x": x, "y": y, "type": f, "correct": p}]}
    essaycheck._bind_coords(a, PAGES)
    essaycheck._result(a, "т", "з")
for errs in GARBAGE:
    for c in GARBAGE:
        a = {"errors": errs, "criteria": c, "recommendations": errs}
        essaycheck._bind_coords(a, PAGES)
        essaycheck._result(a, "т", "з")
ok("фаззер: 682+ комбинации мусорных полей — 0 падений")

# --- 6. честный разбор по-прежнему работает ---
a = {"errors": [{"type": "Орфография", "found": "сочинение", "correct": "сочинение", "why": "w", "src": "s"}],
     "criteria": [{"name": "К1", "got": 2, "max": 2}]}
essaycheck._bind_coords(a, PAGES)
e = a["errors"][0]
assert (e["page"], e["x"], e["y"]) == (0, 20.0, 12.0), e   # центр бокса слова из OCR
r = essaycheck._result(a, "текст", "задание")
assert r["score"] == {"got": 2, "max": 2, "percent": 100, "level": "Отлично"}, r["score"]
ok("честный разбор: координаты из OCR и баллы по критериям на месте")

# --- 7. _near: дешёвый префильтр перед _lev1 НЕ отбрасывает совпадений (звук) ---
ALPH = "абвгде"
for _ in range(3000):
    a = "".join(random.choice(ALPH) for _ in range(random.randint(1, 6)))
    b = "".join(random.choice(ALPH) for _ in range(random.randint(1, 6)))
    assert not essaycheck._lev1(a, b) or essaycheck._near(a, b), (a, b)
ok("_near: 3000 случайных пар — ни одно совпадение _lev1 не отброшено")

# --- 8. _bind_coords не морозит процесс: 6 страниц × 40 ошибок с ненайденными фрагментами ---
pages = []
for pi in range(6):
    ws = [{"t": "слово%d" % i, "x1": 1, "y1": 1, "x2": 2, "y2": 2} for i in range(400)]
    pages.append({"text": "".join(w["t"] for w in ws), "words": ws})
a = {"errors": [{"found": "фрагмент%d" % i} for i in range(40)]}
t0 = time.time()
essaycheck._bind_coords(a, pages)
dt = time.time() - t0
assert dt < 5.0, "_bind_coords слишком долгий: %.2f с" % dt
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "essaycheck.py"), encoding="utf-8").read()
assert "asyncio.to_thread(_bind_coords" in src, "_bind_coords не вынесен из событийного цикла"
ok("_bind_coords: 6 страниц × 40 ошибок — %.2f с, поиск вне event loop" % dt)

print("test_essaycheck_parse: OK")
