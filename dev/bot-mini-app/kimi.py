"""Клиент Kimi API (Moonshot AI) — OpenAI-совместимый Chat Completions.

Подключение — переменными окружения (см. .env.example):
  KIMI_API_KEY    — ключ с https://platform.kimi.ai/ (без него ассистент отвечает заглушкой)
  KIMI_BASE_URL   — по умолчанию https://api.moonshot.ai/v1; для Китая — https://api.moonshot.cn/v1
  KIMI_MODEL      — «слабая» модель быстрых ответов, по умолчанию kimi-k2.6
  KIMI_MODEL_FAST — альтернативное имя слабой модели (резерв), по умолчанию kimi-k2.6
  KIMI_MODEL_DEEP — сильная модель «глубокого изучения», по умолчанию kimi-k3
  KIMI_EFFORT     — режим размышления kimi-k3: low / high / max (по умолчанию max)
  KIMI_MAX_TOKENS / KIMI_MAX_TOKENS_DEEP — ЯВНЫЙ лимит генерации (см. ниже). По умолчанию пусто.
  KIMI_MAX_TOKENS_CEILING — «бесконечный» бюджет, по умолчанию 100 000 000
  KIMI_THINK_EXTRA — надбавка к бюджету на размышления, по умолчанию 0
  KIMI_TIMEOUT    — потолок времени обычного запроса, по умолчанию 600 с
  KIMI_TIMEOUT_DEEP — потолок «глубокого» запроса, по умолчанию 3600 с
  KIMI_SOCK_READ  — сколько секунд ТИШИНЫ в потоке считать обрывом, по умолчанию 300
  KIMI_CONNECT    — таймаут установки соединения, по умолчанию 30

ВАЖНО ПРО ЛИМИТ ТОКЕНОВ (это и была причина «ИИ сейчас не отвечает»):
  `max_tokens` у Kimi ограничивает СУММУ `reasoning_content` + `content`
  (https://platform.kimi.ai/docs/guide/use-thinking-models). Если поле НЕ передать,
  сервер применит свой небольшой дефолт, и у kimi-k3 с reasoning_effort=max он целиком
  сгорает на размышлениях → контент пустой → пользователь видит «ИИ сейчас не отвечает».
  Поэтому поле передаётся ВСЕГДА и всегда с огромным бюджетом: по умолчанию 100 000 000.
  Если конкретный деплой API откажет в таком числе (у каждой модели свой потолок вывода),
  клиент сам подберёт МАКСИМУМ, который этот API принимает, и запомнит его — лимит с
  нашей стороны никогда не режет ответ.

Важно: Kimi принимает текст, картинки и видео — ГОЛОС НЕ РАСПОЗНАЁТ (audio-входа нет):
распознаванием речи занимается отдельный STT (см. whisper.py).

Просьба «не падать» выполнена буквально: любая ошибка (нет ключа, сеть, таймаут,
неожиданный формат ответа) — это None из ask() и запись в лог, а не исключение наверх.
"""
import json, logging, os, re

import aiohttp

log = logging.getLogger("tutor.kimi")

BASE = (os.getenv("KIMI_BASE_URL", "").strip() or "https://api.moonshot.ai/v1").rstrip("/")  # пустое значение = дефолт
API_KEY = os.getenv("KIMI_API_KEY", "").strip()
MODEL = (os.getenv("KIMI_MODEL", "") or "kimi-k2.6").strip()
FAST_MODEL = (os.getenv("KIMI_MODEL_FAST", "") or "kimi-k2.6").strip()
DEEP_MODEL = (os.getenv("KIMI_MODEL_DEEP", "") or "kimi-k3").strip()
EFFORT = (os.getenv("KIMI_EFFORT", "") or "max").strip()
if EFFORT not in ("low", "high", "max"): EFFORT = "max"


def _int(name, default, lo, hi):
    try: v = int(os.getenv(name, "") or default)
    except ValueError: v = default
    return max(lo, min(hi, v))

def _flt(name, default, lo, hi):
    try: v = float(os.getenv(name, "") or default)
    except ValueError: v = default
    return max(lo, min(hi, v))

def _opt_int(name):
    v = os.getenv(name, "").strip()
    try:
        return int(v) if v else None
    except ValueError:
        return None

