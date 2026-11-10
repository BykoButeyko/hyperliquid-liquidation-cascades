"""archive.py on a small hand-made node_fills_by_block file."""
import json

import lz4.frame
import pytest

import archive

T0 = 1_760_054_400_000            # 2025-10-10 00:00 UTC


def fill(coin, px, sz, side, ms, crossed, tid, liq=None):
    x = {"coin": coin, "px": str(px), "sz": str(sz), "side": side, "time": ms, "startPosition": "0", "dir": "Close Long",
         "closedPnl": "-5", "hash": "0xabc", "oid": 1, "crossed": crossed, "fee": "0", "tid": tid, "feeToken": "USDC"}
    if liq:
        x["liquidation"] = {"liquidatedUser": liq, "markPx": str(px), "method": "market"}
    return x


BLOCKS = [
    {"local_time": "x", "block_time": "x", "block_number": 1, "events": [
        ["0xliq", fill("BTC", 100, 1, "A", T0, True, 1, liq="0xliq")],      # forced leg, taker
        ["0xmm", fill("BTC", 100, 1, "B", T0, False, 1, liq="0xliq")],      # maker leg, same tid
        ["0xa", fill("DOGE", 1, 5, "B", T0, True, 2)],                     # other coin, dropped
    ]},
    {"local_time": "x", "block_time": "x", "block_number": 2, "events": [
        ["0xb", fill("BTC", 103, 2, "B", T0 + 30_000, True, 3)],
        ["0xc", fill("BTC", 103, 2, "A", T0 + 30_000, False, 3)],
        ["0xd", fill("BTC", 99, 1, "A", T0 + 61_000, True, 4)],
    ]},
]


@pytest.fixture
def db(tmp_path):
    return archive.open_db(tmp_path / "h.db")


def test_load_fills(db, tmp_path):
    p = tmp_path / "0.lz4"
    with lz4.frame.open(p, "wt", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(b) for b in BLOCKS) + "\n")
    assert archive.load_fills(db, "k", archive.read_lz4(p)) == 2
    assert db.execute("SELECT user, liquidated_user, crossed FROM liq ORDER BY crossed").fetchall() == \
        [("0xmm", "0xliq", 0), ("0xliq", "0xliq", 1)]
    bars = db.execute("SELECT * FROM bars_1m ORDER BY ts").fetchall()
    assert bars == [("BTC", T0 // 1000, 100, 103, 100, 103, 206, 100, 2),
                    ("BTC", T0 // 1000 + 60, 99, 99, 99, 99, 0, 99, 1)]
    # loading the same file again changes nothing
    assert archive.load_fills(db, "k", archive.read_lz4(p)) == 0
    assert db.execute("SELECT sum(n) FROM bars_1m").fetchone()[0] == 3


def test_minute_split_across_files(db):
    archive.load_fills(db, "h0", [json.dumps({"events": [["0xa", fill("ETH", 10, 1, "B", T0 + 59_000, True, 1)]]})])
    archive.load_fills(db, "h1", [json.dumps({"events": [["0xa", fill("ETH", 9, 1, "A", T0 + 59_500, True, 2)]]})])
    assert db.execute("SELECT open, high, low, close, buy_ntl, sell_ntl, n FROM bars_1m").fetchall() == \
        [(10, 10, 9, 9, 10, 9, 2)]


def test_load_ctxs(db):
    csv = "time,coin,funding,open_interest\n2025-10-10T00:00:00Z,BTC,0.00001,30000\n2025-10-10T00:00:00Z,DOGE,0,5\n"
    assert archive.load_ctxs(db, "c", csv) == 1
    assert db.execute("SELECT coin, open_interest FROM asset_ctxs").fetchall() == [("BTC", 30000)]
