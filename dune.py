"""Liquidation history from Dune, for a one-off pull during a trial. Dune bills credits per run and per
export, so the queries return as few rows as the study needs.

  python dune.py sql liq 2025-10-10 2025-10-10      print the liquidation query for a date range (inclusive)
  python dune.py sql prices 2025-10-01 2025-10-31   print the 1-minute mark price query
  python dune.py run liq 2025-10-10 2025-10-10      run it through the API (DUNE_API_KEY in .env) and save the CSV
  python dune.py load                               load every CSV in data/dune/ into data/dune.db

Either I paste the SQL into dune.com and download the CSV into data/dune/, or `run` does both.
Raw exports stay there, outside git; only derived results go in the repo.

The liquidation query keeps only the liquidated account's leg (fill_type = 'liquidation', both market
and backstop) and sums it per coin, block and side. One block holds one oracle update, so a block
plays the part a transaction hash plays in the live data.
"""
import json, sqlite3, sys, time
from pathlib import Path

import pandas as pd

from archive import DATA

DIR = DATA / "dune"
DB = DATA / "dune.db"
COINS = "('BTC', 'ETH', 'SOL', 'HYPE')"

LIQ = """SELECT coin, block_number, MIN(block_time) AS block_time, side, liquidation_method AS method,
       COUNT(*) AS fills, COUNT(DISTINCT trader) AS accounts, SUM(notional_usd) AS notional_usd
FROM hyperliquid.perp_trades
WHERE block_date BETWEEN DATE '{a}' AND DATE '{b}'
  AND perp_dex = 'hyperliquid'
  AND coin IN {coins}
  AND fill_type = 'liquidation'
GROUP BY coin, block_number, side, liquidation_method
ORDER BY block_time"""

PRICES = """SELECT coin, block_minute, mark_price, oracle_price
FROM hyperliquid.perp_oracle_prices
WHERE block_month BETWEEN DATE_TRUNC('month', DATE '{a}') AND DATE_TRUNC('month', DATE '{b}')
  AND block_date BETWEEN DATE '{a}' AND DATE '{b}'
  AND coin IN {coins}
ORDER BY coin, block_minute"""


def ms(col):
    """Dune writes timestamps like '2025-10-10 21:15:03.123 UTC'."""
    return pd.to_datetime(col.astype(str).str.replace(" UTC", "", regex=False), utc=True).astype("int64") // 10**6


def load(directory=DIR, db_path=DB):
    db = sqlite3.connect(db_path)
    db.executescript("""
CREATE TABLE IF NOT EXISTS liq_blocks(coin TEXT, block_number INTEGER, time INTEGER, side TEXT, method TEXT,
  fills INTEGER, accounts INTEGER, ntl REAL, PRIMARY KEY(coin, block_number, side, method));
CREATE TABLE IF NOT EXISTS bars_1m(coin TEXT, ts INTEGER, close REAL, oracle REAL, PRIMARY KEY(coin, ts));
""")
    for f in sorted(Path(directory).glob("*.csv")):
        df = pd.read_csv(f)
        if "block_number" in df:
            rows = zip(df.coin, df.block_number, ms(df.block_time), df.side, df.method, df.fills, df.accounts,
                       df.notional_usd)
            db.executemany("INSERT OR REPLACE INTO liq_blocks VALUES(?,?,?,?,?,?,?,?)", rows)
        elif "block_minute" in df:
            # mark can be missing; the oracle price never is
            close = df.mark_price.fillna(df.oracle_price)
            rows = zip(df.coin, ms(df.block_minute) // 1000, close, df.oracle_price)
            db.executemany("INSERT OR REPLACE INTO bars_1m VALUES(?,?,?,?)", rows)
        else:
            print(f"skip {f.name}: not a liq or prices export")
            continue
        db.commit()
        print(f"{f.name}: {len(df)} rows")
    return db


def dune_legs(db):
    """Same shape as cascades.forced_legs(), one row per coin, block and side.
    A Dune side of 'A' (or 'sell') means the liquidated account sold, so a long was liquidated."""
    df = pd.read_sql("SELECT coin, time, side, method, block_number AS hash, ntl, fills, accounts "
                     "FROM liq_blocks ORDER BY coin, time", db)
    df["side"] = df.side.map(lambda s: "A" if str(s).upper() in ("A", "SELL", "ASK") else "B")
    return df


API = "https://api.dune.com/api/v1"


def api_key():
    for line in (Path(__file__).with_name(".env")).read_text().splitlines():
        if line.startswith("DUNE_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("DUNE_API_KEY missing from .env")


def run(sql, out):
    """Executes ad hoc SQL, waits, pages through the JSON result and writes it as CSV.
    Prints the execution's status fields so I can see what it cost."""
    import requests
    h = {"X-Dune-Api-Key": api_key()}
    r = requests.post(f"{API}/sql/execute", headers=h, json={"sql": sql, "performance": "medium"}, timeout=60)
    r.raise_for_status()
    ex = r.json()["execution_id"]
    while True:
        st = requests.get(f"{API}/execution/{ex}/status", headers=h, timeout=60).json()
        if st["state"] in ("QUERY_STATE_COMPLETED", "QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
            break
        time.sleep(5)
    print(json.dumps({k: v for k, v in st.items() if k != "result_metadata"}, indent=1))
    if st["state"] != "QUERY_STATE_COMPLETED":
        raise SystemExit(st.get("error"))
    rows, offset = [], 0
    while offset is not None:
        page = requests.get(f"{API}/execution/{ex}/results", headers=h, params={"limit": 50000, "offset": offset},
                            timeout=120).json()
        rows += page["result"]["rows"]
        offset = page.get("next_offset")
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=page["result"]["metadata"]["column_names"]).to_csv(out, index=False)
    print(f"{len(rows)} rows -> {out}")


def main(cmd, *args):
    if cmd in ("sql", "run"):
        kind, a, b = args
        sql = (LIQ if kind == "liq" else PRICES).format(a=a, b=b, coins=COINS)
        if cmd == "sql":
            print(sql)
        else:
            run(sql, DIR / f"{kind}_{a.replace('-', '')}_{b.replace('-', '')}.csv")
    elif cmd == "load":
        DIR.mkdir(parents=True, exist_ok=True)
        load()


if __name__ == "__main__":
    main(*sys.argv[1:])
