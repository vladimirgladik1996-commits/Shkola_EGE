"""«Текст проверен»: настоящий index.html в Chromium с подставным API. Сценарий ученика:
распознанный текст сразу после распознавания → нажатие на слово / выделение фрагмента → «Спросить у Вишенки» → виджет
(текстом и голосом) → «Применить» → поправка видна в тексте → зелёная «Текст проверен» → второй этап по поправленному тексту.
Снимки экрана — в /tmp/ec_preview_*.png. Запуск: python3 tests/test_essay_preview_e2e.py"""
import base64, io, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def ok(c): print("  ✓", c)
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p:
        _p.chromium.launch(args=["--no-sandbox"]).close()
except Exception as e:
    print("Chromium недоступен (%s) — E2E «Текст проверен» пропущен" % type(e).__name__); print("OK"); sys.exit(0)

TEXT = "Автор считает что празник важен для людей.\n\nЯ люблю читать кныги и думать."
RESULT = {"task": "", "text": "Автор считает что праздник важен для людей.\n\nЯ люблю читать книги и думать.",
          "errors": [{"type": "Пунктуация", "found": "считает что", "correct": "считает, что", "why": "Запятая перед «что».", "src": "Правила", "x": 30, "y": 22, "page": 0}],
          "criteria": [{"name": "К1 Формулировка проблемы", "max": 1, "got": 1, "why": "Проблема названа верно.", "tip": ""}],
          "recommendations": [{"title": "Проверьте запятые", "text": "Перед «что».", "badge": "+1 К8"}],
          "score": {"got": 1, "max": 1, "percent": 100, "level": "Хорошо"}}
DB = {"students": [], "lessons": [], "payments": [], "cur": "₽", "env": ""}
def nd(*evs): return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in evs)

state = {"run": [], "edit": [], "mode": "normal"}
def handler(route):
    url, req = route.request.url, route.request
    J = lambda o, st=200: route.fulfill(status=st, content_type="application/json", body=json.dumps(o, ensure_ascii=False))
    if url.endswith("/index.html") or url.endswith("/"):
        return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read())
    if "/api/data" in url: return J(DB)
    if "/api/essay_check/last" in url: return J({"last": None})
    if "/api/essay_check/run" in url:
        b = json.loads(req.post_data); state["run"].append(b)
        if b.get("stage") == "ocr":
            return route.fulfill(status=200, content_type="application/x-ndjson", body=nd(
                {"t": "status", "v": "Собрала 1 фото в одно сочинение"}, {"t": "status", "v": "Распознан рукописный текст"}, {"t": "ocr", "v": {"text": TEXT}}))
        if state["mode"] == "gone":
            return route.fulfill(status=200, content_type="application/x-ndjson", body=nd({"t": "fail", "why": "gone"}))
        return route.fulfill(status=200, content_type="application/x-ndjson", body=nd(
            {"t": "status", "v": "Проверяю орфографию, пунктуацию, грамматику и речь"}, {"t": "done", "result": RESULT}))
    if "/api/essay_check/edit" in url:
        b = json.loads(req.post_data); state["edit"].append(b)
        if b.get("audio"): return J({"ok": True, "op": "replace", "replacement": "книги", "say": "Поняла: заменю «кныги» на «книги».", "heard": "замени на книги, оно неправильно распознано"})
        if "иначе" in (b.get("text") or ""): return J({"ok": True, "op": "unclear", "replacement": "", "say": "На какое слово заменить?"})
        if "убери" in (b.get("text") or ""): return J({"ok": True, "op": "replace", "replacement": "", "say": "Поняла: уберу этот фрагмент."})
        if "оба" in (b.get("text") or ""): return J({"ok": True, "op": "replace", "replacement": "считает, что праздник", "say": "Поняла: поправлю фрагмент."})
        return J({"ok": True, "op": "replace", "replacement": "праздник", "say": "Поняла: заменю «празник» на «праздник»."})
    return J({})

INIT = """window.Telegram={WebApp:{ready:function(){},expand:function(){},initData:'',BackButton:{onClick:function(){},show:function(){},hide:function(){}},
  HapticFeedback:{impactOccurred:function(){},notificationOccurred:function(){}},showAlert:function(){},showConfirm:function(q,cb){cb(true)},showPopup:function(o,cb){cb&&cb('ok')}}};"""
