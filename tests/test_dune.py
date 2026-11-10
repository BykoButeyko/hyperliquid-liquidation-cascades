"""dune.py loader on CSVs shaped like Dune's downloads."""
import cascades as cz
import dune

LIQ = """coin,block_number,block_time,side,method,fills,accounts,notional_usd
BTC,100,2025-10-10 21:15:03.000 UTC,A,market,3,2,150000.5
BTC,101,2025-10-10 21:15:06.000 UTC,A,market,1,1,50000
ETH,100,2025-10-10 21:15:03.000 UTC,B,backstop,1,1,1000
"""
PRICES = """coin,block_minute,mark_price,oracle_price
BTC,2025-10-10 21:15:00.000 UTC,110000.5,110001
BTC,2025-10-10 21:16:00.000 UTC,,109000
"""


def test_load_and_episodes(tmp_path):
    (tmp_path / "liq_oct10.csv").write_text(LIQ)
    (tmp_path / "prices_oct.csv").write_text(PRICES)
    db = dune.load(tmp_path, tmp_path / "d.db")
    dune.load(tmp_path, tmp_path / "d.db")                  # loading twice changes nothing
    legs = dune.dune_legs(db)
    assert len(legs) == 3 and set(legs.side) == {"A", "B"}
    assert legs.time.iloc[0] == 1760130903000
    ep = cz.episodes(legs[legs.coin == "BTC"])
    assert ep[["ntl", "updates", "long_share"]].values.tolist() == [[200000.5, 2, 1.0]]
    assert db.execute("SELECT ts, close FROM bars_1m ORDER BY ts").fetchall() == \
        [(1760130900, 110000.5), (1760130960, 109000.0)]       # missing mark falls back to oracle
