"""Cascade definitions and the event study on synthetic data."""
import numpy as np
import pandas as pd
import pytest

import cascades as cz
import study

DAY = 86_400_000
T0 = 1_760_000_000_000 // 60_000 * 60_000


def liqs(rows):
    """rows: (coin, ms, ntl, side, hash)"""
    df = pd.DataFrame(rows, columns=["coin", "time", "ntl", "side", "hash"])
    df["px"], df["sz"], df["method"] = 1.0, df.ntl, "market"
    return df


def test_episodes_chain_on_gap():
    df = liqs([("BTC", T0, 10, "A", "h1"), ("BTC", T0 + 50_000, 20, "A", "h2"), ("BTC", T0 + 200_000, 5, "B", "h3"),
               ("ETH", T0 + 10_000, 7, "B", "h4")])
    ep = cz.episodes(df).sort_values(["coin", "start"])
    assert ep[["coin", "ntl", "fills", "updates", "long_share"]].values.tolist() == \
        [["BTC", 30, 2, 2, 1.0], ["BTC", 5, 1, 1, 0.0], ["ETH", 7, 1, 1, 0.0]]


def background(coin, days=8):
    """One small liquidation every 10 minutes for `days` days."""
    return [(coin, T0 + i * 600_000, 1.0, "A", f"bg{i}") for i in range(days * 144)]


def test_cascade_needs_size_and_three_updates():
    t = T0 + 8 * DAY + 5 * 60_000
    burst = [("BTC", t + i * 3000, 100.0, "A", f"u{i}") for i in range(4)]       # 4 oracle updates
    whale = [("BTC", t + 2 * DAY, 1000.0, "A", "one")]                           # big but a single update
    df = liqs(background("BTC") + burst + whale)
    cas = cz.cascades(cz.episodes(df), cz.thresholds(df))
    assert len(cas) == 1 and cas.start[0] == t and cas.ntl[0] == 400


def test_no_threshold_before_min_history():
    df = liqs(background("BTC", days=2) + [("BTC", T0 + DAY, 500.0, "A", f"x{i}") for i in range(3)])
    assert cz.cascades(cz.episodes(df), cz.thresholds(df)).empty


def test_decluster():
    cas = pd.DataFrame({"coin": ["BTC", "BTC", "ETH", "BTC"],
                        "start": [T0, T0 + 3 * 3_600_000, T0 + 2 * 60_000, T0 + 9 * 3_600_000]})
    kept = cz.decluster(cas)
    assert kept[["coin", "start"]].values.tolist() == [["BTC", T0], ["ETH", T0 + 120_000], ["BTC", T0 + 9 * 3_600_000]]
    assert kept.event.tolist() == [0, 0, 1]


def test_event_study_signs_and_gate():
    idx = pd.date_range(pd.to_datetime(T0, unit="ms"), periods=20 * 1440, freq="60s")
    rng = np.random.default_rng(1)
    px = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 1e-4, len(idx)))), index=idx)
    s = idx[10 * 1440 + 613]
    px[s:] *= 0.95                                                 # longs liquidated, price drops 5%
    px[s + pd.Timedelta(minutes=30):] *= 1.025                     # then gives back half
    cas = pd.DataFrame({"coin": ["BTC"], "start": [int(s.value // 1e6)], "end": [int(s.value // 1e6)],
                        "long_share": [1.0], "event": [0]})
    m = study.measure(cas, {"BTC": px}, rng)
    assert m.impact[0] == pytest.approx(0.05, abs=0.003)
    assert m.fwd_60[0] == pytest.approx(-0.025, abs=0.003)          # give-back shows as negative
    assert abs(m.bench_60[0]) < 0.005
    assert "abn_60" not in study.summarize(m, rng).columns         # 1 cascade: counts only
    assert study.summarize(m, rng, min_events=1).abn_60[0] < -0.02
