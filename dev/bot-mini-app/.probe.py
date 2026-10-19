import asyncio, json, os, sqlite3, sys, time, hashlib, hmac
from urllib.parse import urlencode
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES","ALLOWED_IDS","OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, os.getcwd())
import auth, bot, kimi, chatfiles
from aiohttp.test_utils import TestClient, TestServer

mem = sqlite3.connect(":memory:", check_same_thread=False); bot.db.backup(mem); mem.row_factory = sqlite3.Row
bot.db = mem; auth.init(mem)
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time()+99999, auth._fp())); mem.commit()
def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", bot.TOKEN.encode(), hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)
H = {"X-Init": init(42, "Gladik_N")}

class FakeBot:
    def __init__(self): self.docs, self.deleted, self.prepared = [], [], []
    async def send_document(self, chat_id, document, caption=None, **kw):
        self.docs.append((chat_id, getattr(document, "filename", None), getattr(document, "data", None), kw))
        class Doc: file_id = "FID-%d" % len(self.docs)
        class M: pass
        m = M(); m.message_id = 700 + len(self.docs); m.document = Doc()
        return m
    async def send_photo(self, chat_id, photo, caption=None, **kw):
        class P: file_id = "PH-%d" % len(self.docs)
        class M: pass
        m = M(); m.message_id = 790; m.photo = [P()]
        return m
    async def delete_message(self, chat_id, message_id): self.deleted.append((chat_id, message_id))
    async def save_prepared_inline_message(self, user_id, result, **kw):
        self.prepared.append((user_id, result, kw))
        class P: id = "prepared-1"
        return P()

HTML = "<h1>План урока</h1><p>Разминка 5 минут</p>"
async def main():
    fb = FakeBot()
    app = bot.make_app(); app["bot"] = fb
    async def fake_stream(messages, model=None, deep=False, json_mode=False, images=None, thinking=True):
        yield "text", HTML
    real = kimi.ask_stream; kimi.ask_stream = fake_stream; key = kimi.API_KEY; kimi.API_KEY = "test"
    async with TestClient(TestServer(app)) as c:
        r = await c.post("/api/chat", json={"messages":[{"role":"user","text":"план урока"}], "format":"docx"}, headers=H)
        ev = [json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
        done = [e for e in ev if e["t"]=="done"][-1]
        print("REPLY =", json.dumps(done.get("reply"), ensure_ascii=False))
        print("FILE keys =", sorted((done.get("file") or {}).keys()))
        print("FILE name =", (done.get("file") or {}).get("name"))
        # теперь как фронт: rebuild из текста карточки
        card_text = done.get("reply") or ""
        rr = await c.post("/api/chat_file/rebuild", json={"text": card_text[:100000], "name": (done.get("file") or {}).get("name")}, headers=H)
        d = await rr.json()
        got = await c.get(d["url"])
        b = await got.read()
        _, built, _ = await __import__("asyncio").get_event_loop().run_in_executor(None, chatfiles.build, "docx", card_text)
        print("REBUILT contains card text:", card_text[:40].encode("utf-8") in b)
        print("ORIGINAL source text in rebuilt:", "Разминка" .encode("utf-8") in b)
        import zipfile, io
        z = zipfile.ZipFile(io.BytesIO(b))
        doc = z.read("word/document.xml").decode("utf-8", "ignore")
        print("REBUILT docx text snippet:", doc[doc.find("<w:t"):doc.find("<w:t")+220])
        # prepare по ОРИГИНАЛЬНОЙ записи
        tok = (done["file"]["url"].split("/")[3].split("?")[0])
        pp = await c.post("/api/chat_file/prepare", json={"tok": tok}, headers=H)
        print("PREPARE status:", pp.status, await pp.json())
        print("uploads:", [(x[0], x[1], x[2][:20]) for x in fb.docs])
        print("deleted:", fb.deleted)
        print("result type:", type(fb.prepared[-1][1]).__name__ if fb.prepared else None,
              "| file_id:", getattr(fb.prepared[-1][1], "document_file_id", None) if fb.prepared else None,
              "| title:", getattr(fb.prepared[-1][1], "title", None) if fb.prepared else None)
    kimi.ask_stream = real; kimi.API_KEY = key
asyncio.run(main())
