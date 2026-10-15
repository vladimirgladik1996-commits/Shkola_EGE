"""Раздел «Материалы»: два каталога (ОГЭ-2027, ЕГЭ-2027) по 36 вариантов — файлы на месте, маршруты закрыты входом,
во фронте: чипсины каталогов → чипсины 36 вариантов → открытие варианта.
Статическая проверка (без сети и без запуска бота). Запуск: python3 tests/test_materials.py"""
import json, os, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def ok(c): print("  ✓", c)
def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f: return f.read()

bot, front = read("bot.py"), read("docs", "index.html")
CATS = {   # id → (что внутри варианта, сколько сочинений в варианте)
    "oge-essays-2027": ("Дощинского", ["13.1", "13.2", "13.3"]),
    "ege-essays-2027": ("Дощинского", None),
}

# --- структура каталогов: materials/<id>/{index.json, about.html, v01.html … v36.html}; старых монолитных .html нет
assert not [f for f in os.listdir(os.path.join(ROOT, "materials")) if f.endswith(".html")], "монолитные каталоги должны быть разбиты по вариантам"
for cid in CATS:
    folder = os.path.join(ROOT, "materials", cid)
    assert sorted(os.listdir(folder)) == sorted(["index.json", "about.html"] + ["v%02d.html" % n for n in range(1, 37)]), cid
    ix = json.loads(read("materials", cid, "index.json"))["variants"]
    assert [v["n"] for v in ix] == list(range(1, 37)) and all(v["sub"] and v["q"] for v in ix), cid
    for n in range(1, 37):
        page = read("materials", cid, "v%02d.html" % n)
        assert "fonts.googleapis" not in page and "<link" not in page, (cid, n)   # страница самодостаточна: без внешних шрифтов и стилей
        assert page.count('class="tab"') == (2 if cid.startswith("ege") else 4) and page.count('role="tabpanel"') == page.count('class="tab"'), (cid, n)
        # хлебные крошки: Оглавление › Вариант N › текст | 27 (ЕГЭ) или текст | 13.1 | 13.2 | 13.3 (ОГЭ)
        assert 'data-nav="toc">Оглавление</button>' in page and ('data-nav="var">Вариант %d</button>' % n) in page, (cid, n)
        labels = re.findall(r'role="tab" data-p="[^"]+" aria-selected="(?:true|false)"(?: aria-label="[^"]*")?>([^<]+)</button>', page)
        assert labels == (["Текст", "27"] if cid.startswith("ege") else ["Текст", "13.1", "13.2", "13.3"]), (cid, n, labels)
        assert ('<div class="num">%d</div>' % n) in page and page.count('class="rd"') >= 1, (cid, n)
        if cid.startswith("ege"):   # вкладки: текст · 27 (сочинение + критерии К1–К10)
            assert all(('id="p-%s"' % x) in page for x in ("text", "essay")) and "p-crit" not in page and page.count('<details class="cr">') == 10, (cid, n)
            assert 'class="chip">' in page and "норма ЕГЭ" in page and '<i class="sn">1</i>' in page, (cid, n)
        else:                       # вкладки: текст · 13.1 · 13.2 · 13.3, у каждого эссе свои критерии СК1–СК4
            assert all(('id="p-%s"' % x) in page for x in ("text", "131", "132", "133")), (cid, n)
            assert page.count('class="qb"') == 3 and page.count('<details class="cr">') == 12 and "норма ОГЭ" in page, (cid, n)
            assert page.count('<p class="at">(По ') == 1, (cid, n)   # указание автора выделено в отдельную строку
    about = read("materials", cid, "about.html")
    assert "Дощинск" in about and "критери" in about and 'data-nav="toc">Оглавление</button>' in about, cid
    ok(f"materials/{cid}/: index.json + about.html + 36 страниц вариантов в дизайне приложения (хлебные крошки: Оглавление › Вариант › {'текст | 27' if cid.startswith('ege') else 'текст | 13.1 | 13.2 | 13.3'})")

# --- бэкенд: маршруты под @guarded, имена файлов — только из белого списка
for cid in CATS:
    assert re.search(r'"%s":\s*"%s"' % (cid, cid), bot), cid
assert re.search(r'@guarded\s+async def api_material\(', bot) and re.search(r'@guarded\s+async def api_material_part\(', bot), "маршруты материалов должны быть под @guarded (вход обязателен)"
assert 'add_get("/api/materials/{mid}", api_material)' in bot and 'add_get("/api/materials/{mid}/{part}", api_material_part)' in bot
assert 'MAT_VARIANTS = 36' in bot and 'fullmatch(r"[0-9]{1,2}"' in bot
ok("/api/materials/{id} и /api/materials/{id}/{n|about} зарегистрированы и закрыты @guarded; вариант — только 1…36")

# --- фронт: каталоги → чипсины 36 вариантов → вариант
for t in ("Каталог эссе для ОГЭ-2027. Русский язык. По сборнику Р.А. Дощинского",
          "Каталог сочинений ЕГЭ-2027. Русский язык. По сборнику Р.А. Дощинского"):
    assert t in front, t
assert 'tab=="mt"?mat()' in front and "function matCatOpen" in front and "function matGrid" in front and "function matOpen(ci,part)" in front
assert 'class="mt-chip mt-v"' in front and "Вариант ${v.n}" in front and ".mt-grid{" in front and "/api/materials/\"+m.id" in front and "/api/materials/\"+key" in front
ok("во фронте: чипсины каталогов → сетка чипсин всех вариантов (с поиском) → открытие варианта")
assert 'data-theme="\'+matTheme()' in front and "function matTheme" in front and 'e.data.matNav==="toc"' in front and "#mv.pg .mvb{display:none}" in front
ok("страница варианта подстраивается под тему приложения (светлая/тёмная)")
assert front.count('img:"data:image/jpeg;base64,') == 3 and 'class="mi mc"><img src="${m.img}"' in front and ".mt-list .mi.mc{flex:0 0 58px;height:58px" in front
ok("у каталогов круглые иконки-обложки (встроены в приложение, 58px вместо 40px)")
assert "vishenka(?:_cover)?" in front and 'src="vishenka_cover.png"' in read("materials", "oge-essays-2027", "about.html") and 'src="vishenka.png"' in read("materials", "ege-essays-2027", "about.html")
ok("обложки страниц «о каталоге» подменяются встроенным талисманом (отдельного png нет)")
assert "fr.srcdoc=" in front and "matBlob" not in front, "вариант должен открываться через srcdoc: Android WebView не открывает blob:-URL во фрейме (net::ERR_FILE_NOT_FOUND)"
ok("страница варианта идёт во фрейм через srcdoc (без blob-URL — ломалось в Android WebView)")
print("OK")