def boot(b):
    pg = b.new_page(viewport={"width": 393, "height": 800}); errs = []
    pg.on("pageerror", lambda e: errs.append(str(e))); pg.route("**/*", handler); pg.add_init_script(INIT)
    pg.goto("http://app.local/index.html"); pg.wait_for_function("typeof L!=='undefined'&&Array.isArray(L)", timeout=15000); pg.wait_for_timeout(3600)
    return pg, errs
def ask(pg):
    pg.wait_for_timeout(900)      # кнопка гасит повторный тап в течение 0,8 с (click+touchend+pointerup — одно действие)
    pg.click("#ec-ask")
def words(pg): return pg.evaluate("[...document.querySelectorAll('#ec-pvt .ec-pw')].map(e=>e.textContent)")
def select(pg, a, b):
    pg.evaluate("""([a,b])=>{const w=document.querySelectorAll('#ec-pvt .ec-pw');const r=document.createRange();r.setStartBefore(w[a]);r.setEndAfter(w[b]);
        const g=getSelection();g.removeAllRanges();g.addRange(r)}""", [a, b])
    pg.wait_for_selector("#ec-ask:not([hidden])", timeout=3000)

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg, errs = boot(b)
    # --- этап 1 → экран «Текст проверен» ---
    pg.evaluate("tab='ai';ecKind='ege';ecShots={w:[{src:'x',set:'w',id:'p1',st:'ok'}],t:[]};ecSess='sess1';ecStg='ocr';ecPvTok=null;ecStep=2;ecState='run';ecLive=[];ecWait=ecStages();render();ecRun()")
    pg.wait_for_function("ecStep===5", timeout=8000); pg.wait_for_selector("#ec-pvt .ec-pw", timeout=3000)
    assert state["run"][0] == {"id": "sess1", "kind": "ege", "stage": "ocr"}, state["run"][0]
    assert pg.evaluate("ecState") == "prev" and "".join(words(pg)) == TEXT.replace(" ", "").replace("\n", "")
    assert pg.evaluate("document.querySelectorAll('#ec-pvt p').length") == 2, "абзацы сохраняются"
    assert pg.inner_text("#ec-pvdone") == "Текст проверен" and pg.evaluate("document.querySelector('#ec-pvdone').classList.contains('ec-go')")
    assert "Нажмите на слово или выделите фрагмент" in pg.inner_text(".ec-pvh")
    bg = pg.evaluate("getComputedStyle(document.querySelector('#ec-pvdone')).backgroundImage")
    assert "rgb" in bg, bg
    ok("после распознавания сразу показан печатный текст (абзацы целы) и зелёная кнопка «Текст проверен» в стиле приложения (.ec-go); проверка ошибок не запущена")
    pg.screenshot(path="/tmp/ec_preview_1_text.png")
    assert len(state["run"]) == 1, "второй этап не стартует сам"

    # --- нажатие на слово → «Спросить у Вишенки» ---
    pg.click('#ec-pvt .ec-pw[data-w="3"]'); pg.wait_for_selector("#ec-ask:not([hidden])", timeout=3000)
    assert pg.evaluate("String(getSelection())").strip() == "празник"
    pg.screenshot(path="/tmp/ec_preview_2_word.png")
    ok("нажатие на слово выделяет его и показывает плавающую кнопку «Спросить у Вишенки»")
    ask(pg); pg.wait_for_selector("#ec-pvrr", state="attached", timeout=3000)
    assert "празник" in pg.inner_text(".ecpv .fq") and "1 слово" in pg.inner_text(".ecpv .fq") and pg.inner_text(".ecpv h3") == "Спросить у Вишенки"
    pg.wait_for_timeout(900); pg.screenshot(path="/tmp/ec_preview_3_widget.png")
    ok("виджет Вишенки открывается с этим словом, подпись «1 слово · распознанный текст»")

    # --- правка текстом → ответ → «Применить» ---
    pg.fill("#ec-pvin", "замени на праздник, это неправильно распознано"); pg.click(".ec-pvin button")
    pg.wait_for_function("document.querySelector('#ec-pvact').classList.contains('cf')", timeout=5000)
    e = state["edit"][-1]
    assert e["id"] == "sess1" and e["fragment"] == "празник" and e["before"].endswith("считает что") and e["after"].startswith("важен") and "праздник" in e["text"], e
    assert "заменю «празник» на «праздник»" in pg.inner_text("#ec-pvrr")
    pg.wait_for_timeout(900); pg.screenshot(path="/tmp/ec_preview_4_answer.png")
    ok("правка текстом: на сервер уходят сеанс, слово, контекст до/после и слова ученика; ответ Вишенки и кнопки «Применить / Отмена»")
    pg.click(".act.cf .ok"); pg.wait_for_selector("#ec-pvrr", state="hidden", timeout=3000)
    assert "праздник" in words(pg) and "празник" not in words(pg) and words(pg).index("праздник") == 3, words(pg)
    assert pg.evaluate("ecPvText()").startswith("Автор считает что праздник важен для людей.\n\nЯ люблю")
    pg.screenshot(path="/tmp/ec_preview_5_applied.png")
    ok("«Применить»: слово подменено ровно на своём месте, абзацы и соседние слова целы")

    # --- «иначе»/неясно: ничего не меняется ---
    n0 = pg.evaluate("ecPvText()")
    pg.click('#ec-pvt .ec-pw[data-w="0"]'); pg.wait_for_selector("#ec-ask:not([hidden])"); ask(pg); pg.wait_for_selector("#ec-pvrr", state="attached")
    pg.fill("#ec-pvin", "поправь иначе"); pg.press("#ec-pvin", "Enter"); pg.wait_for_function("document.querySelector('#ec-pvrr').textContent.includes('На какое слово')", timeout=5000)
    assert not pg.evaluate("document.querySelector('#ec-pvact').classList.contains('cf')"), "при неясной просьбе «Применить» не появляется"
    pg.evaluate("ecPvClose()"); pg.wait_for_selector("#ec-pvrr", state="hidden"); assert pg.evaluate("ecPvText()") == n0
    ok("неясная просьба: вопрос Вишенки, «Применить» нет, закрытие шторки не меняет текст")

    # --- выделение фрагмента из нескольких слов + замена другим числом слов ---
    select(pg, 1, 3)
    ask(pg); pg.wait_for_selector("#ec-pvrr", state="attached")
    assert "3 слова" in pg.inner_text(".ecpv .fq"), pg.inner_text(".ecpv .fq")
    pg.fill("#ec-pvin", "исправь оба слова"); pg.click(".ec-pvin button")
    pg.wait_for_function("document.querySelector('#ec-pvact').classList.contains('cf')", timeout=5000)
    assert state["edit"][-1]["fragment"] == "считает что праздник"
    pg.click(".act.cf .ok"); pg.wait_for_selector("#ec-pvrr", state="hidden")
    assert pg.evaluate("ecPvText()").startswith("Автор считает, что праздник важен"), pg.evaluate("ecPvText()")
    ok("выделение нескольких слов: фрагмент уходит целиком, замена подставляется с другим числом слов")

    # --- голос ---
    pg.evaluate("""window.micTake=async()=>({stream:{}});
      window.MediaRecorder=class{constructor(s,o){this.mimeType='audio/webm;codecs=opus'}start(){this.t=1}
        stop(){this.ondataavailable({data:new Blob([new Uint8Array(4000)])});this.onstop()}};
      MediaRecorder.isTypeSupported=()=>true;""")
    wi = pg.evaluate("+[...document.querySelectorAll('#ec-pvt .ec-pw')].find(e=>e.textContent==='кныги').dataset.w")
    pg.click('#ec-pvt .ec-pw[data-w="%d"]' % wi); pg.wait_for_selector("#ec-ask:not([hidden])"); ask(pg); pg.wait_for_selector("#ec-pvrr", state="attached")
    pg.click("#ec-pvmic"); pg.wait_for_selector("#ec-pvmic.rec"); assert "Слушаю" in pg.inner_text("#ec-pvml")
    pg.wait_for_timeout(900); pg.screenshot(path="/tmp/ec_preview_6_listening.png")
    pg.wait_for_timeout(900); pg.click("#ec-pvmic")
    pg.wait_for_function("document.querySelector('#ec-pvact').classList.contains('cf')", timeout=5000)
    e = state["edit"][-1]
    assert e["mime"] == "audio/webm" and len(e["audio"]) > 100 and e["dur"] >= 0.6 and e["fragment"] == "кныги" and "text" not in e, {k: (v if k != "audio" else "…") for k, v in e.items()}
    assert "замени на книги" in pg.inner_text("#ec-pvhh") and "«кныги» на «книги»" in pg.inner_text("#ec-pvrr")
    pg.wait_for_timeout(900); pg.screenshot(path="/tmp/ec_preview_7_voice_answer.png")
    pg.click(".act.cf .ok"); pg.wait_for_selector("#ec-pvrr", state="hidden")
    assert "книги" in words(pg) and "кныги" not in words(pg)
    ok("голос: запись → на сервер уходит аудио (webm, длительность), расшифровка показана, ответ Вишенки, «Применить» меняет слово")

    # --- «Текст проверен» → этап 2 по поправленному тексту ---
    fixed = pg.evaluate("ecPvText()")
    pg.click("#ec-pvdone"); pg.wait_for_function("ecStep===3", timeout=8000)
    r2 = state["run"][-1]
    assert r2 == {"id": "sess1", "kind": "ege", "stage": "analyze", "text": fixed}, r2
    assert "празник" not in r2["text"] and "кныги" not in r2["text"] and "\n\n" in r2["text"] and "считает, что праздник" in r2["text"]
    assert pg.evaluate("ecRes.score.got") == 1 and pg.evaluate("ecRes.errors.length") == 1 and pg.evaluate("ecState") != "fail"
    assert pg.evaluate("!!document.querySelector('.ec-er, .ec-page, #ec-desk')"), "экран разбора отрисован"
    ok("«Текст проверен»: на второй этап уходит поправленный текст (абзацы целы), затем обычный экран разбора и критериев ФИПИ")
    pg.screenshot(path="/tmp/ec_preview_8_result.png")
    assert not errs, errs; pg.close()

    # --- «Назад», сбой второго этапа, «Повторить» ---
    pg, errs = boot(b)
    pg.evaluate("tab='ai';ecKind='ege';ecShots={w:[{src:'x',set:'w',id:'p1',st:'ok'}],t:[]};ecSess='sess2';ecStg='ocr';ecStep=2;ecState='run';ecLive=[];ecWait=ecStages();render();ecRun()")
    pg.wait_for_function("ecStep===5", timeout=8000)
    state["mode"] = "gone"; n_before = len(state["run"])
    pg.click("#ec-pvdone"); pg.wait_for_function("ecStep===3&&ecWhy==='gone'", timeout=8000)
    assert pg.evaluate("ecWhy") == "gone" and pg.evaluate("ecStg") == "ocr" and "устарел" in pg.inner_text("body")
    state["mode"] = "normal"; pg.evaluate("ecRetry()"); pg.wait_for_function("ecStep===5", timeout=8000)
    assert state["run"][-1]["stage"] == "ocr" and state["run"][n_before]["stage"] == "analyze"
    ok("данные этапа 1 пропали на сервере (gone): понятное сообщение, «Повторить» начинает с распознавания и снова даёт текст на проверку")
    pg.evaluate("ecBack()"); pg.wait_for_function("ecStep===1", timeout=4000)
    assert pg.evaluate("ecStg") == "ocr" and pg.evaluate("ecPvTok") is None
    ok("«Назад» с «Текст проверен» → к съёмке, состояние этапа сброшено")
    assert not errs, errs; pg.close()

    # --- пустой текст на этапе 1 → обычный экран сбоя ---
    pg, errs = boot(b)
    pg.evaluate("tab='ai';ecKind='ege';ecSess='s3';ecStg='ocr';ecStep=2;ecState='run';render();ecOcrDone({text:'   '})")
    pg.wait_for_function("ecStep===3&&ecWhy==='empty'", timeout=4000)
    ok("пустой текст на экране «Текст проверен» невозможен: сбой «не получилось разобрать текст»")
    assert not errs, errs; pg.close(); b.close()
print("OK")
