"""Логирование пайплайна «ИИ-проверка»: тайминги этапов, токены Kimi, источник критериев, OCR.
Сеть не вызывается: поток Kimi и сессия Yandex подменяются. Важно: в лог не попадает ни текст работы, ни ключи.
Запуск: python3 tests/test_essaycheck_logging.py"""
import asyncio, json, logging, os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", LOG_LEVEL="CRITICAL")
sys.path.insert(0, ROOT)
import essaycheck, kimi, yavision, fipicache
def ok(c): print("  ✓", c)

class Grab(logging.Handler):
    def __init__(self): super().__init__(logging.INFO); self.lines = []
    def emit(self, r): self.lines.append(r.getMessage())
grab = Grab()
root = logging.getLogger("tutor"); root.setLevel(logging.INFO); root.addHandler(grab)
SECRET = "Я считаю что книга важна СЕКРЕТНЫЙ_ФРАГМЕНТ"
def lines(prefix): return [l for l in grab.lines if l.startswith(prefix)]

# ---------- 1. kimi: чистые помощники usage ----------
u = {"prompt_tokens": 1200, "completion_tokens": 900, "completion_tokens_details": {"reasoning_tokens": 700},
     "prompt_tokens_details": {"cached_tokens": 300}}
assert kimi._usage_pick(u) == (1200, 900, 700, 300) and kimi._usage_pick(None) == (None,) * 4
assert kimi._usage_str(u) == "in=1200 out=900 reasoning=700 cached=300" and "in=-" in kimi._usage_str({})
st = {"t0": time.monotonic()}
kimi._usage_add(st, u); kimi._usage_add(st, u); kimi._usage_add(st, {"prompt_tokens": 5})
assert st["tok"] == [2405, 1800, 1400, 600], st["tok"]          # повторы и продолжения тоже стоят токенов
ok("kimi: usage суммируется по заходам, недостающие поля не ломают строку")

