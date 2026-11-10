"""dump.py and sync.py against two temporary databases, without SSH."""
import io, sqlite3, time

import dump
import sync
from collector import Recorder

NOW = int(time.time())
OLD = NOW - (dump.KEEP_DAYS + 2) * 86400


def remote(tmp_path):
    db = sqlite3.connect(tmp_path / "remote.db")
    Recorder(db)                                          # creates the schema
    rows = [("BTC", OLD, 1, 1, 1, 1, 0, 0, 1), ("BTC", NOW - 600, 2, 2, 2, 2, 0, 0, 1),
            ("BTC", NOW - 10, 3, 3, 3, 3, 0, 0, 1)]       # the last bar is still settling
    db.executemany("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?)", rows)
    db.execute("INSERT INTO ctx VALUES('BTC', ?, 1, 1, 1, 0, 5, 0, 1, 1)", (OLD * 1000,))
    db.execute("INSERT INTO liq_fills VALUES('0xv', 'BTC', 7, ?, 1, 1, 'A', 'x', '0xh', '0xu', 1, 'market', 0, '{}')",
               (NOW * 1000,))
    db.execute("INSERT INTO sessions VALUES(?, ?)", (OLD * 1000, NOW * 1000))
    db.commit()
    return db


def transfer(src, dst):
    out = io.StringIO()
    dump.since(src, sync.marks(dst), out)
    return sync.apply(dst, out.getvalue().splitlines())


def test_sync_then_prune(tmp_path):
    src, dst = remote(tmp_path), sync.open_db(tmp_path / "local.db")
    wm, counts = transfer(src, dst)
    assert counts == {"bars": 2, "ctx": 1, "liq_fills": 1, "sessions": 1}
    assert dst.execute("SELECT ts FROM bars ORDER BY ts").fetchall() == [(OLD,), (NOW - 600,)]

    # a late fill (old time, new rowid) still syncs on the next run, nothing is copied twice
    src.execute("INSERT INTO liq_fills VALUES('0xv', 'BTC', 8, ?, 1, 1, 'A', 'x', '0xh', '0xu', 1, 'market', 0, '{}')",
                (OLD * 1000,))
    src.commit()
    wm, counts = transfer(src, dst)
    assert counts == {"sessions": 1, "liq_fills": 1}
    assert dst.execute("SELECT count(*) FROM liq_fills").fetchone()[0] == 2

    dump.prune(src, wm)
    # old rows that the laptop has are gone; recent ones and the unsynced bar stay
    assert src.execute("SELECT ts FROM bars ORDER BY ts").fetchall() == [(NOW - 600,), (NOW - 10,)]
    assert src.execute("SELECT count(*) FROM ctx").fetchone()[0] == 0
    assert src.execute("SELECT count(*) FROM liq_fills").fetchone()[0] == 1
    assert src.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_prune_never_deletes_unsynced_rows(tmp_path):
    src = remote(tmp_path)
    dump.prune(src, {"bars": 0, "ctx": 0, "depth": 0, "liq_fills": 0})
    assert src.execute("SELECT count(*) FROM bars").fetchone()[0] == 3
