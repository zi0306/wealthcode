"""黃鼎彥的財富密碼 — 雲端看盤下單系統
每位客戶用自己的永豐 API 金鑰登入；同一組金鑰的手機/電腦共用同一個連線，畫面與下單即時同步。
DEMO=1 啟動示範模式（假行情、假下單），不需永豐帳號。"""
import os, time, json, hashlib, secrets, asyncio, random, threading, tempfile, datetime as dt
from fastapi import FastAPI, Form, UploadFile, File, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from engine import Engine
from push import Push
PUSH = Push(os.path.join(os.getenv("DATA_DIR", "/tmp"), "push.json"))

DEMO = os.getenv("DEMO") == "1"
BAR_SEC = int(os.getenv("BAR_SEC", "60"))
TZ = dt.timezone(dt.timedelta(hours=8))
app = FastAPI()
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
ACCOUNTS, TOKENS = {}, {}
PUBLIC = None  # 觀察模式共用行情（伺服器端用營運者的行情金鑰）

def now_str(): return dt.datetime.now(TZ).strftime("%H:%M:%S")

class Account:
    def __init__(self, key_hash, secret_hash):
        self.key_hash, self.secret_hash = key_hash, secret_hash
        self.engine, self.lock = Engine(), threading.Lock()
        self.cur, self.price, self.api, self.can_trade, self.live = None, None, None, False, False
        self.orders, self.position, self.clients, self.version = [], {"qty": 0, "pnl": 0, "avg": 0, "real": 0 if DEMO else None}, set(), 0

    def add_price(self, ts, p):
        k = int(ts // BAR_SEC)
        with self.lock:
            self.price = p
            if self.cur and self.cur["k"] != k:
                b = {x: self.cur[x] for x in "ohlc"}
                b["t"] = self.cur["k"] * BAR_SEC * 1000
                msgs = self.engine.on_bar(b, dt.datetime.fromtimestamp(ts, TZ).strftime("%m/%d %H:%M"))
                if msgs and self.live and time.time() - ts < 300:
                    for m in msgs: PUSH.send_async(self.key_hash, m, self.price)
                self.cur = None
            if not self.cur: self.cur = {"k": k, "o": p, "h": p, "l": p, "c": p}
            else: self.cur.update(h=max(self.cur["h"], p), l=min(self.cur["l"], p), c=p)
            self.version += 1

    def snapshot(self):
        with self.lock:
            s = self.engine.snapshot()
            if self.cur: s["bars"] = s["bars"] + [{"t": self.cur["k"] * BAR_SEC * 1000, **{x: self.cur[x] for x in "ohlc"}}]
            s.update(price=self.price, orders=self.orders[-20:][::-1], position=self.position,
                     can_trade=self.can_trade, guest=self.key_hash == "public", demo=DEMO, devices=len(self.clients), version=self.version)
            return s

# ---------- 永豐連線 ----------
def connect_sinopac(acc, api_key, secret_key, pfx_bytes, ca_pass, person_id):
    import shioaji as sj
    api = sj.Shioaji()
    api.login(api_key=api_key, secret_key=secret_key)
    if pfx_bytes:
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".pfx"); f.write(pfx_bytes); f.close()
        api.activate_ca(ca_path=f.name, ca_passwd=ca_pass, person_id=person_id)
        acc.can_trade = True
    acc.api = api
    c = api.Contracts.Futures.TXF.TXFR1
    try:
        d = dt.date.today()
        kb = api.kbars(c, start=(d - dt.timedelta(days=3)).isoformat(), end=d.isoformat())
        for t, o, h, l, cl in zip(kb.ts, kb.Open, kb.High, kb.Low, kb.Close):
            ts = t / 1e9 - 8 * 3600 - 59  # kbar 時間為該分鐘結束，往前移到同一分鐘內
            for v in (o, h, l, cl): acc.add_price(ts, float(v))
    except Exception as e:
        print("歷史K暖機失敗", e)

    acc.live = True

    @api.on_tick_fop_v1()
    def _tick(exchange, tick):
        acc.add_price(tick.datetime.replace(tzinfo=TZ).timestamp(), float(tick.close))
    api.quote.subscribe(c, quote_type=sj.constant.QuoteType.Tick, version=sj.constant.QuoteVersion.v1)
    refresh_position(acc)
    if acc.can_trade:
        def loop():
            while acc.api is api:
                time.sleep(5); refresh_position(acc); acc.version += 1
        threading.Thread(target=loop, daemon=True).start()

