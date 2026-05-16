# Testing notes — Sepolia gotchas

Operational footguns surfaced while running the wallet's full DeFi surface
(swap / Aave / Uniswap V3 LP) end-to-end on Sepolia. None of these are wallet
bugs — they're testnet realities that bite anyone running the surface for the
first time. Keep them around so the next person doesn't burn a day on the same
trail.

## Two different USDCs

`resolve_token("USDC")` returns **Circle Sepolia USDC**
`0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238`. Aave V3's
`resolve_aave_reserve("USDC")` returns **Aave's mock USDC**
`0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8` — a different ERC-20 that only
Aave's Sepolia pool understands.

So:

- `wallet approve set USDC <aave-pool> ...` → allowance on Circle USDC, useless
  for Aave
- `wallet aave supply USDC ...` → checks allowance on Aave's mock USDC

They never match. To supply to Aave on Sepolia:

```sh
wallet aave faucet USDC <amount> --broadcast --request-id "$(uuidgen)"
wallet approve set <aave-mock-usdc-address> <pool> <amount> --broadcast --request-id "$(uuidgen)"
wallet aave supply USDC <amount> --broadcast --request-id "$(uuidgen)"
```

Pass the mock-USDC address explicitly to `approve set`, not the `USDC` symbol.

## Aave V3 Sepolia supply caps

Stablecoin reserves (USDC, DAI, USDT) are supply-capped on Sepolia and reject
even 1-unit supplies with `aave:51 SUPPLY_CAP_EXCEEDED`. Working alternatives
observed 2026-05-17: **LINK** (supply APR ~217%, caps had room). WBTC / WETH /
AAVE / GHO show 0% supply APR — likely uncapped, but no demand to compound.

## Uniswap V3 NFPM / pool allowlist

For `wallet lp mint / increase / remove / collect` to work in agent mode,
`policy.json` needs:

- `contract_allowlist` += `0x1238536071E1c677A632429e3655c799b22cDA52`
  (NonfungiblePositionManager)
- `lp_pool_allowlist` += an **object** per pool (not a string), e.g. for
  USDC/WETH 0.05%:

  ```json
  {
    "token0": "0x1c7d4b196cb0c7b01d743fbc6116a902379c7238",
    "token1": "0xfff9976782d46cc05630d1f6ebab18b2324d6b14",
    "fee": 500
  }
  ```

  Schema is `{token0, token1, fee}`. V3 invariant: `token0 < token1` lowercased
  hex — the policy loader rejects the other ordering as a silent dead row.

## Sepolia Uniswap USDC/WETH 0.05% pool

- Pool address: `0x3289680dD4d6C10bb19b899729cda5eEF58AEfF1`
- Observed price 2026-05-17: ~13700–14900 USDC/ETH. Current tick around
  `181046` in May 2026.

The testnet pool has no oracle anchor — price drifts arbitrarily based on
whoever swapped most recently. Don't rely on any specific number.

## LP mint amount ratio

V3 mint requires `(amount0, amount1)` to match the pool's current ratio within
`slippage_bps`. The wallet does **not** compute this ratio for you. Wrong
ratio → `Price slippage check` revert.

- Symmetric narrow range straddling current tick → ratio matches spot price
- Asymmetric range (off-center tick) → effective ratio shifts; you must
  compute it from `(sqrtPriceLower, sqrtPriceUpper, sqrtPriceCurrent)`

Workflow when minting from a script:

1. Read `slot0().sqrtPriceX96` from the pool
2. Compute `amount0_per_L` and `amount1_per_L` for your `[tickLower, tickUpper]`
3. Pick one side as binding (deposit ≈ allowance), set the other side slightly
   above the ratio so it's not the binding constraint but `amount_min` still
   passes

This gap is intentional — see `ROADMAP.md` under Tier 3 "LP ratio helper" for
the decision to keep ratio math outside the wallet primitive.

## Approve allowance is consumed per use

`approve` sets an absolute value; each `transferFrom` (e.g. NFPM mint) decrements
it. After `wallet lp mint` consumes 50 USDC of allowance, the NFPM allowance
to USDC is back to 0 — a second mint needs a fresh `approve set`. Standard
ERC-20 semantics; don't expect "approve once, use forever" without unlimited
approval, which `deny_unlimited_approve: true` blocks by design.

## Block time

Sepolia ~12s. After `--broadcast`, sleep ~15–18s before querying state for
confirmation. Public RPCs sometimes lag an extra block.
