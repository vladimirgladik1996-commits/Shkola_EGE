"""Статические проверки bot.py + authorized()/check_photo() в изоляции (aiogram/aiohttp не нужны)."""
import ast, datetime, hashlib, hmac, json, os, re, time, types
from urllib.parse import parse_qsl, urlencode
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
tree = ast.parse(src)

api = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name.startswith("api_")]
assert not [n.name for n in api if not any(getattr(d, "id", "") == "guarded" for d in n.decorator_list)]
routes = re.findall(r'add_(?:get|post)\("([^"]+)", (\w+|lambda)', src)
assert not [(p, h) for p, h in routes if not (h.startswith("api_") or p == "/")]
print(f"✓ все {len(api)} API-обработчиков под @guarded, других открытых маршрутов нет")

def routers(n):
    for d in n.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name) and d.func.attr in ("message", "callback_query"):
            yield d.func.value.id
fns = [n for n in tree.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))]
assert [n.name for n in fns if "pub" in set(routers(n))] == ["start"]
assert {x for n in fns for x in routers(n)} == {"pub", "r"}
assert "r.message.outer_middleware(AuthMW())" in src and "r.callback_query.outer_middleware(AuthMW())" in src and "dp.include_router(pub)" in src
print("✓ без входа доступен только /start; остальные обработчики бота — за AuthMW")

ns = {"hmac": hmac, "hashlib": hashlib, "json": json, "re": re, "time": time, "parse_qsl": parse_qsl,
      "TOKEN": "123456:TESTTOKEN", "MAX_PHOTO": 700_000, "INIT_MAX_AGE": 86400, "date": datetime.date}
sessions = {777}
ns["auth"] = types.SimpleNamespace(is_authorized=lambda u: u in sessions,
                                   allowed=lambda u, name=None: type(u) is int and u == 777)
for n in tree.body:
    if isinstance(n, ast.FunctionDef) and n.name in {"authorized", "check_photo", "check_slot", "money_val", "clip", "monday"}: exec(compile(ast.Module([n], []), "bot", "exec"), ns)
    if isinstance(n, ast.Assign) and any(getattr(t, "id", "") in ("PHOTO_RE", "TIME_FMT", "DAY_FMT", "MAX_MONEY") for t in n.targets): exec(compile(ast.Module([n], []), "bot", "exec"), ns)
authorized, check_photo = ns["authorized"], ns["check_photo"]

def sign(fields, token="123456:TESTTOKEN"):
    chk = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(key, chk.encode(), hashlib.sha256).hexdigest()})
class Rq:
    def __init__(s, x): s.headers = {"X-Init": x} if x is not None else {}
now = int(time.time()); user = lambda i=777: json.dumps({"id": i, "first_name": "T"})
good = sign({"auth_date": str(now), "user": user(), "query_id": "AA"})

assert authorized(Rq(good)) == 200;                                                    print("✓ свежий корректный initData владельца + вход → 200")
sessions.clear(); assert authorized(Rq(good)) == 401; sessions.add(777);               print("✓ подпись верна, но входа нет → 401")
assert authorized(Rq(sign({"auth_date": str(now - 90000), "user": user()}))) == 403;   print("✓ initData старше суток (replay) → 403")
assert authorized(Rq(sign({"auth_date": str(now + 3600), "user": user()}))) == 403;    print("✓ initData «из будущего» → 403")
assert authorized(Rq(sign({"auth_date": str(now), "user": user(555)}))) == 403;        print("✓ валидная подпись, но не владелец → 403")
assert authorized(Rq(sign({"auth_date": str(now), "user": user()}, token="999:OTHER"))) == 403; print("✓ подпись чужим токеном → 403")
assert authorized(Rq(good.replace("AA", "BB"))) == 403;                                print("✓ подмена поля при сохранённом hash → 403")
weird_list = (None, "", "hash=", "hash=é&user=1", "hash=\udcff", "a=1", "x" * 9000,
              sign({"auth_date": str(now), "user": "[1,2]"}), sign({"auth_date": str(now), "user": "null"}),
              sign({"auth_date": str(now), "user": '{"id":"777"}'}), sign({"auth_date": str(now), "user": '{"id":true}'}),
              sign({"auth_date": "abc", "user": user()}), sign({"user": user()}), sign({"auth_date": str(now), "user": "[" * 5000}))
for w in weird_list: assert authorized(Rq(w)) == 403, str(w)[:60]
print(f"✓ {len(weird_list)} уродливых X-Init → 403, без исключений")

jpeg = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
assert check_photo(jpeg) == jpeg and check_photo("") == "" and check_photo(None) is None
evil_list = ('data:image/jpeg;base64,AAAA) , url(http://evil)', 'data:image/svg+xml;base64,PHN2Zz4=', 'javascript:alert(1)',
             'data:image/jpeg;base64,AA"onerror="alert(1)', "data:image/jpeg;base64,AA\n", jpeg + "\n", 123, ["x"], "x" * 700_001, "data:image/jpeg;base64,")
for evil in evil_list:
    try: check_photo(evil); raise SystemExit(f"НЕ ОТВЕРГНУТО: {evil!r}")
    except ValueError: pass
print(f"✓ фото: только строгий JPEG data-URL; {len(evil_list)} вариантов инъекции отвергнуты (в т.ч. «\\n» в конце)")
T = ns["TIME_FMT"]
assert T.fullmatch("09:30") and not T.fullmatch("09:30\n") and not T.fullmatch("24:00"); print("✓ время занятия: «09:30\\n» больше не проходит")
assert not T.fullmatch("0\u0669:3\u0660"); print("✓ время: не-ASCII цифры отвергаются")

check_slot, money_val, clip, monday = ns["check_slot"], ns["money_val"], ns["clip"], ns["monday"]
j = {"day": "2026-10-05", "time": "10:00"}; check_slot(j); assert j["day"] == "2026-10-05"
for bad_day in ("20261005", "2026-W41-1", "2026-10-05\n", "2026-1-5", "\u0662026-10-05", 20261005, None, ["2026-10-05"]):
    try: check_slot({"day": bad_day, "time": "10:00"}); raise SystemExit(f"НЕ ОТВЕРГНУТО: {bad_day!r}")
    except (ValueError, TypeError): pass
    try: monday(bad_day); raise SystemExit(f"monday НЕ ОТВЕРГ: {bad_day!r}")
    except (ValueError, TypeError): pass
print("✓ день занятия: только строгий YYYY-MM-DD (в БД нет «20261005», «2026-W41-1»)")

assert money_val(5000) == 5000 and money_val("700") == 700 and money_val(0) == 0 and money_val(1, 1) == 1
for bad_m in (-1, 0 if False else 10**9, 10**30, "9" * 5000, True, 1.5, None, [1], {"a": 1}, "abc", ""):
    try: money_val(bad_m); raise SystemExit(f"НЕ ОТВЕРГНУТО: {bad_m!r}")
    except (ValueError, TypeError, OverflowError): pass
try: money_val(0, 1); raise SystemExit("0 прошёл как оплата")
except ValueError: pass
assert clip("  Иван  ", 100) == "Иван"
for bad_c in ("x" * 101, 5, None, ["x"]):
    try: clip(bad_c, 100); raise SystemExit(f"НЕ ОТВЕРГНУТО: {bad_c!r}")
    except (ValueError, TypeError): pass
print("✓ суммы и текстовые поля: типы и пределы проверяются (нет 500 от переполнения)")
print("\nALL BOT GUARD TESTS PASSED")