# Бюджет генерации. ПО УМОЛЧАНИЮ ОГРАНИЧЕНИЙ НЕТ: max_tokens выставляется в «бесконечный»
# потолок (100 000 000), а не пропускается — пропуск поля включает серверный дефолт,
# который сгорает на размышлениях. Явно ограничить можно только через env.
CEILING_DEFAULT = 100_000_000
MAX_TOKENS_CEILING = _opt_int("KIMI_MAX_TOKENS_CEILING") or CEILING_DEFAULT
MAX_TOKENS = _opt_int("KIMI_MAX_TOKENS")          # явный лимит обычного ответа (пусто = без лимита)
MAX_TOKENS_DEEP = _opt_int("KIMI_MAX_TOKENS_DEEP")  # явный лимит «глубокого» ответа (пусто = без лимита)
THINK_EXTRA = _opt_int("KIMI_THINK_EXTRA") or 0
TIMEOUT = _flt("KIMI_TIMEOUT", 600, 3, 7200)
TIMEOUT_DEEP = _flt("KIMI_TIMEOUT_DEEP", 3600, 3, 21600)
SOCK_READ = _flt("KIMI_SOCK_READ", 300, 5, 3600)
CONNECT = _flt("KIMI_CONNECT", 30, 3, 300)


def stream_timeout():
    """Таймаут для СТРИМА. Общего потолка времени НЕТ умышленно: размышление kimi-k3
    (reasoning_effort=max) легко идёт 5–10 минут, и любой общий таймаут просто убивает
    соединение на середине — пользователь видит «Вишенка думает…» и затем «ИИ сейчас
    не отвечает». Это и была вторая причина обрыва. Обрыв связи ловится иначе — по
    тишине в потоке (sock_read): если данные вообще не приходят, запрос не виснет вечно."""
    return aiohttp.ClientTimeout(total=None, sock_connect=CONNECT, sock_read=SOCK_READ)


def request_timeout(total):
    """Таймаут для НЕстримового запроса: тут общий потолок уместен (ответ приходит целиком)."""
    return aiohttp.ClientTimeout(total=total, sock_connect=CONNECT, sock_read=SOCK_READ)

# Потолок max_tokens, который принимает конкретный API/модель. Запоминается после первого
# успешного запроса, чтобы не дёргать сервер лишними 400 на каждом вызове.
_ACCEPTED = {}
# Лестница подбора, если API отверг бюджет и в ошибке нет явного максимума.
_LADDER = (CEILING_DEFAULT, 16_777_216, 4_194_304, 1_048_576, 262_144,
           131_072, 65_536, 32_768, 16_384, 8_192, 4_096)
_NUM_RE = re.compile(r"\d[\d_ ]*\d|\d")


def enabled():
    """Есть ли ключ API. Без него ассистент работает заглушкой и наружу не ходит."""
    return bool(API_KEY)


def resolve_model(model=None, deep=False):
    """Какая модель будет использована СТРОГО: явно переданная, иначе выбранная режимом.
    Никаких подмен и аварийных откатов на другую модель здесь и ниже нет."""
    return (model or (DEEP_MODEL if deep else MODEL)).strip()


def budget_for(deep=False, reasoning=False, override=None):
    """Бюджет max_tokens для запроса. Всегда > 0: поле уходит в запрос ОБЯЗАТЕЛЬНО.

    • override — явный бюджет (повтор запроса после отказа API);
    • иначе KIMI_MAX_TOKENS[_DEEP], если заданы в окружении;
    • иначе «бесконечный» потолок (по умолчанию 100 000 000)."""
    if override is not None and override > 0:
        return int(override)
    base = (MAX_TOKENS_DEEP if deep else MAX_TOKENS) or MAX_TOKENS_CEILING
    return int(base) + (THINK_EXTRA if reasoning else 0)


def _cap_key(model):
    return "%s|%s" % (BASE, model)


def _clamp_to_known(model, budget):
    acc = _ACCEPTED.get(_cap_key(model))
    return min(budget, acc) if acc else budget


def _remember(model, budget):
    if budget and budget > _ACCEPTED.get(_cap_key(model), 0):
        _ACCEPTED[_cap_key(model)] = budget


def _cap_from_error(text, want):
    """Максимум max_tokens, который API сам объявил в ошибке («… must be <= 65536»)."""
    best = None
    for raw in _NUM_RE.findall(text or ""):
        try:
            n = int(raw.replace("_", "").replace(" ", ""))
        except ValueError:
            continue
        if 4096 <= n < want and (best is None or n > best):
            best = n
    return best


def _smaller_budget(model, budget, err_text=""):
    """Следующий (меньший) бюджет после отказа API, либо None — дальше снижать некуда."""
    cap = _cap_from_error(err_text, budget)
    if cap:
        _ACCEPTED[_cap_key(model)] = cap
        return cap
    for n in _LADDER:
        if n < budget:
            _ACCEPTED[_cap_key(model)] = n
            return n
    return None


