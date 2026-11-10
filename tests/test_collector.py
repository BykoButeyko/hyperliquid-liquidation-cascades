"""Recorder against an in-memory database, fed with messages shaped like the real websocket ones."""
import json
import sqlite3

import pytest

from collector import Recorder

T0 = 1_800_000_000_000


@pytest.fixture
def rec():
    return Recorder(sqlite3.connect(":memory:"))


def trade(coin, side, px, sz, ms):
    return {"coin": coin, "side": side, "px": str(px), "sz": str(sz), "time": ms, "hash": "0x1", "tid": ms,
            "users": ["0xa", "0xb"]}


def test_bars(rec):
    rec.handle({"channel": "trades", "data": [trade("BTC", "B", 100, 1, T0), trade("BTC", "A", 98, 2, T0 + 300),
                                              trade("BTC", "B", 101, 1, T0 + 900)]}, T0 + 900)
    assert rec.db.execute("SELECT count(*) FROM bars").fetchone()[0] == 0     # second still open
    rec.handle({"channel": "trades", "data": [trade("BTC", "B", 102, 1, T0 + 3000)]}, T0 + 3000)
    assert rec.db.execute("SELECT * FROM bars").fetchall() == [("BTC", T0 // 1000, 100, 101, 98, 101, 201, 196, 3)]
    # a late trade for an already written second is merged, not dropped
    rec.handle({"channel": "trades", "data": [trade("BTC", "A", 97, 1, T0 + 100)]}, T0 + 5000)
    assert rec.db.execute("SELECT low, sell_ntl, n FROM bars WHERE ts = ?", (T0 // 1000,)).fetchone() == (97, 293, 4)


def test_ctx_is_throttled(rec):
    c = {"funding": "0.0000125", "openInterest": "41959.8", "premium": "0.00003", "oraclePx": "76841.4",
         "markPx": "76843.2", "midPx": "76844.5", "impactPxs": ["76844.0", "76845.0"]}
    for dt in (0, 1000, 4999, 5000):
        rec.handle({"channel": "activeAssetCtx", "data": {"coin": "BTC", "ctx": c}}, T0 + dt)
    rows = rec.db.execute("SELECT ts, mark, oracle, oi, impact_ask FROM ctx ORDER BY ts").fetchall()
    assert rows == [(T0, 76843.2, 76841.4, 41959.8, 76845.0), (T0 + 5000, 76843.2, 76841.4, 41959.8, 76845.0)]


def test_depth(rec):
    lv = lambda px, sz: {"px": str(px), "sz": str(sz), "n": 1}
    book = {"coin": "ETH", "time": T0,
            "levels": [[lv(99.5, 1), lv(99.2, 1), lv(98.5, 2), lv(97, 5)],
                       [lv(100.5, 1), lv(101.5, 1), lv(103, 5)]]}
    rec.handle({"channel": "l2Book", "data": book}, T0)
    mid, b1, a1, b2, a2 = rec.db.execute("SELECT mid, bid_1pct, ask_1pct, bid_2pct, ask_2pct FROM depth").fetchone()
    assert mid == 100
    assert (b1, a1) == (pytest.approx(99.5 + 99.2), pytest.approx(100.5))
    assert (b2, a2) == (pytest.approx(99.5 + 99.2 + 197), pytest.approx(100.5 + 101.5))


def test_only_liquidation_fills_are_kept(rec):
    # shape copied from a real fill of the HLP liquidator vault
    liq = {"coin": "BTC", "px": "77505.0", "sz": "0.00702", "side": "B", "time": T0, "startPosition": "0.0",
           "dir": "Liquidated Cross Long", "closedPnl": "0.0", "hash": "0xc2ab", "oid": 1, "crossed": True,
           "fee": "0.0", "tid": 990389462529959, "feeToken": "USDC", "twapId": None,
           "liquidation": {"liquidatedUser": "0x97ef", "markPx": "77784.0", "method": "backstop"}}
    plain = dict(liq, tid=5, dir="Open Long")
    del plain["liquidation"]
    msg = {"channel": "userFills", "data": {"user": "0xliq", "fills": [liq, plain]}}
    rec.handle(msg, T0)
    rec.handle(msg, T0)                                   # replayed snapshot after a reconnect
    rows = rec.db.execute("SELECT coin, liquidated_user, mark_px, method, raw FROM liq_fills").fetchall()
    assert len(rows) == 1
    assert rows[0][:4] == ("BTC", "0x97ef", 77784.0, "backstop")
    assert json.loads(rows[0][4])["tid"] == 990389462529959


def test_session_tracks_last_commit(rec):
    rec.start_session(T0)
    rec.commit(T0 + 60_000)
    assert rec.db.execute("SELECT * FROM sessions").fetchall() == [(T0, T0 + 60_000)]
