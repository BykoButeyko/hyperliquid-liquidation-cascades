# Research plan: liquidation cascades on Hyperliquid

Coins: BTC, ETH, SOL and HYPE perps.

## Questions

1. How often do liquidations cluster into cascades, and how large do they get?
2. What comes before a cascade: the price move, funding, open interest, book depth, and how close price sits to a cluster of liquidation levels?
3. How far does price move during a cascade, and how much of that move does it give back in the following hours?
4. Do the liquidation price clusters that my whale-positioning dashboard maps tell you where cascades will happen?

## Definitions

**Liquidation.** A fill with a `liquidation` object, counted once, on the liquidated user's leg (`liquidatedUser` equals the fill's own user). Market and backstop liquidations both count; I report them apart too. Notional is `px * sz` of that leg.

**Episode.** Per coin, I sort liquidations by time and chain them while the gap between neighbours stays at or under 60 seconds. The episode runs from its first liquidation to its last.

**Cascade.** An episode that meets both conditions:
- liquidated notional at or above the 99th percentile of 60-second liquidation notional for that coin over the trailing 30 days, and
- liquidations at three or more separate oracle updates, so one large account closed at once does not count as a chain.

I fix these thresholds before looking at outcomes and rerun everything at 30 and 120 second gaps and at the 95th and 99.5th percentiles. A result that only holds at one setting goes in the README as fragile.

## Data

| What | Source | Period |
|---|---|---|
| Liquidation fills, seen from the counterparty side | collector `liq_fills`: HLP's liquidator vault and the nine largest makers on these coins | comparable from 10 November 2026 (5 to 10 November watched HLP's market making vaults, which barely trade these coins) |
| Trade flow and prices | collector `bars` (1 second) | from 5 November 2026 |
| Open interest, mark, oracle, premium | collector `ctx` (5 seconds) | from 5 November 2026 |
| Book depth within 1% and 2% | collector `depth` (10 seconds) | from 5 November 2026 |
| Hourly funding | `fundingHistory`, free API | July 2025 to now |
| Liquidation levels of ~4,000 large wallets | Whale-positioning dashboard, positions table (`liq_px`, 15-minute snapshots) | from that project's start |

I only use free data for now. The complete liquidation record exists in requester-pays S3 archives and in Dune's tables. Both needed payment details (Dune's trial asks for a card), so I skipped the history; `archive.py` and `dune.py` can load either source if that changes. With the collector alone, a liquidation counts once per counterparty fill I see, its side is the opposite of the watched vault's side, and its notional is only the part the vault filled. Cascade thresholds are percentiles of that same sampled notional, so the definition stays consistent with what I can see. Only events inside a connected `sessions` interval count.

## Method

**Event study.** For each cascade I measure returns from its start to its end (impact), then from its end to 5, 15, 60 and 240 minutes later (recovery, as the share of the impact move given back). The benchmark is a set of random timestamps drawn from the same coin, the same hour of day and the same volatility tercile (1-hour realized volatility before the event). I report the difference with bootstrap confidence intervals, resampling whole days so events on the same day stay together.

**Declustering.** Cascades cluster. I keep a cascade only if no earlier cascade on the same coin started in the previous 4 hours. When several coins cascade within the same 5 minutes I count one market event and report the coins inside it, so a single crash does not show up as four independent results.

**Enough events before reading anything.** I don't look at the forward returns of a coin until it has 30 declustered cascades, and a pooled result needs 30 separate market events. Before that the code reports only counts. A result also has to hold in both halves of the sample. The first five days held two market-wide bursts, so the pooled threshold is around three months away at that pace.

**What precedes a cascade.** For every 5-minute window I build the features from the hour before it (return, realized volatility, funding, change in open interest, depth, and the notional of mapped liquidation levels within 1%, 2% and 5% of price on each side) and compare windows that start a cascade with windows that don't. Logistic regression with coin effects, plus plain rate tables by feature quintile, since a reader can check those by eye.

**Liquidation map test (question 4).** Using the whale-positioning snapshot taken before each window, I ask two things. First, does the chance of a cascade in the next hour rise with the mapped notional near price? Second, when a cascade happens, does price stop near the edge of the densest mapped cluster in the direction it moved, more often than it would on the random benchmark paths? The dashboard sees large wallets only, so a negative result could mean the map misses the small accounts that make up most of a cascade. Without the archive I can't measure how much of a cascade the map sees, so a negative result here stays ambiguous.

## What would be interesting either way

- If cascades recover most of their move within an hour, forced selling is pushing price away from where traders value it, and the size of that gap is worth reporting. If they don't recover, the cascade is part of a real repricing, and liquidations only speed it up.
- If mapped clusters predict cascades, the whale-positioning liquidation map is useful as a risk gauge. If they don't, either the visible clusters get defended or traded around, or the map misses the accounts that matter. The coverage check above tells those two apart.
- If cascades follow rising open interest and funding more than price moves, crowding sets them up. If price moves alone explain them, positioning data adds nothing.

## Order of work

1. Collector running on the VPS (done, 5 November 2026).
2. Sync to my own machine every day as a second copy (the VPS keeps the full history from 7 November 2026).
3. Watch counts per coin and month until the 30-event thresholds are met.
4. Episodes, cascades, counts per coin and month (question 1).
5. Event study on impact and recovery (question 3).
6. Precursors and the liquidation map test (questions 2 and 4). The map test can only use the period the dashboard has snapshots for.
