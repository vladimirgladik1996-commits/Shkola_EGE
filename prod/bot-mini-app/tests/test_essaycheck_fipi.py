"""Эталонные критерии ФИПИ 2027 в «ИИ-проверке»: баллы считает КОД по таблице, а не модель.
Проверяются: состав таблиц CANON (ЕГЭ — 22 балла, ОГЭ — 8), зажим баллов до эталонных максимумов,
правило «К1 = 0 → К2 и К3 = 0», правило минимального объёма (150/70 слов → 0 по всем критериям),
алиасы свободных имён, отсечение лишних карточек, вывод типа работы, идемпотентность нормализации
(повторная сборка отчёта из истории не меняет баллы и не удваивает пометки) и защита fipicache
от мусора вместо критериев. Сеть не вызывается.
Запуск: python3 tests/test_essaycheck_fipi.py"""
import asyncio, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.update(BOT_TOKEN="1:x", LOG_LEVEL="CRITICAL")
import essaycheck, fipicache, kimi

def ok(c): print("  ✓", c)

def full(kind, over=None):
    """Полный ответ модели по эталонной таблице: все критерии на максимуме, с переопределениями."""
    raw = []
    for code, label, mx in essaycheck.CANON[kind]:
        c = {"name": label, "got": mx, "max": mx, "why": "почему", "tip": ""}
        c.update((over or {}).get(code, {}))
        raw.append(c)
    return {"errors": [], "criteria": raw, "recommendations": []}

LONG = " ".join(["слово"] * 160)     # больше минимума ЕГЭ (150 слов)
SHORT = " ".join(["слово"] * 149)    # 149 слов: по правилам ФИПИ работа не засчитывается

# ---------- 1. эталонная таблица: состав, подписи, итог в шкале ФИПИ ----------
r = essaycheck._result(full("ege"), LONG, "")
assert [c["name"] for c in r["criteria"]] == [c[1] for c in essaycheck.CANON["ege"]], r["criteria"]
assert r["score"] == {"got": 22, "max": 22, "percent": 100, "level": "Отлично"}, r["score"]
r = essaycheck._result(full("oge"), LONG, "")
assert [c["name"] for c in r["criteria"]] == [c[1] for c in essaycheck.CANON["oge"]], r["criteria"]
assert r["score"] == {"got": 8, "max": 8, "percent": 100, "level": "Отлично"}, r["score"]
ok("эталон: карточки строго по таблице ФИПИ, итог — 22 (ЕГЭ) / 8 (ОГЭ), а не шкала модели")

# ---------- 2. баллы зажимаются эталоном; К1 = 0 → К2 и К3 = 0 ----------
r = essaycheck._result(full("ege", {"К1": {"got": 5, "max": 5}, "К2": {"got": 9, "max": 6}}), LONG, "")
c1, c2 = r["criteria"][0], r["criteria"][1]
assert (c1["got"], c1["max"]) == (1, 1) and (c2["got"], c2["max"]) == (3, 3), (c1, c2)   # зажим по эталону
assert r["score"]["got"] == 22, r["score"]
r = essaycheck._result(full("ege", {"К1": {"got": 0}}), LONG, "")
assert [c["got"] for c in r["criteria"][:3]] == [0, 0, 0], r["criteria"][:3]
assert "К1 = 0" in r["criteria"][1]["why"] and "К1 = 0" in r["criteria"][2]["why"]
assert r["score"]["got"] == 16, r["score"]          # 22 − 1 − 3 − 2
ok("баллы зажимаются эталонными максимумами; К1 = 0 → К2 и К3 обнуляются с пометкой")

# ---------- 3. минимальный объём (правило ФИПИ): меньше — 0 по всем критериям ----------
r = essaycheck._result(full("ege"), SHORT, "")
assert r["score"] == {"got": 0, "max": 22, "percent": 0, "level": "Нужно доработать"}, r["score"]
assert all(c["got"] == 0 for c in r["criteria"])
assert r["criteria"][0]["why"].startswith("Объём 149 слов (минимум 150)"), r["criteria"][0]["why"]
r2 = essaycheck._result(r, r["text"], r["task"])     # пересборка отчёта из истории (essayreport.from_client)
assert [c["why"] for c in r2["criteria"]] == [c["why"] for c in r["criteria"]], "пометка об объёме удвоилась"
assert r2["score"] == r["score"], (r2["score"], r["score"])
assert essaycheck._result(full("oge"), " ".join(["слово"] * 69), "")["score"]["got"] == 0     # ОГЭ: минимум 70
assert essaycheck._result(full("oge"), " ".join(["слово"] * 70), "")["score"]["got"] == 8     # ровно 70 — засчитана
assert essaycheck._result(full("ege"), " ".join(["слово"] * 150), "")["score"]["got"] == 22   # ровно 150 — засчитана
ok("правило объёма: 149/69 слов → 0 по всем; 150/70 — засчитывается; пометка идемпотентна")

