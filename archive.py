"""Load Hyperliquid's S3 archive into data/history.db. Runs on my own machine, never the VPS.

Both buckets are requester-pays, so this needs the AWS CLI configured with my own key.
  python archive.py probe 20251010              one hour of fills and one day of asset contexts, prints sizes
  python archive.py fills 20251010 20251011     node_fills_by_block, every hour of each date (inclusive)
  python archive.py ctxs 20251010 20251011      asset_ctxs, one file per date
  python archive.py funding 20250727            hourly funding from the public API since that date (no AWS)

From the fills I keep only BTC, ETH, SOL and HYPE:
  liq           every fill that carries a `liquidation` object (both legs, the analysis picks one)
  bars_1m       1-minute OHLC and taker buy and sell notional, from taker legs only so a match counts once
asset_ctxs keeps whatever columns Hyperliquid ships, filtered to the four coins.
Downloaded files stay in data/raw so a rerun costs nothing; `loaded` stops a file from loading twice.
"""
import io, json, sqlite3, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lz4.frame

COINS = {"BTC", "ETH", "SOL", "HYPE"}
DATA = Path(__file__).with_name("data")
RAW = DATA / "raw"
DB = DATA / "history.db"
FILLS = "s3://hl-mainnet-node-data/node_fills_by_block/hourly/{d}/{h}.lz4"
CTXS = "s3://hyperliquid-archive/asset_ctxs/{d}.csv.lz4"

SCHEMA = """
CREATE TABLE IF NOT EXISTS liq(coin TEXT, time INTEGER, user TEXT, liquidated_user TEXT, method TEXT,
  mark_px REAL, px REAL, sz REAL, side TEXT, dir TEXT, crossed INTEGER, closed_pnl REAL, hash TEXT,
  tid INTEGER, PRIMARY KEY(coin, tid, user));
CREATE TABLE IF NOT EXISTS bars_1m(coin TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL,
  buy_ntl REAL, sell_ntl REAL, n INTEGER, PRIMARY KEY(coin, ts));
CREATE TABLE IF NOT EXISTS loaded(key TEXT PRIMARY KEY, rows INTEGER);
CREATE TABLE IF NOT EXISTS funding(coin TEXT, time INTEGER, rate REAL, premium REAL, PRIMARY KEY(coin, time));
"""
# ts in bars_1m: unix seconds at the start of the minute. time in liq: unix milliseconds.


def open_db(path=DB):
    path.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def fetch(key):
    """Download one S3 object into data/raw unless it is already there. Returns the local path or None."""
    local = RAW / key.split("://", 1)[1]
    if not local.exists():
        local.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["aws", "s3", "cp", key, str(local), "--request-payer", "requester", "--only-show-errors"],
                           capture_output=True, text=True)
        if r.returncode:
            print(f"skip {key}: {r.stderr.strip()}", flush=True)
            return None
    return local


def parse_fill_lines(lines):
    """Yields (user, fill) for the four coins from node_fills_by_block lines (one block per line)."""
    for line in lines:
        if not line.strip():
            continue
        for user, fill in json.loads(line)["events"]:
            if fill["coin"] in COINS:
                yield user, fill


