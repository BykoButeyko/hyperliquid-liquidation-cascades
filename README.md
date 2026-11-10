# Liquidation cascades on Hyperliquid

I study how forced liquidations on Hyperliquid chain into cascades on BTC, ETH, SOL and HYPE perps: how often it happens, what comes before it, how far price moves, how much of the move comes back, and whether the liquidation levels my [whale-positioning dashboard](https://github.com/BykoButeyko/hyperliquid-whale-positioning) maps point to where cascades happen.

No findings yet. The study covers only data my collector recorded from 5 November 2026 onward, plus what Hyperliquid's free API returns (hourly funding). I don't use the paid archives, so there is no history before that date.

This is descriptive research, not trading advice.

Related: [how much of the bitcoin ETF bid is a basis trade](https://bykobuteyko.com/research/bitcoin-etf-basis/).

## How I find liquidations

The public trade stream does not mark liquidations. The flag sits on individual fills, which the API only returns per user. [SOURCE.md](SOURCE.md) lists what I checked against live data. The full record of every liquidation sits in requester-pays S3 archives (Hyperliquid's own and Hydromancer's) and in Dune's tables. I skipped all of them: the S3 buckets bill the downloader, and Dune's Hyperliquid tables need a trial that asks for a card. `archive.py` and `dune.py` can load those sources, but I haven't run either. Instead the collector watches HLP's liquidator vault and the nine largest market makers on these four coins. A maker's fill carries the liquidation tag when it takes the other side of one. That gives a sample of liquidations with exact timestamps, not the full record.

## Data

- `collector.py` keeps one websocket connection open and records, for the four coins: 1-second trade flow (OHLC, taker buy and sell notional), mark, oracle, funding and open interest every 5 seconds, and resting book notional within 1% and 2% of mid every 10 seconds. It also stores liquidation-tagged fills of the ten watched addresses as they happen (Hyperliquid allows ten per IP). It makes no REST calls.
- `sync.py` copies the collector's rows to my own machine every day. The VPS keeps its own full copy from 7 November 2026 16:59 UTC; I pruned the rows before that on 10 November, so the first two days exist only in my local copy.
- Hourly funding back to July 2025 comes from the free `fundingHistory` endpoint.

`archive.py` can load the S3 archives if I ever pay for them; until then it only loads funding. `cascades.py` turns liquidations into episodes and cascades, and `study.py` runs the event study.

## Method

[PLAN.md](PLAN.md) has the definitions and the method: what counts as a cascade, the event study against random timestamps matched on coin, hour and volatility, declustering, and the minimum number of events before I read any result.

## How much data before a result counts

I don't report a cascade result until it rests on at least 30 declustered cascades: 30 separate market events for a pooled result, and 30 on a coin for a claim about that coin. In the first five days the collector saw two market-wide liquidation bursts, so at that pace the pooled threshold takes around three months and per-coin claims longer. Until then the code prints counts only. I also rerun every result with the thresholds moved (PLAN.md) and with the first and second half of the sample apart; a result that flips between halves isn't a finding.

## Limitations

- The liquidation record is a sample: only the fills where a watched maker was the counterparty, plus HLP's backstop takeovers. The nine makers made 35% of maker notional on these coins in a 15-minute check on 10 November 2026, so a liquidation shows up only if one of them filled part of it. Notional per event is a slice of the real total. Makers pull quotes in fast markets, so the slice can shrink exactly when a cascade runs. I compare timing and counts more than dollar totals.
- From 5 to 10 November the collector watched HLP's two market making vaults instead, which turned out to make almost none of the maker flow on these coins. That stretch holds mostly backstop takeovers, so I treat 10 November as the start of the comparable liquidation sample.
- Everything starts on 5 November 2026. A few months of data may hold no large cascade at all, and the results only describe the market conditions those months had.
- The collector drops its connection every few hours and reconnects within seconds. The `sessions` table records connected time, and I count only events inside it.
- The book depth comes from Hyperliquid's aggregated book (3 significant figures, 20 levels a side). On ETH a price bucket is about 0.4% wide, so depth within 1% is coarse.
- The liquidation map from my whale-positioning dashboard covers about 4,000 large wallets. Small accounts make up much of a cascade and the map can't see them.
- Liquidation prices move as accounts add margin or change positions, so a level I map at one snapshot may be gone 15 minutes later.
- Cascades cluster in time and across coins, which leaves fewer independent events than the raw count suggests.

## Running it

```
pip install -r requirements.txt            # the collector only needs websockets
pip install -r requirements-analysis.txt   # archive.py, study.py and the tests
python collector.py        # writes hl_liquidations.db next to the script
python archive.py probe 20251010   # needs the AWS CLI with my own key; requester pays
python study.py
python sync.py             # needs HL_VPS=user@host in .env; copies new collector rows from the VPS to data/live.db
python -m pytest
```