# ---------- 4. свободные имена → коды эталона; лишние карточки балл не портят ----------
a = {"errors": [], "criteria": [
    {"name": "Орфография", "got": 2, "max": 3, "why": "2 ошибки", "tip": ""},
    {"name": "Грамотность", "got": 1, "max": 1, "why": "лишняя", "tip": ""},
    {"name": "К2 Комментарий", "got": 3, "max": 3, "why": "ок", "tip": ""}]}
r = essaycheck._result(a, LONG, "")
assert r["criteria"][6]["name"] == "К7 Орфография" and r["criteria"][6]["got"] == 2, r["criteria"][6]
assert r["criteria"][1]["got"] == 3, r["criteria"][1]                 # К2 — по коду из имени
assert all("лишняя" not in c["why"] for c in r["criteria"])           # «Грамотность» не из таблицы — мимо
assert (r["score"]["got"], r["score"]["max"]) == (5, 22), r["score"]
ok("алиасы имён собираются в эталонные коды; лишние карточки в балл не попадают")

# ---------- 5. тип работы выводится по карточкам; ОГЭ — только СК1–СК4 ----------
a = {"errors": [], "criteria": [{"name": "СК2 Примеры", "got": 4, "max": 4, "why": "ок", "tip": ""},
                                {"name": "К1", "got": 1, "max": 1, "why": "не для ОГЭ", "tip": ""}]}
r = essaycheck._result(a, LONG, "")                  # kind не передан → выводится из карточек (СК* → ОГЭ)
assert [c["name"] for c in r["criteria"]] == [c[1] for c in essaycheck.CANON["oge"]], r["criteria"]
assert r["criteria"][1]["got"] == 4 and r["score"] == {"got": 4, "max": 8, "percent": 50, "level": "Удовлетворительно"}
ok("без явного типа СК-карточки определяются как ОГЭ; егэшный К1 в ОГЭ не попадает")

# ---------- 6. пустые данные — честные missing, без выдуманных критериев ----------
r = essaycheck._result({}, LONG, "")
assert r["criteria"] == [] and "criteria" in r["missing"], (r["criteria"], r["missing"])
r = essaycheck._result({"criteria": [{"name": "Выдуманный критерий", "got": 1, "max": 1}]}, LONG, "")
assert r["criteria"] == [] and "criteria" in r["missing"], r["criteria"]
ok("нет разбора или ни одна карточка не из таблицы → критерии в missing, баллы не выдумываются")

# ---------- 7. fipicache: в промт идут только тексты-критерии, мусор отбрасывается ----------
assert fipicache._like("К1 Отражение позиции автора — 1 балл") and fipicache._like("СК1. Ответ на вопрос… 0 баллов")
assert not fipicache._like("Демоверсии, спецификации и кодификаторы. Скачайте файлы на сайте.")
assert not fipicache._like("") and not fipicache._like(None)
real_load, old_key = fipicache._load, kimi.API_KEY
try:
    kimi.API_KEY = ""                                # ИИ-провайдер выключен: обновления с fipi.ru не будет
    fipicache._load = lambda kind: {"ts": 0, "text": "мусор со страницы fipi.ru без таблицы критериев"}
    assert asyncio.run(fipicache.criteria("ege", "FALLBACK")) == "FALLBACK"      # мусорный кэш отброшен
    fipicache._load = lambda kind: {"ts": time.time(), "text": "К1 (0–1): 1 балл — позиция сформулирована"}
    assert asyncio.run(fipicache.criteria("ege", "FALLBACK")).startswith("К1")   # свежая таблица берётся
finally:
    fipicache._load, kimi.API_KEY = real_load, old_key
ok("fipicache: мусор вместо критериев в промт не попадает, работает fallback")

# ---------- 8. промт несёт эталонную таблицу и не несёт старую шкалу ----------
m = essaycheck._msg("ege", "", LONG, essaycheck.CRITERIA["ege"])
assert "22 балла" in m and "К10 Речевые нормы" in m and "Если К1=0" in m
assert "Шкала ошибок" not in m and "итого 17" not in m
m = essaycheck._msg("oge", "", LONG, essaycheck.CRITERIA["oge"])
assert "8 баллов" in m and "СК4 Композиционная стройность" in m
ok("промт несёт эталонные таблицы ФИПИ 2027 (22/8), старая шкала убрана")

print("test_essaycheck_fipi: все проверки прошли")
