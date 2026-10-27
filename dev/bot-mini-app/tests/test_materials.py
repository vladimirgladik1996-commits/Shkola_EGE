"""Раздел «Материалы»: три каталога (ОГЭ-2027, ОГЭ-2027 «до 80 слов», ЕГЭ-2027) по 36 вариантов — файлы на месте, маршруты закрыты входом,
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
    "oge-essays-2027-vishenka": ("Дощинского", ["13.1", "13.2", "13.3"]),
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
        # хлебные крошки (один ряд): Оглав. › В. N › текст | 27 (ЕГЭ) или текст | 13.1 | 13.2 | 13.3 (ОГЭ)
        assert 'data-nav="toc" aria-label="Оглавление">Оглав.</button>' in page and ('data-nav="var" aria-label="Вариант %d">В. %d</button>' % (n, n)) in page, (cid, n)
        nav = page.split("</nav>")[0]
        assert ">Оглавление</button>" not in nav and (">Вариант %d</button>" % n) not in nav, (cid, n)   # в крошках только сокращения (полное слово — в aria-label)
        assert "flex-wrap:nowrap" in page and "flex-wrap:wrap" not in page.split(".bar{")[1].split("}")[0] and page.count("height:48px") >= 2, (cid, n)   # крошки и вкладки — в один ряд, цель нажатия 48px
        labels = re.findall(r'role="tab" data-p="[^"]+" aria-selected="(?:true|false)"(?: aria-label="[^"]*")?>([^<]+)</button>', page)
        assert labels == (["Текст", "27"] if cid.startswith("ege") else ["Текст", "13.1", "13.2", "13.3"]), (cid, n, labels)
        assert ('<div class="num">%d</div>' % n) in page and page.count('class="rd"') >= 1, (cid, n)
        assert 'h.style.scrollBehavior' in page, (cid, n)   # переключение вкладок: новый раздел всегда с начала
        if cid.startswith("ege"):   # вкладки: текст · 27 (сочинение + критерии К1–К10)
            assert all(('id="p-%s"' % x) in page for x in ("text", "essay")) and "p-crit" not in page and page.count('<details class="cr">') == 10, (cid, n)
            assert 'class="chip">' in page and "норма ЕГЭ" in page and '<i class="sn">1</i>' in page, (cid, n)
        else:                       # вкладки: текст · 13.1 · 13.2 · 13.3, у каждого эссе свои критерии СК1–СК4
            assert all(('id="p-%s"' % x) in page for x in ("text", "131", "132", "133")), (cid, n)
            assert page.count('class="qb"') == 3 and page.count('<details class="cr">') == 12 and "норма ОГЭ" in page, (cid, n)
            assert page.count('<p class="at">(По ') == (1 if cid == "oge-essays-2027" else 0), (cid, n)   # автор: отдельной строкой либо в шапке (каталог «до 80 слов»)
    about = read("materials", cid, "about.html")
    assert "Дощинск" in about and "критери" in about and 'data-nav="toc" aria-label="Оглавление">Оглав.</button>' in about and 'h.style.scrollBehavior' in about, cid
    ok(f"materials/{cid}/: index.json + about.html + 36 страниц вариантов в дизайне приложения (хлебные крошки в один ряд: Оглав. › В. N › {'текст | 27' if cid.startswith('ege') else 'текст | 13.1 | 13.2 | 13.3'})")

# --- бэкенд: маршруты под @guarded, имена файлов — только из белого списка
for cid in CATS:
    assert re.search(r'"%s":\s*"%s"' % (cid, cid), bot), cid
assert re.search(r'@guarded\s+async def api_material\(', bot) and re.search(r'@guarded\s+async def api_material_part\(', bot), "маршруты материалов должны быть под @guarded (вход обязателен)"
assert 'add_get("/api/materials/{mid}", api_material)' in bot and 'add_get("/api/materials/{mid}/{part}", api_material_part)' in bot
assert 'MAT_VARIANTS = 36' in bot and 'fullmatch(r"[0-9]{1,2}"' in bot
ok("/api/materials/{id} и /api/materials/{id}/{n|about} зарегистрированы и закрыты @guarded; вариант — только 1…36")

# --- фронт: каталоги → чипсины 36 вариантов → вариант
for t in ("Каталог эссе для ОГЭ-2027. Русский язык. По сборнику Р.А. Дощинского",
          "Каталог эссе для ОГЭ-2027. Русский язык. По сборнику Р.А. Дощинского (До 80 слов)",
          "Каталог сочинений ЕГЭ-2027. Русский язык. По сборнику Р.А. Дощинского"):
    assert t in front, t
assert 'tab=="mt"?mat()' in front and "function matCatOpen" in front and "function matGrid" in front and "function matOpen(ci,part,sec)" in front
assert 'class="mt-chip mt-v"' in front and "Вариант ${v.n}" in front and ".mt-grid{" in front and "/api/materials/\"+m.id" in front and "/api/materials/\"+key" in front
ok("во фронте: чипсины каталогов → сетка чипсин всех вариантов (с поиском) → открытие варианта")
assert 'data-theme="\'+matTheme()' in front and "function matTheme" in front and 'e.data.matNav==="toc"' in front and "#mv.pg .mvb{display:none}" in front
assert 'class="mt-chip mt-back" type="button" onclick="matBack()"' in front and ".mt-back{min-height:52px" in front and "position:sticky;top:calc(56px" in front, "в оглавлении нужна крупная кнопка «Материалы»"
assert 'class="mvc" type="button" onclick="matClose()" aria-label="Оглавление">Оглав.</button>' in front and '"В. "+part' in front and "‹ Варианты" not in front
ok("оглавление: крупная приклеенная кнопка «Материалы»; полоса загрузки варианта — те же крошки «Оглав. › В. N»")
front_nb = re.sub(r"data:[a-z/+-]+;base64,[A-Za-z0-9+/=]+", "", front)   # для проверок «такого в коде нет»: символы base64 внутри встроенных картинок не в счёт
assert 'const MAT_ART="data:image/webp;base64,' in front and front.count("${matArt()}") == 2 and ".mt-art{" in front, "иллюстрация внизу каталогов и оглавления"
assert "matOpen(ci,n,sec)" in front and "matShow" in front and "c.cat=MATS[matCat].id" in front, "голос: страница открывается на нужной вкладке"
assert 'if raw.get("cat") is not None:' in bot and "_material_nav" in read("assist.py") and "import kimi, matlib, matnav, whisper" in read("assist.py")
assert all("matShow" in read("materials", c, f) for c in CATS for f in ["about.html"] + ["v%02d.html" % n for n in range(1, 37)]), "страницы принимают matShow"
ok("голос: «Открой эссе 13.2 варианта 7 ОГЭ» → сервер отдаёт stage open, мини-апп открывает страницу; иллюстрация внизу «Материалов»")
assert "function viewTurn(" in front and "function ptCurl(" in front and "function ptGeom(" in front and "ptScreenTurn(false)" in front and "ptScreenTurn(true,fin)" in front and "viewTurn(true," in front and "viewTurn(false," in front, "листание с изгибом в оболочке"
assert "prefers-reduced-motion" in front and "ptOK()" in front and "REDUCE" in front, "при «уменьшении анимации» листание отключается"
assert ".pt-k{overflow:hidden;background:var(--bg)}" in front and "function ptFix(" in front and "ptFix(c,host)" in front, "лист непрозрачный и совмещён с оригиналом: новая страница не просвечивает сквозь поворачиваемый лист"
for cid in CATS:
    for f in ["about.html"] + ["v%02d.html" % n for n in range(1, 37)]:
        pg = read("materials", cid, f)
        assert "function swap(" in pg and "function place(" in pg and "function ptCurl(" in pg and "prefers-reduced-motion" in pg and ".thumb{" in pg, (cid, f)
        assert ".pt-k{overflow:hidden;background:var(--bg)}" in pg and "ptFix(src,se)" in pg, (cid, f)
        assert ('<span class="thumb" aria-hidden="true"></span>' in pg) == (f != "about.html"), (cid, f)
ok("анимации: жидкая «капля» между вкладками крошек, листание с изгибом (разделы, экраны, варианты); без анимации при «уменьшении движения»")
ok("страница варианта подстраивается под тему приложения (светлая/тёмная)")
assert front.count('img:"data:image/jpeg;base64,') == 3 and 'class="mi mc"><img src="${m.img}"' in front and ".mt-list .mi.mc{flex:0 0 58px;height:58px" in front
ok("у каталогов круглые иконки-обложки (встроены в приложение, 58px вместо 40px)")
assert "vishenka(?:_cover)?" in front and 'src="vishenka_cover.png"' in read("materials", "oge-essays-2027", "about.html") and 'src="vishenka.png"' in read("materials", "ege-essays-2027", "about.html")
ok("обложки страниц «о каталоге» подменяются встроенным талисманом (отдельного png нет)")
assert "fr.srcdoc=" in front and "matBlob" not in front, "вариант должен открываться через srcdoc: Android WebView не открывает blob:-URL во фрейме (net::ERR_FILE_NOT_FOUND)"
ok("страница варианта идёт во фрейм через srcdoc (без blob-URL — ломалось в Android WebView)")
# --- «Спросить у Вишенки»: кнопка при выделении текста → виджет голосом → ответ в чат с ботом
asst = read("assist.py")
for cid in CATS:
    for n in (1, 7, 36):
        page = read("materials", cid, "v%02d.html" % n)
        assert 'id="ask"' in page and ('data-cat="%s" data-n="%d"' % (cid, n)) in page and "matAsk" in page and 'src="vishenka.png"' in page, (cid, n)
        assert "['click','touchend','pointerup']" in page, (cid, n)   # кнопка срабатывает и по тапу: preventDefault на touchstart подавляет click
    assert 'id="ask"' not in read("materials", cid, "about.html"), cid   # на странице «о каталоге» кнопки нет
ok("страницы вариантов: при выделении текста появляется кнопка «Спросить у Вишенки» (на «о каталоге» её нет)")
code = re.sub(r'data:[^"\')\s]+', '', front)   # base64-иллюстрации могут случайно содержать «fqc» — мусорные слова ищем только в коде
code = re.sub(r'"[A-Za-z0-9+/=]{200,}"', '""', code)   # иллюстрации вида "data:image/…" + "<base64>" — сам base64 живёт отдельной строкой
assert "function fragOpen(a)" in front and "e.data.matAsk" in front and 'api(fr?"/api/fragment":"/api/voice"' in front and "fqc" not in code and "FRAG_ASKS" not in code and "body:has(#mv.on) #sheet{z-index:90}" in front
assert 'add_post("/api/fragment", api_fragment)' in bot and re.search(r'@guarded\s+async def api_fragment\(', bot), "маршрут /api/fragment должен быть под @guarded"
ok("мини-апп открывает виджет Вишенки по выделенному фрагменту; /api/fragment закрыт входом (@guarded)")

# логика проверки запроса и промпта (чистые функции вырезаются из исходников: бот целиком без aiogram не импортируется)
ns = {"re": re, "MATERIALS": {"oge-essays-2027": "oge-essays-2027", "ege-essays-2027": "ege-essays-2027"}, "MAT_VARIANTS": 36}
exec(bot[bot.index("def _int_id(x):"):bot.index("def _day(x):")], ns)
exec(bot[bot.index("FRAG_MAX = 3000"):bot.index("async def tg_send_long")], ns)
good = {"fragment": "  Однажды   осенью\u00a0я ехал.\n\n\n  Моим попутчиком  ", "cat": "ege-essays-2027", "n": 7, "part": "p-text"}
frag, meta = ns["fragment_meta"](good)
assert frag == "Однажды осенью я ехал.\n\n\nМоим попутчиком" or frag.startswith("Однажды осенью я ехал."), frag
assert meta == {"cat": "ege-essays-2027", "n": 7, "part": "p-text"}
assert ns["fragment_meta"]({**good, "part": None})[1]["part"] == "other"
for bad in ({**good, "fragment": "ab"}, {**good, "fragment": "я" * 3001}, {**good, "fragment": 5}, {**good, "cat": "../x"}, {**good, "n": 0},
            {**good, "n": 37}, {**good, "n": True}, {**good, "n": "7"}, {**good, "part": "p-999"}, {k: v for k, v in good.items() if k != "cat"}):
    try:
        ns["fragment_meta"](bad); raise SystemExit("не отсеяно: %r" % (bad,))
    except ValueError:
        pass
assert "\x00" not in ns["fragment_meta"]({**good, "fragment": "до\x00после текста"})[0]
ns2 = {}
exec(asst[asst.index("FRAG_SYS = ("):asst.index("async def answer_fragment")], ns2)
pr = ns2["fragment_prompt"]("Текст фрагмента.", "перепиши короче", {"cat": "oge-essays-2027", "n": 12, "part": "p-132"})
assert "вариантом 12 каталога эссе ОГЭ-2027" in pr and "сочинения 13.2" in pr and "«Текст фрагмента.»" in pr and "ЗАДАЧА РЕПЕТИТОРА: перепиши короче" in pr, pr
ok("проверка запроса (3…3000 символов, каталог из белого списка, вариант 1…36, часть страницы) и сбор задачи для модели")
print("OK")
