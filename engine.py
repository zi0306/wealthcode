"""V轉/A轉 狀態機（與 TradingView 版同邏輯，已情境測試）"""
class Engine:
    NAME = {1: "多 ▲", -1: "空 ▼", 2: "翻多（觀察回碰）", -2: "翻空（觀察反彈）", 0: "等待資料"}

    def __init__(self, N=3, M=5, GAP=5):
        self.N, self.M, self.GAP = N, M, GAP
        self.bars, self.events = [], []
        self.state, self.swH, self.swL, self.anchor, self.ext, self.cnt = 0, None, None, None, None, 0
        self.last_hold = None

    def on_bar(self, b, t):
        B, N, M, G = self.bars, self.N, self.M, self.GAP
        B.append(b); del B[:-600]
        i = len(B) - 1
        H = lambda k: B[k]["h"]; L = lambda k: B[k]["l"]
        msgs = []
        if self.state == 0 and i > N * 4:
            self.state = 1 if B[i]["c"] >= B[i - N * 4]["c"] else -1
        ph = pl = None
        j = i - N
        if j >= N:
            rng = [k for k in range(j - N, j + N + 1) if k != j]
            if all(H(k) < H(j) for k in rng): ph = H(j)
            if all(L(k) > L(j) for k in rng): pl = L(j)
        if ph is not None: self.swH = ph
        if pl is not None: self.swL = pl
        if ph is not None:
            if self.state == 1 and self.swL is not None and self.last_hold != self.swL:
                self.last_hold = self.swL
                msgs.append(f"A轉沒有破底 {self.swL:g} → 維持多")
        if pl is not None:
            if self.state == -1 and self.swH is not None and self.last_hold != self.swH:
                self.last_hold = self.swH
                msgs.append(f"V轉沒有過高 {self.swH:g} → 維持空")
        st = self.state
        if st == -1 and self.swH is not None and b["c"] > self.swH + G:
            self.state, self.anchor, self.ext, self.cnt = 2, self.swH, b["h"], 0
            msgs.append(f"V轉過高 {self.anchor:g} → 翻多")
        elif st == 1 and self.swL is not None and b["c"] < self.swL - G:
            self.state, self.anchor, self.ext, self.cnt = -2, self.swL, b["l"], 0
            msgs.append(f"A轉破底 {self.anchor:g} → 翻空")
        elif st == 2:
            self.ext = max(self.ext, b["h"]); self.cnt += 1
            if b["c"] <= self.anchor:
                self.state, self.swH = -1, self.ext
                msgs.append(f"拉回碰到前高 {self.anchor:g} → 續空")
            elif (pl is not None and pl > self.anchor) or self.cnt >= M:
                self.state = 1; self.swL = min(x["l"] for x in B[-(self.cnt + 1):])
                msgs.append("沒回碰前高，多方確立")
        elif st == -2:
            self.ext = min(self.ext, b["l"]); self.cnt += 1
            if b["c"] >= self.anchor:
                self.state, self.swL = 1, self.ext
                msgs.append(f"反彈碰到前低 {self.anchor:g} → 續多")
            elif (ph is not None and ph < self.anchor) or self.cnt >= M:
                self.state = -1; self.swH = max(x["h"] for x in B[-(self.cnt + 1):])
                msgs.append("沒反彈碰前低，空方確立")
        for m in msgs:
            self.events.append({"t": t, "msg": m, "state": self.state})
        del self.events[:-100]
        return msgs

    def snapshot(self):
        return {"state": self.state, "name": self.NAME[self.state], "swH": self.swH, "swL": self.swL,
                "anchor": self.anchor if self.state in (2, -2) else None,
                "bars": self.bars[-150:], "events": self.events[-40:][::-1]}
