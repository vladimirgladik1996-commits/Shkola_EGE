"""Инструменты поиска в чате (webtools.py + tool_calls в kimi.py): накопитель tool_calls из стрима,
исполнение и возврат role=tool, цикл «модель просит инструмент → получает результат → отвечает».
Сеть подменена. Запуск: python3 tests/test_webtools.py
"""
import asyncio, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(KIMI_API_KEY="123456:TESTKEY", KIMI_WEBTOOLS="1", LOG_LEVEL="CRITICAL")
for k in ("KIMI_BASE_URL",):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import kimi, webtools


def ok(m):
    print("  ✓", m)


# --- накопитель tool_calls: id/имя/аргументы приходят кусками по index ---
acc = {}
kimi._tc_add(acc, [{"index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "web_search", "arguments": ""}}])
kimi._tc_add(acc, [{"index": 0, "function": {"arguments": "{\"query\": \"ФИПИ\"}"}}])
kimi._tc_add(acc, [{"index": 1, "id": "call_2", "function": {"name": "fetch", "arguments": "{\"url\": \"https://fipi.ru\"}"}}])
calls = kimi._tc_list(acc)
assert len(calls) == 2 and calls[0]["id"] == "call_1"
assert calls[0]["function"] == {"name": "web_search", "arguments": "{\"query\": \"ФИПИ\"}"}
assert calls[1]["function"]["name"] == "fetch"; ok("tool_calls собираются из дельт по index (2 вызова, аргументы склеены)")
assert kimi._tc_list({}) == [] and kimi._tc_list({0: {"id": "", "name": "", "args": []}}) == []; ok("пустой накопитель → []")

# --- исполнение tool_calls: сбой не роняет цикл, а превращается в строку ---
async def good_exec(name, args):
    return "РЕЗУЛЬТАТ: " + name

async def bad_exec(name, args):
    raise RuntimeError("net down")

async def tool_results():
    okr = await kimi._tool_results(calls, good_exec)
    bad = await kimi._tool_results(calls, bad_exec)
    return okr, bad

okr, bad = asyncio.run(tool_results())
assert [m["role"] for m in okr] == ["tool", "tool"]
assert okr[0] == {"role": "tool", "tool_call_id": "call_1", "content": "РЕЗУЛЬТАТ: web_search"}
assert bad[0]["content"] == "Инструмент недоступен — ответь без него."; ok("результаты → role=tool; сбой инструмента — строкой, не исключением")

# --- build_payload: tools уходят в запрос, картинки к role=tool не подмешиваются ---
tools_decl = [{"type": "function", "function": {"name": "web_search"}}]
p = kimi.build_payload([{"role": "user", "content": "привет"}], tools=tools_decl)
assert p["tools"] == tools_decl and "tools" not in kimi.build_payload([{"role": "user", "content": "x"}]); ok("build_payload: tools передаётся только когда задан")
p = kimi.build_payload([{"role": "assistant", "content": "", "tool_calls": calls},
                        {"role": "tool", "tool_call_id": "call_1", "content": "Р"}], images=["QQ=="])
assert p["messages"][-1]["content"] == "Р"; ok("картинки не подмешиваются к сообщениям role=tool")

# --- webtools: имена, результат fibers, угадывание формулы ---
assert webtools._tool_name({"type": "function", "function": {"name": "web_search"}}) == "web_search"
assert webtools._tool_name({"name": "fetch"}) == "fetch"; ok("_tool_name читает оба формата деклараций")
assert webtools._result_text({"context": {"output": "текст"}}) == "текст"
assert "ENCRYPTED" in webtools._result_text({"context": {"encrypted_output": "----MOONSHOT ENCRYPTED BEGIN----x"}})
assert webtools._result_text({}) == ""; ok("_result_text: output / encrypted_output / пусто")
assert webtools._guess_uri("web_search") == webtools.FORMULAS[0] and webtools._guess_uri("fetch") == webtools.FORMULAS[1]
assert webtools._guess_uri("nope") is None; ok("_guess_uri: web_search/fetch → формулы, иное → None")

# --- declarations: кэш и устойчивость к сбоям (сеть подменена) ---
async def fake_fetch(s):
    return ([{"type": "function", "function": {"name": "web_search"}},
             {"type": "function", "function": {"name": "fetch"}}],
            {"web_search": webtools.FORMULAS[0], "fetch": webtools.FORMULAS[1]})

async def fail_fetch(s):
    raise RuntimeError("no net")

real_fetch = webtools._fetch_all
webtools._fetch_all = fake_fetch
d1 = asyncio.run(webtools.declarations())
d2 = asyncio.run(webtools.declarations())
assert d1 == d2 and len(d1) == 2; ok("declarations: декларации загружаются и кэшируются")
assert webtools._cache["by_name"]["web_search"] == webtools.FORMULAS[0]; ok("имена инструментов привязаны к своим формулам")
webtools._fetch_all = fail_fetch
webtools._cache["tools"] = None                     # «перезапуск»: сбой загрузки → None без исключения
assert asyncio.run(webtools.declarations()) is None
assert asyncio.run(webtools.declarations()) is None; ok("после сбоя повтор не раньше RETRY_ON_FAIL, ничего не падает")
webtools._fetch_all = real_fetch

# --- сквозной цикл ask_stream: модель просит web_search → получает результат → отвечает текстом ---
ROUNDS = [["data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"function\":{\"name\":\"web_search\",\"arguments\":\"\"}}]}}]}",
           "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\"{\\\"query\\\": \\\"ФИПИ\\\"}\"}}]}}]}",
           "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"tool_calls\"}]}",
           "data: [DONE]"],
          ["data: {\"choices\":[{\"delta\":{\"content\":\"ответ по свежим данным\"}}]}",
           "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}",
           "data: [DONE]"]]
SENT = []


class FakeResp:
    status = 200

    def __init__(self, lines):
        self._lines, self.content = lines, self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for line in self._lines:
            yield line.encode("utf-8") if isinstance(line, str) else line

    async def text(self):
        return ""


class FakeSession:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):
        SENT.append(json)
        return FakeResp(ROUNDS.pop(0))


real_session = kimi.aiohttp.ClientSession
kimi.aiohttp.ClientSession = FakeSession
EXEC_LOG = []


async def exec_log(name, args):
    EXEC_LOG.append((name, args))
    return "выдача по запросу ФИПИ"

async def run_stream():
    ev = []
    async for pair in kimi.ask_stream([{"role": "user", "content": "какие требования ФИПИ?"}],
                                      tools=tools_decl, tool_exec=exec_log):
        ev.append(pair)
    return ev

ev = asyncio.run(run_stream())
kimi.aiohttp.ClientSession = real_session
assert EXEC_LOG == [("web_search", "{\"query\": \"ФИПИ\"}")]; ok("модель сама запросила web_search — инструмент исполнен с её аргументами")
assert ev == [("tool", "web_search"), ("text", "ответ по свежим данным")]; ok("tool-заход даёт маркер «tool» + ответ модели после инструмента (done добавляет chat_stream)")
assert len(SENT) == 2 and SENT[0]["tools"] == tools_decl
hist = SENT[1]["messages"]
assert hist[-2]["role"] == "assistant" and hist[-2]["tool_calls"][0]["id"] == "call_1"
assert hist[-1] == {"role": "tool", "tool_call_id": "call_1", "content": "выдача по запросу ФИПИ"}
assert hist[0] == {"role": "user", "content": "какие требования ФИПИ?"}; ok("история: assistant с tool_calls + role=tool уходят в следующий запрос целиком")

# --- лимит заходов: после последнего tool_call — финал БЕЗ инструментов, «Поищу…» не в ответе ---
ROUNDS.extend([["data: {\"choices\":[{\"delta\":{\"content\":\"Поищу свежие требования…\",\"tool_calls\":[{\"index\":0,\"id\":\"call_9\",\"function\":{\"name\":\"web_search\",\"arguments\":\"{}\"}}]}}]}",
                "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"tool_calls\"}]}",
                "data: [DONE]"],
               ["data: {\"choices\":[{\"delta\":{\"content\":\"ФИПИ требует 200 слов и аргументы\"}}]}",
                "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}",
                "data: [DONE]"]])
EXEC_LOG2 = []


async def exec_log2(name, args):
    EXEC_LOG2.append(name)
    return "данные с fipi.ru"


real_limit = kimi.MAX_TOOL_ROUNDS
kimi.MAX_TOOL_ROUNDS = 1
kimi.aiohttp.ClientSession = FakeSession


async def run_stream2():
    ev = []
    async for pair in kimi.ask_stream([{"role": "user", "content": "требования?"}],
                                      tools=tools_decl, tool_exec=exec_log2):
        ev.append(pair)
    return ev


ev2 = asyncio.run(run_stream2())
kimi.aiohttp.ClientSession = real_session
kimi.MAX_TOOL_ROUNDS = real_limit
assert EXEC_LOG2 == ["web_search"]; ok("лимит: последний tool_call всё же исполняется")
assert ev2 == [("text", "Поищу свежие требования…"), ("tool", "web_search"),
               ("text", "ФИПИ требует 200 слов и аргументы")]; ok("маркер «tool» отделяет служебные «Поищу…» от финального текста")
assert "tools" not in SENT[3]; ok("после лимита инструменты убраны из запроса")
assert "Инструменты поиска больше недоступны" in SENT[3]["messages"][-1]["content"]; ok("в историю добавлен промт «дай финальный ответ по полученным данным»")

# --- assist.chat_stream: done = чистый финал, служебные «Поищу…» не входят в ответ ---
os.environ.update(BOT_TOKEN="***", PUBLIC_URL="https://bot.example.com", AUTH_LOGIN="Admin")
import assist, webtools as wt


async def no_decls():
    return None


async def fake_tool_stream(messages, model=None, deep=False, json_mode=False, images=None,
                           thinking=False, max_tokens=None, tools=None, tool_exec=None,
                           _attempt=0, tag=""):
    yield "text", "Поищу актуальные требования…"
    yield "tool", "web_search"
    yield "text", "ФИПИ требует 200 слов"


real_decls, real_ask = wt.declarations, kimi.ask_stream
wt.declarations, kimi.ask_stream = no_decls, fake_tool_stream


async def run_chat():
    return [pair async for pair in assist.chat_stream([{"role": "user", "content": "требования ФИПИ?"}])]


ev3 = asyncio.run(run_chat())
wt.declarations, kimi.ask_stream = real_decls, real_ask
assert ("done", "ФИПИ требует 200 слов") in ev3 and ("done", "Поищу актуальные требования…") not in ev3
ok("done отдаёт только финальный ответ — служебный текст tool-заходов сброшен")
assert not [e for e in ev3 if e[0] == "tool"]; ok("маркер «tool» не утекает наружу (bot.py принял бы его за done)")

print("test_webtools: all good")