def build_payload(messages, model=None, deep=False, json_mode=False, images=None, thinking=False, max_tokens=None):
    """Тело запроса /chat/completions.

    • temperature/top_p не передаём: у K2.x и K3 они фиксированы, API отвечает ошибкой
      на любые другие значения.
    • Модель выбирается СТРОГО по режиму: deep → KIMI_MODEL_DEEP, иначе KIMI_MODEL.
    • thinking — параметр только семейства K2.x; строго следует выбранному режиму:
      быстрый ответ — размышления выключены, глубокий — включены. У K3 размышление
      всегда включено и управляется reasoning_effort (режим K3), поле thinking K3 не принимает.
    • json_mode — ответ строго в JSON (response_format json_object): используется для разбора задач.
    • images — список base64/jpeg|png|webp: подмешиваются в последнее сообщение (vision)."""
    m = resolve_model(model, deep)
    content = messages[-1]["content"]
    if images:
        parts = [{"type": "text", "text": content if isinstance(content, str) else ""}]
        for i in images:
            parts.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + i}})
        content = parts
    msgs = list(messages[:-1]) + [{**messages[-1], "content": content}]
    reasoning = bool(thinking) or m.startswith("kimi-k3")
    budget = budget_for(deep, reasoning, max_tokens)
    if max_tokens is None:                       # явный бюджет (повтор после отказа) не трогаем
        budget = _clamp_to_known(m, budget)
    p = {"model": m, "messages": msgs,
         # Лимит передаётся ВСЕГДА: без поля сервер ставит свой маленький дефолт,
         # и K3 тратит его целиком на размышления (контент остаётся пустым).
         "max_tokens": budget}
    if m.startswith("kimi-k2.6"):
        p["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if m.startswith("kimi-k3"):
        p["reasoning_effort"] = EFFORT
    if json_mode:
        p["response_format"] = {"type": "json_object"}
    return p


async def ask(messages, model=None, deep=False, json_mode=False, images=None, max_tokens=None, _attempt=0):
    """Ответ модели (str) или None — нет ключа / API недоступен / формат ответа неожиданный."""
    if not API_KEY:
        return None
    m = resolve_model(model, deep)
    budget = budget_for(deep, m.startswith("kimi-k3"), max_tokens)
    if max_tokens is None:
        budget = _clamp_to_known(m, budget)
    attempt, drops = 0, 0
    while True:
        body = build_payload(messages, model=m, deep=deep, json_mode=json_mode, images=images,
                             max_tokens=budget)
        timeout = TIMEOUT_DEEP if deep else TIMEOUT
        try:
            async with aiohttp.ClientSession(timeout=request_timeout(timeout)) as s:
                async with s.post(BASE + "/chat/completions", json=body,
                                  headers={"Authorization": "Bearer " + API_KEY}) as r:
                    raw = await r.text()
                    if r.status != 200:
                        log.warning("kimi: HTTP %s: %s", r.status, raw[:300])
                        if r.status == 400 and "max_tokens" in raw:
                            nb = _smaller_budget(m, budget, raw)
                            if nb is not None and drops < 6:   # защита от бесконечного пересогласования
                                drops += 1
                                budget = nb
                                continue          # повтор с бюджетом, который API примет
                        return None
        except Exception as e:                    # сеть, таймаут — всё мимо бота
            log.warning("kimi: запрос не прошёл: %r", e)
            return None
        try:
            j = json.loads(raw)
        except ValueError:
            log.warning("kimi: не JSON в ответе: %s", raw[:300])
            return None
        ch = (j.get("choices") or [{}])[0]
        reply = (ch.get("message") or {}).get("content")
        if not isinstance(reply, str) or not reply.strip():
            # бюджет сгорел на размышлениях (finish_reason=length) — повторяем с тем же
            # огромным бюджетом: модель почти всегда доходит до ответа со второго раза
            log.warning("kimi: пустой ответ (finish=%s usage=%s max_tokens=%s)",
                        ch.get("finish_reason"), j.get("usage"), body.get("max_tokens"))
            if attempt == 0:
                attempt += 1
                continue
            return None
        _remember(m, budget)
        return reply.strip()


async def ask_stream(messages, model=None, deep=False, json_mode=False, images=None, thinking=False,
                     max_tokens=None, _attempt=0):
    """Стриминг ответа модели: async-генератор пар (тип, дельта).

    Типы: «think» — кусочек размышления (reasoning_content), «text» — кусочек ответа,
    «err» — сбой («off» — нет ключа, «http» — API отказал, «net» — сеть/таймаут,
    «empty» — модель не вернула ни одного кусочка ответа); после err генератор завершается.
    Мысль идёт отдельными дельтами — её показывают «от имени Вишенки» («Вишенка думает…»).

    Модель и режим НЕ МЕНЯЮТСЯ ни на одном шаге: повтор идёт строго с той же моделью
    и тем же режимом, что и первый заход (требование: «выбрана K3 — работает K3»).

    Если стрим оборвался без ответа (бюджет сгорел на размышлениях, обрыв соединения),
    запрос повторяется один раз с тем же огромным бюджетом, но мышление при повторе уже
    не показывается — пользователь не видит дублей «Вишенка думает»."""
    if not API_KEY:
        yield "err", "off"
        return
    m = resolve_model(model, deep)
    reasoning = bool(thinking) or m.startswith("kimi-k3")
    budget = budget_for(deep, reasoning, max_tokens)
    if max_tokens is None:
        budget = _clamp_to_known(m, budget)
    attempt, drops = 0, 0
    while True:
        body = build_payload(messages, model=m, deep=deep, json_mode=json_mode, images=images,
                             thinking=thinking, max_tokens=budget)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}    # usage в конце стрима — видно, сгорел ли бюджет
        finish, usage, got_text, broke = None, None, False, False
        try:
            async with aiohttp.ClientSession(timeout=stream_timeout()) as s:
                async with s.post(BASE + "/chat/completions", json=body,
                                  headers={"Authorization": "Bearer " + API_KEY}) as r:
                    if r.status != 200:
                        raw = await r.text()
                        log.warning("kimi: HTTP %s: %s", r.status, raw[:300])
                        if r.status == 400 and "max_tokens" in raw:
                            nb = _smaller_budget(m, budget, raw)
                            if nb is not None and drops < 6:   # защита от бесконечного пересогласования
                                drops += 1
                                budget = nb
                                continue          # повтор с бюджетом, который API примет
                        yield "err", "http"
                        return
                    async for line in r.content:
                        s_line = line.decode("utf-8", "replace").strip()
                        if not s_line.startswith("data:"):
                            continue
                        data = s_line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            j = json.loads(data)
                        except ValueError:
                            continue
                        if j.get("usage"):
                            usage = j["usage"]
                        ch = (j.get("choices") or [{}])[0]
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
                        d = ch.get("delta") or {}
                        th, tx = d.get("reasoning_content"), d.get("content")
                        if th and attempt == 0:
                            yield "think", th
                        if tx:
                            got_text = True
                            yield "text", tx
        except Exception as e:                    # сеть, таймаут, кривой SSE — всё мимо бота
            log.warning("kimi: стрим не прошёл: %r", e)
            broke = True
        if got_text:
            _remember(m, budget)
            return                              # частичный ответ уже показан — не трогаем
        # стрим дошёл до конца без единой дельты ответа: почти всегда бюджет max_tokens
        # сгорел на размышлениях (finish_reason=length) либо соединение оборвалось
        log.warning("kimi: стрим без ответа (finish=%s usage=%s max_tokens=%s broke=%s)",
                    finish, usage, body.get("max_tokens"), broke)
        if attempt == 0:
            attempt += 1
            continue                            # повтор строго той же моделью и режимом
        yield "err", ("net" if broke else "empty")
        return


