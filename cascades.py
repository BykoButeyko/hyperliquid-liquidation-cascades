"""Liquidations -> episodes -> cascades, with the definitions from PLAN.md.

Input is the `liq` table that archive.py fills. A liquidation counts once, on the liquidated
user's own leg (user = liquidated_user); the counterparty leg carries the same tag.
"""
import pandas as pd

GAP_S = 60                # chain liquidations while neighbours are at most this far apart
QUANTILE = 0.99           # episode notional must reach this quantile of 60s liquidation notional
WINDOW = "30D"            # ...measured over this trailing window
MIN_HISTORY = 7 * 1440    # minutes of history before a threshold exists
MIN_UPDATES = 3           # liquidations at this many separate oracle updates (distinct tx hashes)
DECLUSTER_H = 4           # drop a cascade if another one on the coin started within this many hours before
MARKET_EVENT_MIN = 5      # cascades on different coins starting this close together form one market event


def forced_legs(db):
    df = pd.read_sql("SELECT coin, time, px, sz, side, hash, method FROM liq WHERE user = liquidated_user "
                     "ORDER BY coin, time", db)
    df["ntl"] = df.px * df.sz
    return df


def watched_legs(db, coins=("BTC", "ETH", "SOL", "HYPE")):
    """Liquidations the live collector saw from the other side: fills of the HLP vaults it watches,
    tagged with someone else's liquidation. Only part of each liquidation and only the part HLP filled,
    so notional is a sample. `side` is flipped to the liquidated user's side."""
    # The websocket replays each vault's recent fills on every connect, which reaches back before the
    # collector started. Keep only what falls inside a connected session, where coverage is complete.
    q = ",".join("?" * len(coins))
    df = pd.read_sql(f"SELECT coin, time, px, sz, side, hash, method FROM liq_fills f "
                     f"WHERE user != liquidated_user AND coin IN ({q}) AND EXISTS (SELECT 1 FROM sessions s "
                     f"WHERE f.time BETWEEN s.start_ms AND s.last_ms) ORDER BY coin, time", db, params=list(coins))
    df["side"] = df.side.map({"A": "B", "B": "A"})
    df["ntl"] = df.px * df.sz
    return df


def episodes(liq, gap_s=GAP_S):
    """One row per chain of liquidations on a coin. long_share: notional share of liquidated longs."""
    liq = liq.sort_values(["coin", "time"]).copy()
    new = (liq.coin != liq.coin.shift()) | (liq.time.diff() > gap_s * 1000)
    liq["ep"] = new.cumsum()
    liq["long_ntl"] = liq.ntl.where(liq.side == "A", 0.0)       # a liquidated long is sold
    g = liq.groupby("ep")
    ep = g.agg(coin=("coin", "first"), start=("time", "min"), end=("time", "max"), ntl=("ntl", "sum"),
               fills=("ntl", "size"), updates=("hash", "nunique"), long_ntl=("long_ntl", "sum"))
    ep["long_share"] = ep.long_ntl / ep.ntl
    return ep.drop(columns="long_ntl").reset_index(drop=True)


def thresholds(liq, q=QUANTILE, window=WINDOW):
    """Per coin, the trailing quantile of liquidated notional per 60-second bucket, empty buckets included.
    Each bucket only sees buckets before it. Returns {coin: Series indexed by bucket start}."""
    out = {}
    for coin, g in liq.groupby("coin"):
        s = g.set_index(pd.to_datetime(g.time, unit="ms")).ntl.resample("60s").sum()
        out[coin] = s.rolling(window, min_periods=MIN_HISTORY).quantile(q).shift(1)
    return out


def cascades(ep, thr, min_updates=MIN_UPDATES):
    ep = ep.copy()
    start = pd.to_datetime(ep.start, unit="ms").dt.floor("60s")
    ep["threshold"] = [thr[c].get(t) if c in thr else None for c, t in zip(ep.coin, start)]
    hit = (ep.ntl >= ep.threshold) & (ep.updates >= min_updates)
    return ep[hit.fillna(False)].reset_index(drop=True)


def decluster(cas, hours=DECLUSTER_H, market_min=MARKET_EVENT_MIN):
    """Keeps a cascade only if no cascade on the same coin started in the `hours` before it.
    Adds `event`: kept cascades on any coin starting within `market_min` minutes share one id."""
    cas = cas.sort_values("start").copy()
    prev = cas.groupby("coin").start.shift()
    cas = cas[prev.isna() | (cas.start - prev > hours * 3_600_000)].copy()
    cas["event"] = (cas.start.diff() > market_min * 60_000).cumsum()
    return cas.reset_index(drop=True)
