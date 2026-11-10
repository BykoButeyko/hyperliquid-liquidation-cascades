# How I identify liquidations on Hyperliquid

I checked each source below against live data on 2 November 2026 before relying on it.

## What the API exposes

The public `trades` websocket channel has no liquidation flag. A trade carries `coin, side, px, sz, time, hash, tid` and `users` as `[buyer, seller]`, nothing more.

The flag lives on fills. A fill from `userFills` (REST or websocket) can carry:

```
"liquidation": {"liquidatedUser": "0x...", "markPx": "77784.0", "method": "market" | "backstop"}
```

`userEvents` also has a `liquidation` event, and `userNonFundingLedgerUpdates` has a `liquidation` entry. All three need a user address. The docs list no channel that reports liquidations for every user.

## What I confirmed

1. **Backstop liquidations** go to HLP's liquidator sub-vault, `0xb0a55f13d22f66e6d495ac98113841b2326e9540` (a child of the HLP vault `0xdfc2...f303`, found with `vaultDetails`). Its fills carry `method: "backstop"` and the liquidated address. In its last 2000 fills I counted 224 tagged ones, including BTC and ETH.
2. **Market liquidations** show up on the liquidated user's own fills as ordinary taker fills (`crossed: true`, `dir: "Close Short"`) with `method: "market"`. I traced three wallets that the liquidator vault took over on ETH. Each had a market liquidation fill six seconds earlier, and all three fills shared one transaction hash.
3. **That hash belongs to the validators' oracle price update.** The explorer endpoint (`https://rpc.hyperliquid.xyz/explorer`, `txDetails`) returns a `SetGlobalAction` sent by `0x58e1b0e63c905d5982324fcd9108582623b8132e`, which carries oracle prices for every perp. Market liquidations execute inside that update, about every 3 seconds.
4. **The oracle hash alone does not identify a liquidation.** Over six minutes I matched 19 public trades to oracle update hashes. I looked up each taker's own fill and found zero liquidations: all 19 were regular `Close Long` / `Close Short` fills, most likely triggered stop or take-profit orders that also execute on the price update.
5. **The counterparty leg carries the tag too.** My first 25-minute watch of HLP's two market making vaults caught no liquidation. After the collector had run five days (5 to 10 November 2026), those two vaults had 2,976 fills with `method: "market"` and someone else's address as `liquidatedUser`, across dozens of coins. So the API stamps both legs of a market liquidation, and a count has to keep only the leg whose own user is the liquidated one.
6. **Which makers to watch.** In a 15-minute sample of trades on the four coins (10 November 2026), HLP's two market making vaults made 0% of the maker notional; they quote smaller coins. The nine largest makers made 35%. The collector now watches those nine plus the liquidator vault, the most Hyperliquid's ten-user limit allows.
7. **Zero hashes are something else.** About 20% of trades on these four coins have the hash `0x000...0`. The maker side of those fills shows up normally, but the taker addresses had no matching fill in their own history. I have not worked out what produces them, and I do not treat them as liquidations.

## What this means for the collector

Confirming one liquidation live costs a `userFillsByTime` call (weight 20 or more) per candidate taker, and finding candidates needs the oracle hashes from the explorer (weight 40 per call, and one page covers only about 30 seconds). The VPS shares a 1200 weight per minute budget with my other jobs, so I can't run that loop there.

So I split it:

- **Liquidation history** comes from Hyperliquid's node data archive: `node_fills_by_block` (and the older `node_fills`) in the requester-pays S3 bucket `hl-mainnet-node-data`. Hyperliquid's historical data page says `node_fills` matches the API format, which has the optional `liquidation` object, so the archive should give the full record for all users. The start date (July 2025 for `node_fills_by_block`) comes from Dwellir's docs, not Hyperliquid's. I have not downloaded a sample yet. Third-party docs (Dune, Dwellir) say the flag sits on both legs of a liquidation match, so a count has to keep only the liquidated user's leg. Dune's version of the table marks the forced leg with `fill_type = 'liquidation'`. In the raw fills I take the leg where the fill's own user equals `liquidatedUser`, and I will check that on a real file before counting anything.
- **Open interest, oracle and book depth** also have an archive: `asset_ctxs` (asset contexts, one CSV a day; I expect the API's fields, funding, open interest, mark and oracle, but have not opened a file yet) and `market_data` (L2 book snapshots per coin and hour) in the requester-pays bucket `hyperliquid-archive`. Hyperliquid uploads it about once a month and warns that data may be missing.
- **The live collector** keeps my own copy of open interest, mark and oracle prices, funding and book depth as they happen, plus 1-second trade flow, so recent weeks and any archive gaps are covered. It also stores tagged fills of HLP's liquidator vault as they happen, which gives a live check on the backstop side.
