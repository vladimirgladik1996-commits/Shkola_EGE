"""Контракт «фронт ↔ маршруты»: каждый api("/api/…") / del("/api/…") в docs/index.html должен
идти тем HTTP-методом, который зарегистрирован на сервере (api() шлёт POST, если передано тело, иначе GET).
Ловит ровно ту ошибку, из-за которой «Поделиться PDF» в APK отвечало 405."""
import os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", AUTH_LOGIN="Admin", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import bot
def ok(c): print("  ✓", c)

routes = {}                                   # путь → {методы}
for res in bot.make_app().router.resources():
    for r in res:
        routes.setdefault(res.canonical, set()).add(r.method)

html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()

def top_level_args(src, i):
    """Аргументы вызова верхнего уровня; i — позиция сразу после «(»."""
    depth, args, cur, q = 1, [], "", None
    while i < len(src) and depth:
        ch = src[i]
        if q:
            cur += ch
            if ch == "\\": i += 1; cur += src[i]
            elif ch == q: q = None
        elif ch in "\"'`": q = ch; cur += ch
        elif ch in "([{": depth += 1; cur += ch
        elif ch in ")]}":
            depth -= 1
            if depth: cur += ch
        elif ch == "," and depth == 1: args.append(cur); cur = ""
        else: cur += ch
        i += 1
    args.append(cur)
    return [a.strip() for a in args if a.strip()]

calls = []                                    # (путь, метод, фрагмент)
for m in re.finditer(r"(?<![\w.])(api|del)\(", html):
    args = top_level_args(html, m.end())
    if not args: continue
    paths = re.findall(r"[\"'](/api/[\w/.]+)", args[0])
    if not paths: continue
    if m.group(1) == "del":                   # del(path,id,question) → api(path,{id})
        method = "POST"
    else:
        method = "POST" if len(args) > 1 else "GET"
    for p in paths:
        calls.append((p, method, html[m.start():m.start() + 70].replace("\n", " ")))

assert len(calls) >= 10, f"разобрано слишком мало вызовов api(): {len(calls)}"
bad = []
for path, method, snippet in calls:
    if path not in routes:
        bad.append(f"{method} {path}: маршрута нет на сервере   ← {snippet}")
    elif method not in routes[path]:
        bad.append(f"{method} {path}: сервер принимает только {sorted(routes[path] - {'HEAD'})}   ← {snippet}")
assert not bad, "\n" + "\n".join(bad)
ok(f"все {len(calls)} вызовов api() из index.html совпадают с методами маршрутов")

pl = [c for c in calls if c[0] == "/api/pdf_link"]
assert pl and all(m == "GET" for _, m, _ in pl)
ok("/api/pdf_link зовётся GET-ом (start в query-строке)")
print("ALL FRONTEND CONTRACT TESTS PASSED")