# ---------- 2. kimi.ask_stream пишет ровно одну строку на стрим (подмена aiohttp) ----------
SSE = [{"choices": [{"delta": {"reasoning_content": "думаю"}}]},
       {"choices": [{"delta": {"content": '{"errors":[]}'}}]},
       {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": u}]
class FakeResp:
    status = 200
    @property
    def content(self):
        async def gen():
            for j in SSE: yield ("data: " + json.dumps(j) + "\n").encode()
            yield b"data: [DONE]\n"
        return gen()
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
class FakeSession:
    def __init__(self, *a, **k): pass
    def post(self, *a, **k): return FakeResp()
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
real_cs, real_key = kimi.aiohttp.ClientSession, kimi.API_KEY
kimi.aiohttp.ClientSession, kimi.API_KEY = FakeSession, "test-key-123"
try:
    async def go():
        return [ev async for ev in kimi.ask_stream([{"role": "user", "content": SECRET}], json_mode=True,
                                                    thinking=True, max_tokens=65536, tag="ec-criteria")]
    evs = asyncio.run(go())
finally:
    kimi.aiohttp.ClientSession, kimi.API_KEY = real_cs, real_key
assert [k for k, _ in evs] == ["think", "text"], evs
ln = lines("kimi: stream")
assert len(ln) == 1, ln
for part in ("[ec-criteria]", "thinking=True", "max_tokens=65536", "calls=1", "finish=stop",
             "tokens in=1200 out=900 reasoning=700 cached=300"):
    assert part in ln[0], (part, ln[0])
assert "first_think=-" not in ln[0] and "first_text=-" not in ln[0], ln[0]
ok("kimi.ask_stream: одна строка на стрим — метка этапа, лимит, 1й токен мышления/ответа, токены")

# ---------- 2б. продолжение после обрыва и сбой HTTP: заходы суммируются, err виден в строке ----------
class SResp:
    def __init__(self, status, sse, raw=""): self.status, self._sse, self._raw = status, sse, raw
    async def text(self): return self._raw
    @property
    def content(self):
        async def gen():
            for j in self._sse: yield ("data: " + json.dumps(j) + "\n").encode()
            yield b"data: [DONE]\n"
        return gen()
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
def scripted(responses):
    it = iter(responses)
    class S:
        def __init__(self, *a, **k): pass
        def post(self, *a, **k): return next(it)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    return S
def run_stream(responses, **kw):
    real = kimi.aiohttp.ClientSession, kimi.API_KEY
    kimi.aiohttp.ClientSession, kimi.API_KEY = scripted(responses), "test-key-123"
    try:
        async def g(): return [ev async for ev in kimi.ask_stream([{"role": "user", "content": SECRET}], **kw)]
        return asyncio.run(g())
    finally:
        kimi.aiohttp.ClientSession, kimi.API_KEY = real
n0 = len(lines("kimi: stream"))
evs = run_stream([SResp(200, [{"choices": [{"delta": {"content": "abc"}, "finish_reason": "length"}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}}]),
                  SResp(200, [{"choices": [{"delta": {"content": "def"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 160, "completion_tokens": 30}}])],
                 json_mode=True, tag="cont")
assert "".join(v for k, v in evs if k == "text") == "abcdef", evs
ln = lines("kimi: stream")[n0:]
assert len(ln) == 1 and "[cont]" in ln[0] and "calls=2" in ln[0] and "cont=1" in ln[0] and "in=260 out=80" in ln[0], ln
evs = run_stream([SResp(500, [], raw="boom")], tag="http")
ln = lines("kimi: stream")[n0 + 1:]
assert evs == [("err", "http")] and len(ln) == 1 and "err=http" in ln[0] and "calls=1" in ln[0], (evs, ln)
ok("kimi.ask_stream: продолжение (calls=2 cont=1, токены суммой) и сбой HTTP (err=http) — по одной строке")

# ---------- 3. yavision._recognize_one: время, попытки, слова ----------
class YResp:
    status = 200
    async def json(self, content_type=None): return {"x": 1}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
class YSession:
    def post(self, *a, **k): return YResp()
real_parse, real_ykey = yavision.parse, yavision.API_KEY
yavision.parse, yavision.API_KEY = (lambda j: {"text": SECRET, "words": [{"t": "Я"}, {"t": "считаю"}]}), "ykey-xyz"
try:
    res = asyncio.run(yavision._recognize_one(YSession(), asyncio.Semaphore(1), "AAAA"))
finally:
    yavision.parse, yavision.API_KEY = real_parse, real_ykey
ln = lines("yavision: фото распознано")
assert len(ln) == 1 and "попыток=1" in ln[0] and "слов=2" in ln[0] and "dt=" in ln[0], ln
ok("yavision: «фото распознано dt=… попыток=1 слов=2»")

# ---------- 4. fipicache: источник критериев и время ----------
real_load, real_enabled = fipicache._load, kimi.enabled
fipicache._load = lambda kind: {"text": "К1 Отражение позиции автора — 1 балл. К2 … К10", "ts": time.time()}
try:
    t = asyncio.run(fipicache.criteria("ege", "FALLBACK"))
finally:
    fipicache._load = real_load
ln = lines("fipicache: критерии ege источник=")
assert t.startswith("К1") and len(ln) == 1 and "источник=кэш " in ln[0] and "dt=" in ln[0], (t, ln)
ok("fipicache: «критерии ege источник=кэш dt=…»")

# ---------- 5. essaycheck.run: итоговая строка этапов, без текста работы ----------
essaycheck.MIN_WORDS = {"ege": 0, "oge": 0}
PAGES = [{"text": SECRET, "words": [{"t": t, "x1": i * 10, "y1": 5, "x2": i * 10 + 9, "y2": 10}
                                    for i, t in enumerate(SECRET.split())]}]
ERR_JSON = json.dumps({"errors": [{"type": "Пунктуация", "found": "считаю что", "correct": "считаю, что",
                                   "why": "запятая", "src": "Розенталь, §1"}]})
CR = [{"name": n, "got": g, "max": m, "why": "ок", "tip": ""} for n, g, m in
      (("К1 Отражение позиции автора", 1, 1), ("К2 Комментарий к позиции", 3, 3), ("К3 Собственное отношение", 2, 2),
       ("К4 Фактическая точность", 1, 1), ("К5 Логичность", 2, 2), ("К6 Этические нормы", 1, 1))]
CRIT_JSON = json.dumps({"flags": {"copied": False}, "criteria": CR, "recommendations": []})
async def fake_recognize(imgs): return PAGES
async def fake_stream(messages, **kw):
    is_err = "Найди ВСЕ ошибки" in messages[-1]["content"]
    await asyncio.sleep(0.05)
    if not is_err: yield "think", "думаю"
    yield "text", ERR_JSON if is_err else CRIT_JSON
async def fake_criteria(kind, fallback=""): return fallback
orig = (essaycheck.yavision.recognize_many, essaycheck.kimi.ask_stream, essaycheck.fipicache.criteria)
essaycheck.yavision.recognize_many, essaycheck.kimi.ask_stream, essaycheck.fipicache.criteria = (
    fake_recognize, fake_stream, fake_criteria)
try:
    async def go2(): return [ev async for ev in essaycheck.run(["w1"], [], "ege")]
    asyncio.run(go2())
finally:
    essaycheck.yavision.recognize_many, essaycheck.kimi.ask_stream, essaycheck.fipicache.criteria = orig
ln = lines("essaycheck: проверка")
assert len(ln) == 1, ln
for part in ("kind=ege", "итого=", "ocr=", "ошибки=", "критерии=", "мышление", "привязка=", "ошибок=1",
             "'Пунктуация': 1", "критериев=10", "балл=", "why=-"):
    assert part in ln[0], (part, ln[0])
assert lines("essaycheck: OCR и критерии готовы") and "слов=" in lines("essaycheck: OCR и критерии готовы")[0]
ok("essaycheck.run: одна итоговая строка (ocr / ошибки / критерии / привязка / баллы) и строка про OCR")

# ---------- 6. приватность: ни текста работы, ни ключей в логах ----------
blob = "\n".join(grab.lines)
assert "СЕКРЕТНЫЙ_ФРАГМЕНТ" not in blob and "считаю" not in blob, "текст работы попал в лог"
assert "test-key-123" not in blob and "ykey-xyz" not in blob, "ключ попал в лог"
ok("в логи не попадают ни текст работы, ни ключи API")
print("test_essaycheck_logging: все проверки прошли")