def near_month(api):
    """下單用實際近月合約（R1 是連續月代碼，只拿來看行情）"""
    cs = [c for c in api.Contracts.Futures.TXF if not c.code.endswith(("R1", "R2"))]
    return min(cs, key=lambda c: c.delivery_date)

def refresh_position(acc):
    if DEMO or not acc.api: return
    try:
        ps = acc.api.list_positions(acc.api.futopt_account)
        q = sum((p.quantity if str(p.direction).endswith("Buy") else -p.quantity) for p in ps)
        n = sum(p.quantity for p in ps)
        avg = sum(float(p.price) * p.quantity for p in ps) / n if n else 0
        acc.position = {"qty": q, "pnl": round(sum(float(p.pnl) for p in ps)), "avg": avg, "real": None}
    except Exception as e:
        print("庫存查詢失敗", e)

def demo_feed(acc):
    p, t, d = 22400.0, time.time() - BAR_SEC * 200, -1
    for i in range(200 * 6):
        if i % 240 == 0: d = -d
        p += d * 0.7 + random.uniform(-6, 6); acc.add_price(t + i * BAR_SEC / 6, round(p))
    acc.live = True
    while True:
        p += random.uniform(-6, 6.3); acc.add_price(time.time(), round(p))
        q = acc.position["qty"]
        if q: acc.position["pnl"] = round((p - acc.position["avg"]) * q * 200)
        time.sleep(1)

# ---------- 觀察模式 ----------
@app.on_event("startup")
def start_public():
    global PUBLIC
    PUBLIC = Account("public", "")
    if DEMO:
        threading.Thread(target=demo_feed, args=(PUBLIC,), daemon=True).start()
    elif os.getenv("SJ_API_KEY"):
        def run():
            try: connect_sinopac(PUBLIC, os.getenv("SJ_API_KEY"), os.getenv("SJ_SEC_KEY"), None, "", "")
            except Exception as e: print("觀察模式行情連線失敗：", e)
        threading.Thread(target=run, daemon=True).start()
    else:
        print("未設定 SJ_API_KEY，觀察模式無行情")

@app.post("/api/guest")
def guest():
    if not PUBLIC or PUBLIC.price is None: raise HTTPException(503, "行情準備中，請稍後再試")
    tok = secrets.token_urlsafe(24); TOKENS[tok] = "public"
    return {"token": tok}

# ---------- API ----------
@app.get("/")
def index(): return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))

@app.post("/api/login")
async def login(api_key: str = Form(""), secret_key: str = Form(""), person_id: str = Form(""),
                ca_pass: str = Form(""), pfx: UploadFile | None = File(None)):
    if DEMO: api_key, secret_key = api_key or "demo", secret_key or "demo"
    if not api_key or not secret_key: raise HTTPException(400, "請輸入 API Key 和 Secret Key")
    kh = hashlib.sha256(api_key.encode()).hexdigest()
    sh = hashlib.sha256(secret_key.encode()).hexdigest()
    acc = ACCOUNTS.get(kh)
    if acc and acc.secret_hash != sh: raise HTTPException(401, "金鑰不正確")
    if not acc:
        acc = Account(kh, sh)
        if DEMO:
            acc.can_trade = True
            threading.Thread(target=demo_feed, args=(acc,), daemon=True).start()
        else:
            pfx_bytes = await pfx.read() if pfx else None
            try:
                await asyncio.to_thread(connect_sinopac, acc, api_key, secret_key, pfx_bytes, ca_pass, person_id.upper())
            except Exception as e:
                raise HTTPException(401, f"永豐登入失敗：{e}")
        ACCOUNTS[kh] = acc
    tok = secrets.token_urlsafe(24); TOKENS[tok] = kh
    return {"token": tok}