def load_fills(db, key, lines):
    if db.execute("SELECT 1 FROM loaded WHERE key = ?", (key,)).fetchone():
        return 0
    liq, bars = [], {}
    for user, x in parse_fill_lines(lines):
        if x.get("liquidation"):
            l = x["liquidation"]
            liq.append((x["coin"], x["time"], user, l.get("liquidatedUser"), l.get("method"), float(l["markPx"]),
                        float(x["px"]), float(x["sz"]), x["side"], x["dir"], int(x["crossed"]),
                        float(x.get("closedPnl") or 0), x["hash"], x["tid"]))
        if not x["crossed"]:
            continue
        px, sz = float(x["px"]), float(x["sz"])
        k = (x["coin"], x["time"] // 60_000 * 60)
        b = bars.get(k)
        if b is None:
            b = bars[k] = [x["time"], px, px, px, x["time"], px, 0.0, 0.0, 0]  # first_ms, o, h, l, last_ms, c, ...
        if x["time"] < b[0]:
            b[0], b[1] = x["time"], px
        if x["time"] >= b[4]:
            b[4], b[5] = x["time"], px
        b[2], b[3] = max(b[2], px), min(b[3], px)
        b[6 if x["side"] == "B" else 7] += px * sz       # the taker leg's side is the aggressor
        b[8] += 1
    db.executemany("INSERT OR IGNORE INTO liq VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", liq)
    # A minute can straddle two hourly files: merge into the stored row.
    db.executemany(
        "INSERT INTO bars_1m VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(coin, ts) DO UPDATE SET "
        "high = max(high, excluded.high), low = min(low, excluded.low), close = excluded.close, "
        "buy_ntl = buy_ntl + excluded.buy_ntl, sell_ntl = sell_ntl + excluded.sell_ntl, n = n + excluded.n",
        [(c, ts, b[1], b[2], b[3], b[5], b[6], b[7], b[8]) for (c, ts), b in sorted(bars.items())])
    db.execute("INSERT INTO loaded VALUES(?, ?)", (key, len(liq)))
    db.commit()
    return len(liq)


def load_ctxs(db, key, text):
    import pandas as pd
    if db.execute("SELECT 1 FROM loaded WHERE key = ?", (key,)).fetchone():
        return 0
    df = pd.read_csv(io.StringIO(text))
    df = df[df["coin"].isin(COINS)]
    df.to_sql("asset_ctxs", db, if_exists="append", index=False)
    db.execute("INSERT INTO loaded VALUES(?, ?)", (key, len(df)))
    db.commit()
    return len(df)


def load_funding(db, since):
    """fundingHistory returns at most 500 hourly rows per call. Weight 20 a call, run from my own machine."""
    import requests
    for coin in sorted(COINS):
        start = db.execute("SELECT max(time) + 1 FROM funding WHERE coin = ?", (coin,)).fetchone()[0] or since
        while start < time.time() * 1000 - 3_600_000:
            rows = requests.post("https://api.hyperliquid.xyz/info", timeout=30,
                                 json={"type": "fundingHistory", "coin": coin, "startTime": start}).json()
            if not rows:
                break
            db.executemany("INSERT OR IGNORE INTO funding VALUES(?,?,?,?)",
                           [(coin, r["time"], float(r["fundingRate"]), float(r["premium"])) for r in rows])
            db.commit()
            start = rows[-1]["time"] + 1
            time.sleep(2)
        print(coin, db.execute("SELECT count(*) FROM funding WHERE coin = ?", (coin,)).fetchone()[0], "hours", flush=True)


def read_lz4(path):
    with lz4.frame.open(path, "rt", encoding="utf-8") as f:
        yield from f


def dates(a, b):
    d, end = datetime.strptime(a, "%Y%m%d").date(), datetime.strptime(b, "%Y%m%d").date()
    while d <= end:
        yield d.strftime("%Y%m%d")
        d += timedelta(days=1)


def main(cmd, a, b=None):
    db = open_db()
    if cmd == "funding":
        return load_funding(db, int(datetime.strptime(a, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp() * 1000))
    if cmd == "probe":
        # I took the key layout from third-party docs. Print the real one before trusting FILLS.
        for prefix in ("s3://hl-mainnet-node-data/node_fills_by_block/", "s3://hyperliquid-archive/asset_ctxs/"):
            r = subprocess.run(["aws", "s3", "ls", prefix, "--request-payer", "requester"], capture_output=True, text=True)
            print(prefix, "\n", (r.stdout or r.stderr)[:600], flush=True)
        for key in (FILLS.format(d=a, h=12), CTXS.format(d=a)):
            p = fetch(key)
            if p:
                print(f"{key}: {p.stat().st_size / 1e6:.1f} MB compressed", flush=True)
        return
    for d in dates(a, b or a):
        if cmd == "fills":
            for h in range(24):
                key = FILLS.format(d=d, h=h)
                p = fetch(key)
                if p:
                    print(f"{key}: {load_fills(db, key, read_lz4(p))} liquidation fills", flush=True)
        elif cmd == "ctxs":
            key = CTXS.format(d=d)
            p = fetch(key)
            if p:
                print(f"{key}: {load_ctxs(db, key, ''.join(read_lz4(p)))} rows", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
