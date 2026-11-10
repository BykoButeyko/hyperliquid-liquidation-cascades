"""Event study: price impact during a cascade and what happens after it, against matched random times.

  python study.py           live data: data/live.db from sync.py
  python study.py archive   data/history.db from archive.py
  python study.py dune      data/dune.db from dune.py
It prints counts, and returns only once a group has MIN_EVENTS declustered cascades.

Returns are signed in the direction of the forced flow: positive impact means price moved the way the
liquidations pushed it (down when longs were liquidated). A negative forward return means a give-back.
"""
import sqlite3

import numpy as np
import pandas as pd

import cascades as cz
from archive import DB
from sync import DB as LIVE_DB

HORIZONS = [5, 15, 60, 240]   # minutes after the cascade ends
MIN_EVENTS = 30               # don't show returns for a group with fewer declustered cascades
DRAWS = 50                    # random benchmark times per cascade
BOOT = 2000


def closes(db, table="bars_1m"):
    """1-minute closes. Both bar tables keep ts in unix seconds; the collector's are 1-second bars."""
    b = pd.read_sql(f"SELECT coin, ts, close FROM {table}", db)
    b["t"] = pd.to_datetime(b.ts, unit="s")
    return {c: g.set_index("t").close.resample("60s").last().ffill() for c, g in b.groupby("coin")}


def vol_tercile(px):
    """1-hour realized volatility of 1-minute log returns, cut into terciles over the coin's whole sample."""
    v = np.log(px).diff().rolling(60).std()
    return pd.qcut(v, 3, labels=False)


def ret(px, a, b):
    try:
        return px.loc[b] / px.loc[a] - 1
    except KeyError:
        return np.nan


def measure(cas, px_by_coin, rng):
    """One row per cascade: impact, forward returns, and the matched benchmark mean for each horizon."""
    rows = []
    for coin, g in cas.groupby("coin"):
        px = px_by_coin[coin]
        terc = vol_tercile(px)
        # benchmark pool: minutes at least DECLUSTER_H hours away from any cascade on this coin
        near = pd.Series(False, index=px.index)
        for s in pd.to_datetime(g.start, unit="ms"):
            near |= (px.index > s - pd.Timedelta(hours=cz.DECLUSTER_H)) & (px.index < s + pd.Timedelta(hours=cz.DECLUSTER_H))
        pool = pd.DataFrame({"hour": px.index.hour, "terc": terc.values}, index=px.index)[~near.values]
        for _, e in g.iterrows():
            s = pd.to_datetime(e.start, unit="ms").floor("60s")
            end = pd.to_datetime(e.end, unit="ms").floor("60s") + pd.Timedelta(minutes=1)
            d = -1 if e.long_share >= 0.5 else 1
            row = {"coin": coin, "start": s, "event": e.event, "dir": d,
                   "impact": d * ret(px, s - pd.Timedelta(minutes=1), end)}
            cand = pool[(pool.hour == s.hour) & (pool.terc == terc.get(s - pd.Timedelta(minutes=1)))].index
            bench = rng.choice(cand, size=min(DRAWS, len(cand)), replace=False) if len(cand) else []
            for h in HORIZONS:
                row[f"fwd_{h}"] = d * ret(px, end, end + pd.Timedelta(minutes=h))
                row[f"bench_{h}"] = np.nanmean([d * ret(px, t, t + pd.Timedelta(minutes=h)) for t in bench]) \
                    if len(bench) else np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def summarize(m, rng, min_events=MIN_EVENTS):
    """Mean abnormal forward return per horizon with a day-block bootstrap 95% interval.
    Groups with fewer than min_events cascades get counts only."""
    out = []
    for name, g in [("all", m)] + list(m.groupby("coin")):
        r = {"group": name, "cascades": len(g), "market_events": g.event.nunique()}
        if len(g) >= min_events:
            r["impact_med"] = g.impact.median()
            days = g.start.dt.floor("D")
            for h in HORIZONS:
                ab = (g[f"fwd_{h}"] - g[f"bench_{h}"]).groupby(days).apply(list)
                means = [np.nanmean(sum(rng.choice(ab.values, len(ab)).tolist(), [])) for _ in range(BOOT)]
                r[f"abn_{h}"] = np.nanmean(sum(ab.tolist(), []))
                r[f"ci_{h}"] = (np.nanpercentile(means, 2.5), np.nanpercentile(means, 97.5))
        out.append(r)
    return pd.DataFrame(out)


def main(source="live"):
    live = source == "live"
    if source == "dune":
        import dune
        db = sqlite3.connect(dune.DB)
        liq = dune.dune_legs(db)
    else:
        db = sqlite3.connect(LIVE_DB if live else DB)
        liq = cz.watched_legs(db) if live else cz.forced_legs(db)
    cas = cz.decluster(cz.cascades(cz.episodes(liq), cz.thresholds(liq)))
    print(f"{len(liq)} liquidations, {len(cas)} declustered cascades in {cas.event.nunique()} market events")
    print(cas.groupby("coin").size().to_string())
    rng = np.random.default_rng(7)
    m = measure(cas, closes(db, "bars" if live else "bars_1m"), rng)
    with pd.option_context("display.width", 200, "display.max_columns", 30):
        print(summarize(m, rng))


if __name__ == "__main__":
    import sys
    main(*sys.argv[1:])
