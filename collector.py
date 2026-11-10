"""Live market recorder for the liquidation cascade study.

One websocket connection, no REST calls (subscriptions cost no rate-limit weight).
For BTC, ETH, SOL and HYPE perps it stores:
  bars       1-second trade flow: OHLC, taker buy and sell notional, trade count
  ctx        mark, oracle, funding, open interest, premium (one row per coin every 5s)
  depth      resting notional within 1% and 2% of mid on each side (every 10s)
It also stores every fill that carries a `liquidation` object for a few watched
addresses (HLP's liquidator and market making vaults), on any coin.
`sessions` records connected time, so the analysis can tell a quiet minute from a gap.

The full liquidation history comes from Hyperliquid's node_fills archive, see SOURCE.md.
Hyperliquid's own archive of open interest and book snapshots arrives about once a month and can
have gaps, so I record my own copy as it happens.

  python collector.py
"""
import asyncio, json, sqlite3, time
from pathlib import Path

import websockets

WS = "wss://api.hyperliquid.xyz/ws"
DB = Path(__file__).with_name("hl_liquidations.db")
COINS = ["BTC", "ETH", "SOL", "HYPE"]
# Three significant figures gives 20 book levels that reach past 2% from mid on all four coins.
BOOK_SIG_FIGS = 3
CTX_EVERY_MS, DEPTH_EVERY_MS = 5_000, 10_000
# Hyperliquid allows 10 distinct users in websocket subscriptions per IP. A market maker's fill carries
# the liquidation tag when it takes the other side of one, so watching the biggest makers on these
# coins samples market liquidations. These nine made 35% of the maker notional on BTC, ETH, SOL and
# HYPE in a 15-minute sample on 10 Nov 2026 (HLP's own market making vaults made 0% there).
WATCH = {
    "0xb0a55f13d22f66e6d495ac98113841b2326e9540": "HLP liquidator",
    "0xcf3f419d08a5bdc2c6e5fbd9ad70904c5420f95f": "maker",
    "0xdc519874071cca5457fdf8b3c31760ac1ca16ef7": "maker",
    "0x7b7f72a28fe109fa703eeed7984f2a8a68fedee2": "maker",
    "0xecb63caa47c7c4e77f60f1ce858cf28dc2b82b00": "maker",
    "0xf9109ada2f73c62e9889b45453065f0d99260a2d": "maker",
    "0x39475d17bcd20adc540e647dae6781b153fbf3b1": "maker",
    "0xaf68caebaa151dd592112a07e2344b3950e13561": "maker",
    "0x34fb5ec7d4e939161946340ea2a1f29254b893de": "maker",
    "0xa62b923a112d50d03e1e096bbd53422490dac104": "maker",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars(coin TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL,
  buy_ntl REAL, sell_ntl REAL, n INTEGER, PRIMARY KEY(coin, ts));
CREATE TABLE IF NOT EXISTS ctx(coin TEXT, ts INTEGER, mark REAL, oracle REAL, mid REAL, funding REAL,
  oi REAL, premium REAL, impact_bid REAL, impact_ask REAL, PRIMARY KEY(coin, ts));
CREATE TABLE IF NOT EXISTS depth(coin TEXT, ts INTEGER, mid REAL, bid_1pct REAL, ask_1pct REAL,
  bid_2pct REAL, ask_2pct REAL, PRIMARY KEY(coin, ts));
CREATE TABLE IF NOT EXISTS liq_fills(user TEXT, coin TEXT, tid INTEGER, time INTEGER, px REAL, sz REAL,
  side TEXT, dir TEXT, hash TEXT, liquidated_user TEXT, mark_px REAL, method TEXT, closed_pnl REAL,
  raw TEXT, PRIMARY KEY(user, coin, tid));
CREATE TABLE IF NOT EXISTS sessions(start_ms INTEGER PRIMARY KEY, last_ms INTEGER);
"""
# ts: bars in unix seconds, everything else in unix milliseconds


def f(x):
    return None if x is None else float(x)


class Recorder:
    """Turns websocket messages into rows. Kept free of I/O apart from the db handle so tests can feed it."""

    def __init__(self, db):
        self.db = db
        db.executescript(SCHEMA)
        self.open_bars = {}                  # (coin, second) -> [o, h, l, c, buy, sell, n]
        self.last_ctx, self.last_depth = {}, {}
        self.session = None

    def start_session(self, now_ms):
        self.session = now_ms
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES(?, ?)", (now_ms, now_ms))

    def handle(self, msg, now_ms):
        ch, d = msg.get("channel"), msg.get("data")
        if ch == "trades":
            for t in d:
                self.trade(t)
            self.flush_bars(int(now_ms // 1000) - 2)
        elif ch == "activeAssetCtx":
            self.ctx(d["coin"], d["ctx"], now_ms)
        elif ch == "l2Book":
            self.depth(d)
        elif ch == "userFills":
            self.fills(d["user"], d["fills"])

    def trade(self, t):
        px, sz, s = float(t["px"]), float(t["sz"]), t["time"] // 1000
        b = self.open_bars.get((t["coin"], s))
        if b is None:
            b = self.open_bars[(t["coin"], s)] = [px, px, px, px, 0.0, 0.0, 0]
        b[1], b[2], b[3] = max(b[1], px), min(b[2], px), px
        b[4 if t["side"] == "B" else 5] += px * sz          # side B: the buyer took liquidity
        b[6] += 1

    def flush_bars(self, before_s):
        done = [k for k in self.open_bars if k[1] < before_s]
        # A trade that arrives after its second was written is merged into the stored row.
        self.db.executemany(
            "INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(coin, ts) DO UPDATE SET "
            "high = max(high, excluded.high), low = min(low, excluded.low), close = excluded.close, "
            "buy_ntl = buy_ntl + excluded.buy_ntl, sell_ntl = sell_ntl + excluded.sell_ntl, n = n + excluded.n",
            [(c, s, *self.open_bars.pop((c, s))) for c, s in done])

    def ctx(self, coin, c, now_ms):
        if now_ms - self.last_ctx.get(coin, 0) < CTX_EVERY_MS:
            return
        self.last_ctx[coin] = now_ms
        imp = c.get("impactPxs") or [None, None]
        self.db.execute("INSERT OR IGNORE INTO ctx VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (coin, now_ms, f(c["markPx"]), f(c["oraclePx"]), f(c.get("midPx")), f(c["funding"]),
                         f(c["openInterest"]), f(c.get("premium")), f(imp[0]), f(imp[1])))

    def depth(self, d):
        coin, ts = d["coin"], d["time"]
        bids, asks = d["levels"]
        if ts - self.last_depth.get(coin, 0) < DEPTH_EVERY_MS or not bids or not asks:
            return
        self.last_depth[coin] = ts
        mid = (float(bids[0]["px"]) + float(asks[0]["px"])) / 2

        def within(levels, pct):
            return sum(float(l["px"]) * float(l["sz"]) for l in levels
                       if abs(float(l["px"]) / mid - 1) <= pct)

        self.db.execute("INSERT OR IGNORE INTO depth VALUES(?,?,?,?,?,?,?)",
                        (coin, ts, mid, within(bids, .01), within(asks, .01), within(bids, .02), within(asks, .02)))

    def fills(self, user, fills):
        rows = [(user, x["coin"], x["tid"], x["time"], float(x["px"]), float(x["sz"]), x["side"], x["dir"],
                 x["hash"], x["liquidation"].get("liquidatedUser"), f(x["liquidation"].get("markPx")),
                 x["liquidation"].get("method"), f(x.get("closedPnl")), json.dumps(x))
                for x in fills if x.get("liquidation")]
        self.db.executemany("INSERT OR IGNORE INTO liq_fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def commit(self, now_ms):
        if self.session:
            self.db.execute("UPDATE sessions SET last_ms = ? WHERE start_ms = ?", (now_ms, self.session))
        self.db.commit()


def subscriptions():
    for c in COINS:
        yield {"type": "trades", "coin": c}
        yield {"type": "activeAssetCtx", "coin": c}
        yield {"type": "l2Book", "coin": c, "nSigFigs": BOOK_SIG_FIGS}
    for u in WATCH:
        yield {"type": "userFills", "user": u}


async def run(rec, stats):
    async with websockets.connect(WS, ping_interval=None, max_size=2**24) as ws:
        for s in subscriptions():
            await ws.send(json.dumps({"method": "subscribe", "subscription": s}))
        rec.start_session(int(time.time() * 1000))
        last_ping = last_commit = time.time()
        while True:
            if time.time() - last_ping > 30:           # HL drops idle connections after 60s
                await ws.send(json.dumps({"method": "ping"}))
                last_ping = time.time()
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            except asyncio.TimeoutError:
                continue
            rec.handle(msg, int(time.time() * 1000))
            stats["msgs"] += 1
            if time.time() - last_commit > 5:
                rec.commit(int(time.time() * 1000))
                last_commit = time.time()


async def main():
    rec, stats = Recorder(sqlite3.connect(DB)), {"msgs": 0}
    rec.db.execute("PRAGMA journal_mode=WAL")
    backoff, last_log = 5, time.time()
    print("collector start", flush=True)
    while True:
        task, started = asyncio.create_task(run(rec, stats)), time.time()
        try:
            while not task.done():
                await asyncio.sleep(5)
                if time.time() - last_log > 3600:
                    print(f"last hour: {stats['msgs']} messages", flush=True)
                    stats["msgs"], last_log = 0, time.time()
            task.result()
        except Exception as e:
            rec.db.rollback()
            if time.time() - started > 300:            # it ran fine for a while: start backoff over
                backoff = 5
            print(f"disconnected: {e!r}; retry in {backoff}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    asyncio.run(main())
