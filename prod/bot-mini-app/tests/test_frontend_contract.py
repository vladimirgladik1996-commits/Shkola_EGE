"""Контракт «фронт ↔ маршруты»: каждый api("/api/…") / del("/api/…") в docs/index.html должен
идти тем HTTP-методом, который зарегистрирован на сервере (api() шлёт POST, если передано тело, иначе GET).
Ловит ровно ту ошибку, из-за которой «Поделиться PDF» в APK отвечало 405."""
import os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
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
# --- ИИ-проверка: боевая сборка без мок-заглушки + баннер демо-режима (баг-репорт 001) ---
import re as _re
m = _re.search(r"const EC_MOCK\s*=\s*(true|false)", html)
assert m, "в index.html нет переключателя EC_MOCK"
assert m.group(1) == "false", "EC_MOCK=true — экран разбора снова подменяет живой разбор моками (баг-репорт 001)"
ok("EC_MOCK=false: разбор берётся из /api/essay_check/run, а не из локального мока")
assert "ec-demo" in html and "Демо-режим" in html, "нет баннера демо-режима на экране разбора"
assert "demo:!!s.demo" in html.replace(" ", "").replace("demo:!!s.demo", "demo:!!s.demo") or "demo:!!s.demo" in html.replace(" ", ""),     "ecNrm не пропускает флаг demo — баннер демо-режима не покажется"
assert "recommendations:EC_MOK_RECS,demo:true" in html.replace(" ", "").replace("recommendations: EC_MOK_RECS, demo: true", "recommendations:EC_MOK_RECS,demo:true"),     "отладочный мок не помечен demo:true"
ok("баннер «Демо-режим» есть, флаг demo проходит через ecNrm, мок помечен demo:true")

print("ALL FRONTEND CONTRACT TESTS PASSED")