async def balance():
    """Баланс аккаунта Moonshot/Kimi (dict с available_balance/voucher_balance/cash_balance) или None.

    Деньги считаются в юанях (CNY) — как в личном кабинете platform.kimi.ai.
    Использует тот же KIMI_API_KEY, что и все запросы модели."""
    if not API_KEY:
        return None
    try:
        async with aiohttp.ClientSession(timeout=request_timeout(TIMEOUT)) as s:
            async with s.get(BASE + "/users/me/balance",
                             headers={"Authorization": "Bearer " + API_KEY}) as r:
                raw = await r.text()
                if r.status != 200:
                    log.warning("kimi: balance HTTP %s: %s", r.status, raw[:300])
                    return None
        j = json.loads(raw)
        d = j.get("data") if isinstance(j, dict) else None
        return d if isinstance(d, dict) else None
    except Exception as e:
        log.warning("kimi: баланс не получился: %r", e)
        return None


async def ask_json(messages, **kw):
    """Ответ как dict (модель отвечает строгим JSON) или None. Просит JSON через response_format
    и дополнительно проверяет разбор — кривой JSON не должен ронять конвейер."""
    raw = await ask(messages, json_mode=True, **kw)
    if raw is None:
        return None
    try:
        j = json.loads(raw)
    except ValueError:
        log.warning("kimi: ответ не разобрался как JSON: %s", raw[:300])
        return None
    return j if isinstance(j, dict) else None
