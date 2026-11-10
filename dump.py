"""VPS side of the sync to my own machine. sync.py runs it over SSH; I never run it by hand.

  python dump.py since bars=0 ctx=0 depth=0 liq_fills=0    gzip JSON lines of new rows to stdout
  python dump.py prune bars=N ctx=N depth=N liq_fills=N     delete rows up to rowid N that are older than KEEP_DAYS

Rows are tracked by rowid, so a fill that arrives late (a snapshot replayed after a reconnect) still syncs.
Bars change for a few seconds after they're written, so only bars older than BAR_LAG_S go out.
Prune only removes rows the laptop already has (rowid at or below what it reports) and keeps
the last KEEP_DAYS on the VPS whatever happens. Sessions are tiny and never pruned.
"""
import gzip, json, sqlite3, sys, time

from collector import DB

KEEP_DAYS = 365                  # the VPS keeps its own full copy; prune only trims what is over a year old
BAR_LAG_S = 120
TIME = {"bars": ("ts", 1), "ctx": ("ts", 1000), "depth": ("ts", 1000), "liq_fills": ("time", 1000)}


def since(db, marks, out):
    now = time.time()
    for t, wm in marks.items():
        q, args = f"SELECT rowid, * FROM {t} WHERE rowid > ?", [wm]
        if t == "bars":
            q, args = q + " AND ts < ?", args + [int(now - BAR_LAG_S)]
        for row in db.execute(q + " ORDER BY rowid", args):
            out.write(json.dumps([t, row]) + "\n")
    for row in db.execute("SELECT * FROM sessions"):
        out.write(json.dumps(["sessions", [None, *row]]) + "\n")


def prune(db, marks):
    now = time.time()
    for t, wm in marks.items():
        col, unit = TIME[t]
        cutoff = int((now - KEEP_DAYS * 86400) * unit)
        n = 0
        while True:                       # small batches so the collector never waits long for the lock
            c = db.execute(f"DELETE FROM {t} WHERE rowid IN (SELECT rowid FROM {t} WHERE rowid <= ? AND {col} < ? "
                           "LIMIT 20000)", (wm, cutoff))
            db.commit()
            n += c.rowcount
            if c.rowcount == 0:
                break
        print(f"{t}: pruned {n}")


def main(cmd, *pairs):
    marks = {k: int(v) for k, v in (p.split("=") for p in pairs)}
    assert set(marks) <= set(TIME), marks
    if cmd == "since":
        db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        with gzip.open(sys.stdout.buffer, "wt", encoding="utf-8") as out:
            since(db, marks, out)
    elif cmd == "prune":
        prune(sqlite3.connect(DB, timeout=30), marks)


if __name__ == "__main__":
    main(*sys.argv[1:])