def get_acc(token):
    kh = TOKENS.get(token)
    if kh == "public": return PUBLIC
    if not kh or kh not in ACCOUNTS: raise HTTPException(401, "請重新登入")
    return ACCOUNTS[kh]

@app.post("/api/order")
def order(token: str = Form(...), action: str = Form(...), qty: int = Form(1), device: str = Form("")):
    acc = get_acc(token)
    if acc is PUBLIC: raise HTTPException(403, "觀察模式不能下單，請用自己的 API 登入")
    if not acc.can_trade: raise HTTPException(400, "未上傳憑證，只能看盤不能下單")
    if action not in ("buy", "sell", "close") or not (1 <= qty <= 10): raise HTTPException(400, "參數錯誤")
    label = {"buy": "買進", "sell": "賣出", "close": "全部平倉"}[action]
    if DEMO:
        P, p = acc.position, acc.price
        q0, avg, real = P["qty"], P["avg"], P["real"]
        if action == "close" and q0 == 0: raise HTTPException(400, "目前沒有庫存")
        d = -q0 if action == "close" else (qty if action == "buy" else -qty)
        n = q0 + d
        if q0 and (d > 0) != (q0 > 0):
            c = min(abs(d), abs(q0)); real += round((p - avg) * (1 if q0 > 0 else -1) * c * 200)
            avg = p if abs(d) > abs(q0) else (avg if n else 0)
        else:
            avg = (avg * abs(q0) + p * abs(d)) / abs(n) if q0 else p
        acc.position = {"qty": n, "avg": avg, "real": real, "pnl": round((p - avg) * n * 200) if n else 0}
        result = f"成交 {p:g}（示範）"
    else:
        import shioaji as sj
        api = acc.api; c = near_month(api)
        refresh_position(acc)
        if action == "close":
            q = acc.position["qty"]
            if q == 0: raise HTTPException(400, "目前沒有庫存")
            action_, qty_, oc = ("Sell" if q > 0 else "Buy"), abs(q), "Cover"
        else:
            action_, qty_, oc = ("Buy" if action == "buy" else "Sell"), qty, "Auto"
        o = api.Order(action=action_, price=0, quantity=qty_, price_type="MKT", order_type="IOC",
                      octype=oc, account=api.futopt_account)
        tr = api.place_order(c, o)
        result = f"委託已送出（{getattr(tr.status, 'status', '')}）"
        time.sleep(1); refresh_position(acc)
    acc.orders.append({"t": now_str(), "msg": f"{device or '裝置'} {label}{(' '+str(qty)+'口') if action != 'close' else ''} {result}"})
    acc.version += 1
    return {"ok": True, "msg": result}

@app.websocket("/ws")
async def ws(websocket: WebSocket, token: str):
    await websocket.accept()
    try: acc = get_acc(token)
    except HTTPException:
        await websocket.send_json({"error": "請重新登入"}); await websocket.close(); return
    acc.clients.add(websocket); last = -1
    try:
        while True:
            if acc.version != last:
                last = acc.version
                await websocket.send_text(json.dumps(acc.snapshot(), ensure_ascii=False))
            await asyncio.sleep(0.5)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        acc.clients.discard(websocket)

@app.get("/api/mode")
def mode(): return {"demo": DEMO}

# ---------- 背景推播（App 關掉也收得到） ----------
@app.get("/sw.js")
def sw(): return FileResponse(os.path.join(os.path.dirname(__file__), "static", "sw.js"), media_type="application/javascript")

@app.get("/api/vapid")
def vapid(): return {"key": PUSH.public_key}

@app.post("/api/subscribe")
async def subscribe(token: str = Form(...), sub: str = Form(...)):
    acc = get_acc(token)
    PUSH.add(acc.key_hash, json.loads(sub))
    return {"ok": True}

@app.post("/api/test_push")
def test_push(token: str = Form(...)):
    acc = get_acc(token)
    n = PUSH.send(acc.key_hash, "測試通知：通知設定成功", acc.price)
    return {"sent": n}
