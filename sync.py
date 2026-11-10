"""Copy the collector's new rows from the VPS to data/live.db on my own machine, then let the VPS prune rows over a year old.

  python sync.py

Reads the SSH target from HL_VPS (environment or .env, e.g. HL_VPS=root@my-vps) and uses my SSH key.
Safe to run as often as I like: it only asks for rows after the last rowid it stored for each table,
and the VPS only deletes rows this machine already has.
"""
import gzip, json, os, sqlite3, subprocess, sys
from pathlib import Path

from collector import SCHEMA
from archive import DATA

REMOTE = "cd /home/hlliq/hl-liquidations && sudo -u hlliq .venv/bin/python dump.py"
DB = DATA / "live.db"
TABLES = ["bars", "ctx", "depth", "liq_fills"]


def open_db(path=DB):
    path.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA + "CREATE TABLE IF NOT EXISTS sync_state(tbl TEXT PRIMARY KEY, wm INTEGER);")
    return db


def marks(db):
    have = dict(db.execute("SELECT tbl, wm FROM sync_state"))
    return {t: have.get(t, 0) for t in TABLES}


def apply(db, lines):
    """Inserts dump.py output. Returns the new watermarks; commits once at the end, so a broken
    transfer leaves the local database and the watermarks as they were."""
    wm, batch = marks(db), {}
    for line in lines:
        t, row = json.loads(line)
        batch.setdefault(t, []).append(row[1:])
        if t != "sessions":
            wm[t] = max(wm[t], row[0])
    for t, rows in batch.items():
        verb = "INSERT OR REPLACE" if t in ("bars", "sessions") else "INSERT OR IGNORE"
        db.executemany(f"{verb} INTO {t} VALUES({','.join('?' * len(rows[0]))})", rows)
    db.executemany("INSERT OR REPLACE INTO sync_state VALUES(?, ?)", wm.items())
    db.commit()
    return wm, {t: len(r) for t, r in batch.items()}


def vps():
    host = os.environ.get("HL_VPS")
    env = Path(__file__).with_name(".env")
    if not host and env.exists():
        host = next((l.split("=", 1)[1].strip() for l in env.read_text().splitlines() if l.startswith("HL_VPS=")), None)
    if not host:
        raise SystemExit("HL_VPS is not set. Put HL_VPS=user@host (or an ssh config alias) in .env or the environment.")
    return host


def ssh(cmd, wm):
    args = " ".join(f"{t}={v}" for t, v in wm.items())
    return subprocess.run(["ssh", "-o", "BatchMode=yes", vps(), f"{REMOTE} {cmd} {args}"], capture_output=True, check=True)


def main():
    db = open_db()
    raw = ssh("since", marks(db)).stdout
    wm, counts = apply(db, gzip.decompress(raw).decode().splitlines())
    print(f"copied {counts} ({len(raw) / 1e6:.1f} MB over the wire)", flush=True)
    print(ssh("prune", wm).stdout.decode().strip(), flush=True)


if __name__ == "__main__":
    sys.exit(main())
