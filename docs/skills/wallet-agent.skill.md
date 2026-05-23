---
name: wallet-agent
description: |
  Use the local DeFi wallet CLI (`wallet`) for ERC-20 / ETH operations on
  EVM-compatible chains. Read operations (balance, history, account list) are
  always safe; signing operations require explicit user authorization in the
  current turn AND must conform to the local policy at ~/.wallet/policy.json.
  Always invoke with `WALLET_JSON=1` (or `--json`) so output is machine-
  parseable.
---

# Output mode

Always set `WALLET_JSON=1` once at the start of your shell session (or pass
`--json` on every call). Every command emits a single-line JSON envelope:

- success: `{"ok":true,"command":"<name>","chain":"<chain>","data":{...}}`
- error:   `{"ok":false,"command":"<name>","chain":"<chain>","error":"<code>","code":"<code>","reason":"<details>"}`

Parse with `jq` or `json.loads`. **Do NOT regex-parse human-readable rich
output** — it is not a stable API.

Error `code` values are enumerable: `validation_error`, `policy_block`,
`idempotency_mismatch`, `not_found`, `rpc_error`, `vault_error`,
`simulation_reverted`, `aborted`, `missing_request_id`, `confirmation_required`,
`tty_required`, `no_route`, `insufficient_allowance`, `insufficient_funds`,
`superseded`, `tx_reverted`. Branch on these, not on `reason` text.

For debugging only, add `--explain` or `WALLET_EXPLAIN=1`. Decision details
go to **stderr** so stdout JSON stays clean.

## Flag position conventions

- Global flags (`--json`, `--quiet`, `-q`, `--explain`, `--debug`) belong
  **before** the subcommand: `wallet --json account list`, NOT
  `wallet account list --json`. The latter is rejected with a
  `No such option` error + an in-line hint pointing at the correct form.
- Subcommand flags (`--broadcast`, `--wait`, `--slippage-bps`, …) go
  **after** the subcommand.
- `wallet send` is `<TO> <AMOUNT>` (not the more common `<AMOUNT> <TO>`).
  Passing them reversed produces a `validation_error` carrying a
  directly-runnable `did you mean wallet send <addr> <amount>?` line —
  if you see that in `.reason`, just re-run with the suggested form.

# Wallet usage rules for agents

The `wallet` CLI defends against agent abuse with a four-layer stack:
**this skill (guidance)** → **policy (hard limits)** → **idempotency (no
double-spend on retry)** → **audit log (forensics)**. Follow these rules so
the policy gate doesn't reject your operations.

## Always-safe (no signing, no money movement)

Set this once per session:

```sh
export WALLET_JSON=1
```

Then invoke read commands and parse with jq:

```sh
wallet balance --token USDC  | jq -r '.data.balances[0].amount'
wallet portfolio             | jq '.data.accounts[0].balances'
wallet account list          | jq -r '.data.accounts[].name'
wallet account show <name>   | jq -r '.data.signed'        # true / false
wallet history -n 20         | jq '.data.transactions[] | {dir:.direction, hash}'
wallet token list            | jq -c '.data.tokens'
wallet book list             | jq '.data.entries'
wallet watch list            | jq '.data.entries'
wallet policy show           | jq '.data.policy'
wallet info                  | jq .
wallet tx pending            | jq '.data.pending[] | {hash:.tx_hash, nonce, age_sec}'   # read-only; see "Recovering a stuck broadcast"

# Aave V3 read-only (Phase 2 PR3)
wallet aave positions        | jq '.data.summary'                                   # totals + HF
wallet aave positions        | jq '.data.supplies[] | {symbol, amount}'             # per-asset
wallet aave rates            | jq '.data.rates[] | {symbol, supply: .supply_apr_pct, borrow: .variable_borrow_apr_pct}'
wallet aave rates --token USDC | jq '.data.rates[0]'

# Pre-flight: which reserves still have supply cap room? null cap = unlimited.
# When supply_cap_used_pct >= 100 on the target reserve, `aave supply` will
# revert with SUPPLY_CAP_EXCEEDED — check here first instead of catching the
# simulation revert.
wallet aave rates | jq '.data.rates[] | select(.supply_cap_used_pct != null) | {sym:.symbol, used:.supply_cap_used_pct, cap:.supply_cap}'

# Aave V3 supply / withdraw / faucet (Phase 2 PR4) — writes, need --broadcast + --request-id
wallet aave faucet USDC 1000 --broadcast --yes --request-id "$(uuidgen)"  # claim mock tokens (testnet only)
wallet aave supply USDC 10                                                # dry-run preview, shows current HF
wallet aave supply USDC 10 --broadcast --yes --request-id "$(uuidgen)"    # real supply
wallet aave withdraw USDC 5 --broadcast --yes --request-id "$(uuidgen)"   # partial withdraw
wallet aave withdraw USDC --max --broadcast --yes --request-id "$(uuidgen)"  # full withdraw

# Aave V3 borrow / repay (Phase 2 PR5)
wallet aave borrow USDC 50                                                # dry-run, shows current + estimated HF
wallet aave borrow USDC 50 --broadcast --yes --request-id "$(uuidgen)"    # variable-rate borrow
wallet aave repay USDC 50 --broadcast --yes --request-id "$(uuidgen)"     # partial repay
wallet aave repay USDC --max --broadcast --yes --request-id "$(uuidgen)"  # full repay
# When policy.min_health_factor is set, borrow/withdraw is pre-flighted: if the
# estimated post-op HF would drop below the threshold, you get
# `error: policy_block, code: hf-would-drop-below-min:X<Y`. Don't try to brute-
# force around it — ask the user to either reduce the amount or change policy.

# Notes for agents:
#  - The Aave Pool address (e.g. 0x6Ae43..8951 on Sepolia) must be in
#    policy.contract_allowlist. If you hit `aave-pool-not-in-contract-allowlist`,
#    tell the user to add the address in their terminal.
#  - Aave uses its own (often mock) token deployments distinct from chain
#    builtin tokens — `wallet aave rates` lists exactly the symbols available.
#  - supply requires prior `wallet approve set <token> <pool> <amount>`.
#    When `<pool>` is the Aave V3 pool address the wallet auto-resolves
#    `<token>` symbols (e.g. `USDC`) to Aave's reserve list rather than
#    the chain's builtin token — you do NOT need to pass the mock address
#    manually. Look for `auto-resolving USDC to Aave reserve 0x…` on stderr.
#  - withdraw triggers Aave's HF-must-stay-above-1 check; if it would revert,
#    you get `simulation_reverted` before any signing happens.
```

## Sending ETH or ERC-20 (signing — needs user OK + policy compliance)

Workflow EVERY time:

1. **Confirm intent with the user in plain language** before touching anything.
2. **Dry-run first** — no `--broadcast`. Parse the preview JSON and show key
   fields to the user (`from`, `to`, `amount`, `unit`, `estimated_fee`):
   ```sh
   wallet send <to> <amount> | jq '{from:.data.from,to:.data.to,amount:(.data.amount+" "+.data.unit),fee:.data.estimated_fee}'
   ```
3. **Wait for user's explicit "yes / go ahead / broadcast"** in the same turn.
4. **Generate a fresh request-id** (idempotency key — required for all
   broadcasts you initiate; never reuse):
   ```sh
   RID=$(python -c "import uuid; print(uuid.uuid4())")
   ```
5. **Broadcast with `--broadcast --yes --request-id`** (in JSON mode `--yes`
   is required because there's no interactive confirmation):
   ```sh
   wallet send <to> <amount> --broadcast --yes --request-id "$RID" \
     | jq -r '.data.tx_hash'
   wallet approve set <token> <spender> <amount> --broadcast --yes --request-id "$RID"
   wallet approve revoke <token> <spender> --broadcast --yes --request-id "$RID"
   ```

   **Prefer `--wait`** for any tx whose follow-up depends on the previous
   one having mined. With `--wait` the call blocks until receipt and
   merges `data.wait = {status, block_number, gas_used, effective_fee, …}`
   into the envelope; without it you have to `sleep 18 && wallet …`
   poll yourself and risk reading stale state. `--wait-timeout` defaults
   to 60s and reads `WALLET_WAIT_TIMEOUT` env var.

   ```sh
   wallet send <to> <amount> --broadcast --yes --wait --request-id "$RID" \
     | jq '{tx:.data.tx_hash, status:.data.wait.status, block:.data.wait.block_number, fee:.data.wait.effective_fee}'
   ```

   Wait semantics:

   - `wait.status == "success"` → exit 0, tx mined and didn't revert.
   - `wait.status == "reverted"` → envelope becomes `ok: false`,
     `code: tx_reverted`, exit **5**. Broadcast succeeded; the tx
     failed on-chain. Surface `wait.block_number` + `wait.gas_used` to
     the user. Auto-retry policy depends on **what** reverted:
     - **Deterministic** reverts (HF / cap / slippage / policy
       violations — anything the wallet pre-flighted but the chain
       state diverged at execution) → do NOT retry; same params will
       fail the same way. Adjust amounts or surface to the user.
     - **Transient** reverts → one retry with a fresh
       `--request-id` is fine; if it reverts a second time treat as
       deterministic. Observed transient cases:
       - Aave `borrow` / `withdraw --max` on a high-utilization
         reserve where the pool liquidity dipped mid-block
         (Sepolia LINK at 200%+ borrow demand throws
         `LIQUIDITY_LESS_THAN_AVAILABLE` intermittently even when
         your HF=inf). One retry typically succeeds as borrows are
         repaid in subsequent blocks.
       - Uniswap V3 `lp remove` / `lp increase` whose slippage
         tolerance is tight relative to in-block tick drift on
         active pools — `amount_min0/min1` checks just barely fail
         on the racy block, pass on the next. If retry also fails,
         widen `--slippage-bps` rather than retrying a third time.
       - Generic nonce race / RPC blip during private-relay submit
         that surfaced as a revert in the receipt.
   - `wait.status == "timeout"` → envelope stays `ok: true`, exit 0,
     `tx_hash` is valid. The tx may still mine; re-query via the
     explorer or wait longer.

   `--wait` composes with idempotency: replaying the same request-id
   while passing `--wait` polls receipt for the **cached** tx_hash, so
   a retry after a network hiccup returns the same receipt instead of
   re-broadcasting.
6. **If the call fails with `error: rpc_error`** (transient network), RETRY
   using the SAME request-id. The wallet's idempotency store returns the
   cached result and never double-broadcasts:
   ```jsonc
   // First call returns broadcast:
   {"ok":true,"data":{"phase":"broadcast","tx_hash":"0x...","outcome":"broadcast"}}
   // Retry with same request-id:
   {"ok":true,"data":{"phase":"idempotent_replay","tx_hash":"0x...","outcome":"replayed_idempotent"}}
   ```
7. **If `.ok == false`**, show the user `.error` and `.reason` verbatim. Use
   the error code → action table below to suggest the right next step.

   Note: same-`request-id` retry handles RPC failures **before** the tx hit
   the mempool. If a tx already broadcast but is sitting unconfirmed (low
   fee, base-fee spike), use `wallet tx replace / cancel` — see "Recovering
   a stuck broadcast" below. Never re-broadcast the same logical op with a
   new `--request-id`.

## Recovering a stuck broadcast

A successful broadcast may still sit in mempool unconfirmed (priority too
low, base-fee spiked above its `maxFeePerGas`). The nonce is occupied, so
**every subsequent op from the same account is queued behind it**. Use
`wallet tx`:

```sh
wallet tx pending --account <name>                     # list local unconfirmed txs
wallet tx cancel <nonce> --broadcast --request-id "$(uuidgen)"   # free the nonce
wallet tx replace <nonce> --broadcast --request-id "$(uuidgen)"  # re-send original, bumped gas
```

How it works:

- `cancel` is a **0-value self-send** at the same nonce with gas bumped to
  `max(old × 1.10, base_fee × 2 + bumped_priority)` — clears the EIP-1559
  110% replacement floor AND current chain pricing. The original never
  lands; the nonce is consumed by a no-op.
- `replace` recovers the original `(to, value, data, gas)` via
  `eth.getTransaction(cached_hash)` and rebroadcasts at the same nonce
  with bumped fees. Use this when you wanted the operation to happen but
  it priced itself out.

Race outcomes are first-class — handle both:

- **Replacement wins**: envelope `outcome=broadcast`, `tx_hash=<new>`,
  audit adds `recovery: cancel|replace` + `old_tx_hash: <original>`.
- **Original landed first**: envelope `ok: false`, `code: superseded`,
  `reason: original_landed_first`. Exit code is 0 (benign race outcome,
  not failure). Idempotency cache records `outcome=superseded` so a
  retry with same `--request-id` gets the cached race outcome — no
  double-broadcast.

Policy: `tx cancel` bypasses `recipient_allowlist` (you're sending to
yourself) only when `to == from` AND `amount_wei == 0`; any deviation is
treated as a forged label and blocked. `tx replace` delegates to the
original op's category — a replacement of an `aave borrow` faces the
same HF / allowlist gates the original did.

Discovery: `wallet tx pending` lists only broadcasts originally made
through THIS wallet (sourced from local `idempotency.json`). It cannot
recover txs broadcast from MetaMask or another wallet.

## Forbidden

These commands either bear secrets or weaken the security model. **Do not
invoke them under any circumstances**:

- `wallet account create` — generates a new mnemonic to stdout (would enter your context window)
- `wallet account import` — accepts a mnemonic on stdin
- `wallet policy init` — creates / overwrites the policy file
- Editing `~/.wallet/policy.json` directly via Edit / Write / sed / shell redirect
- `--policy-bypass` — silently rejected in agent context anyway, but don't try
- `--unlimited` on `wallet approve set` — refused by default policy
- Reading `~/.wallet/audit.log` programmatically to plan next actions
- `agent-vault set / get / rm / import` — these have TTY checks that will reject you anyway

If the user wants any of these done, **tell them to run the command in their
own terminal** (in Claude Code, `! <command>` drops to the shell).

## Swapping tokens (Phase 2)

`wallet swap` with the default `--via auto` first asks the 0x aggregator for
the best route; if 0x has no API key or no route, it falls back to a direct
Uniswap V3 single-hop swap.

```sh
# Dry-run preview — shows route provider + expected_out + min_out (after slippage)
wallet swap ETH USDC 0.001 --slippage-bps 50 \
  | jq '{provider:.data.swap_provider, route:.data.swap_route,
         expected:.data.swap_amount_out_expected, min:.data.swap_amount_out_min}'

# Real broadcast — needs fresh --request-id and --yes
wallet swap ETH USDC 0.001 --broadcast --yes --request-id "$(uuidgen)" \
  | jq -r '.data.tx_hash'

# Pin the provider explicitly when needed
wallet swap ETH USDC 0.001 --via 0x        # aggregator only (errors if no API key)
wallet swap ETH USDC 0.001 --via uniswap-v3  # skip aggregator entirely
```

Notes on `--via auto`:

- 0x aggregator pricing usually wins on mainnet for liquid pairs (Uniswap V2/V3,
  Curve, Balancer, etc. all routed through one quote).
- On Sepolia (or any chain with thin aggregator liquidity), 0x often returns
  no-route and the wallet quietly degrades to direct Uniswap V3.
- The `swap_provider` field in the JSON envelope tells you which path was
  actually used — `"0x"` or `"uniswap_v3"`.

**Native ETH** input doesn't need `approve` (router wraps via msg.value).
**ERC-20** input requires prior `wallet approve set <token> <router> <amount>`.

**Native ETH output**: when `token_out` is the chain's native symbol (e.g.
`ETH`) on the `uniswap-v3` route, the wallet emits a multicall with
`unwrapWETH9` so the user receives real ETH, not WETH. You don't need a
follow-up unwrap step — checking the user's ETH balance after the swap
will show the swap proceeds delivered as native.

If you get `error: insufficient_allowance`, the envelope's `data` includes
a `suggested_command` field — just run that, then retry the swap with the
same logical params (use a NEW request-id for the approve, then ANOTHER
new request-id for the swap).

The swap **router** (e.g. `0x3bFA...` for Uniswap V3 on Sepolia) must be in
`policy.contract_allowlist`. If you get
`error: policy_block, code: swap-router-not-in-contract-allowlist`, the user
needs to add it in their terminal — you cannot modify the policy file.

## Uniswap V3 LP (Phase 2)

```sh
# Read-only: list NFPM positions for an account, with in-range status + current pool tick
wallet lp positions --account main | jq '.data.positions[] | {id:.token_id, pair, in_range, tick_lower, tick_upper, current_tick, sqrt:.sqrt_price_x96}'

# Mint a new position
# Required: --fee (100/500/3000/10000), --tick-lower/--tick-upper aligned to the
# fee tier's spacing (100→1, 500→10, 3000→60, 10000→200), --amount-a / --amount-b.
wallet lp mint USDC WETH --fee 500 --tick-lower 187500 --tick-upper 187650 \
  --amount-a 10 --amount-b 0.00145 --slippage-bps 500 \
  --account main --broadcast --yes --wait --request-id "$(uuidgen)"

# Add to existing position (token pair order normalized automatically)
wallet lp increase <token_id> USDC WETH --amount-a 5 --amount-b 0.00075 \
  --slippage-bps 500 --broadcast --yes --wait --request-id "$(uuidgen)"

# Burn liquidity (does NOT collect — feeds tokens into tokens_owed0/1)
wallet lp remove <token_id> --percent 100 --slippage-bps 500 \
  --broadcast --yes --wait --request-id "$(uuidgen)"

# Sweep owed amounts (post-remove and accrued fees) to recipient (default: sender)
wallet lp collect <token_id> --broadcast --yes --wait --request-id "$(uuidgen)"
```

The NFPM contract (`0x1238…cDA52` on Sepolia) and each target pool must be
in policy:
- `policy.contract_allowlist` must include the NFPM address
- `policy.lp_pool_allowlist` must include an `{token0, token1, fee}` object
  (lowercased hex, `token0 < token1`) for every pool you mint/increase against.
  String entries are silently dropped — the schema is strict.

### `lp mint` revert with expected-amounts enrichment

The dominant `lp mint` failure mode is `Price slippage check`: your
`(amount-a, amount-b)` ratio doesn't match the pool's current ratio at
`current_tick`, so the binding side under-fills and `amount_min`
fails. The wallet enriches that revert with the actual `(amount0,
amount1)` Uniswap would have pulled — branch on the new fields
instead of computing sqrt math:

```jsonc
{
  "ok": false,
  "code": "simulation_reverted",
  "data": {
    "lp_pool_address": "0x3289680d…",
    "lp_current_tick": 187535,
    "lp_binding_side": "token0",
    "lp_amount0_desired_wei": "10000000",
    "lp_amount1_desired_wei": "5000000000000000",
    "lp_amount0_expected_wei": "9999999",
    "lp_amount1_expected_wei": "435422385813452",
    "suggestion": "at tick 187535 the binding side is token0; reduce --amount-b to ~0.000435422385813452 WETH (or widen the tick range)"
  }
}
```

Use `data.suggestion` verbatim — it tells you which CLI flag to lower
and to what. The `lp_amount{0,1}_expected_wei` are decimals-applied raw
wei, ready to feed back via `parse_units`. `slippage-bps` in the wallet
is interpreted as `amount_min = desired * (1 - slip)`, **not** a
price-band tolerance — so if `desired_a / desired_b` deviates from
pool ratio by more than `slip/10000`, you need to reduce desired on
the over-funded side rather than raising slippage.

## When the wallet rejects you

Common policy errors and how to react:

| Reason | Meaning | Right next step |
|---|---|---|
| `no-policy-configured` | First-time setup; no policy file exists | Tell user: "Run `wallet policy init` in your terminal, then add allowlist entries" |
| `recipient-not-in-allowlist` | The `to` address is not authorized | Ask user to add it: `wallet book add <alias> <addr>` then update policy |
| `max-per-tx-exceeded:ETH:X` | Amount exceeds per-tx cap | Ask user to lower the amount OR raise the cap in TTY |
| `max-per-day-exceeded:ETH:X` | Today's outflow + this tx exceeds daily cap | Wait until tomorrow or raise cap in TTY |
| `unlimited-approve-denied` | Tried `--unlimited` approve | Use a finite approval amount instead |
| `spender-not-in-contract-allowlist` | approve target contract not allowed | Ask user to add the contract to policy |
| `swap-router-not-in-contract-allowlist` | swap router (e.g. Uniswap V3) not allowed | Ask user to add the router address to `contract_allowlist` |
| `no_route` | No DEX pool has liquidity for this pair/amount | Try a smaller amount; try a different output token; on Sepolia liquidity is thin |
| `insufficient_allowance` | ERC-20 not approved for the swap router yet | Run the `suggested_command` from envelope.data, then retry |
| `insufficient_funds` | Sender balance < value + gas fee | Reduce the amount, or fund the account; never retry with the same amount |
| `superseded` | `tx cancel/replace` race — original landed first | Benign; the operation already settled on chain. No retry needed |
| `tx_reverted` | `--wait` saw the tx revert on-chain (exit 5) | Broadcast succeeded but execution failed. Surface `data.wait.{block_number,gas_used}` + the explorer URL. Retry only when the revert is transient (Aave reserve liquidity dip, nonce race); never retry on deterministic reverts (HF / cap / slippage / policy). One retry max — second revert is deterministic by definition |
| `first-send-blocked-for-agent` | Recipient never seen before | Ask user to confirm the address and add to book or watch |
| `missing-request-id-for-agent` | You forgot `--request-id` | Generate a fresh uuid and retry |
| `idempotency-mismatch` | You reused a request-id for different params | Generate a fresh uuid; never reuse |

When you hit one of these, **show the user the rejection reason verbatim
and suggest the action above**. Do not silently try alternatives that might
also violate policy (e.g., trying multiple addresses to find one that works
— that's exfiltration behavior).

## Mental model

The wallet treats you as a **constrained signing delegate**:

- You hold no private keys (those live in agent-vault, accessed via FIFO; you
  never see the mnemonic).
- You can propose operations within the policy envelope; you cannot widen it.
- You are responsible for: **clear user confirmations**, **fresh request-ids**,
  **handling rejections gracefully**.
- The wallet is responsible for: **enforcing limits**, **deduping retries**,
  **logging everything for the human to audit later**.

If the user asks you to do something the policy blocks, your job is to
explain that to the user and suggest the legitimate path (TTY edit / wait /
adjust amount). Do not try to circumvent.

## Sepolia DeFi smoke test — worked recipe

When the user asks you to "re-run the full DeFi surface on Sepolia" (or
similar), use this sequence as the skeleton. Every broadcast uses
`--wait` so you read confirmed state, not stale mempool. `RID()` is a
shorthand for `--request-id "$(uuidgen)"`.

```sh
export WALLET_JSON=1
export WALLET_WAIT_TIMEOUT=60

# 1. Read-only smoke (no signing)
wallet account list                                       | jq -r '.data.accounts[].name'
wallet portfolio --account main                           | jq '.data.accounts[0].balances'
wallet aave positions --account main                      | jq '.data.summary'
wallet lp positions --account main                        | jq '.data.positions | length'
wallet policy show                                        | jq '.data.policy.recipient_allowlist'

# 2. Native send (proves signing + broadcast + wait)
wallet send 0xFb0bD07524C7FBaa947CA4f7BBa445F9a749d126 0.001 \
  --account main --broadcast --yes --wait --request-id "$(uuidgen)"

# 3. Swap (USDC ↔ WETH, requires allowance to router first)
wallet approve set USDC 0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E 10 \
  --account main --broadcast --yes --wait --request-id "$(uuidgen)"
wallet swap USDC WETH 10 --via uniswap-v3 \
  --account main --broadcast --yes --wait --request-id "$(uuidgen)"

# 4. Aave full cycle on LINK (Sepolia stablecoin caps are saturated — see step 0)
#    Step 0: check the cap first
wallet aave rates | jq '.data.rates[] | select(.symbol=="LINK") | .supply_cap_used_pct'
wallet aave faucet LINK 50 --account main --broadcast --yes --wait --request-id "$(uuidgen)"
# approve uses Aave's mock LINK automatically because spender is the aave_v3 pool
wallet approve set LINK 0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951 50 \
  --account main --broadcast --yes --wait --request-id "$(uuidgen)"
wallet aave supply LINK 50 --account main --broadcast --yes --wait --request-id "$(uuidgen)"
wallet aave borrow LINK 1 --account main --broadcast --yes --wait --request-id "$(uuidgen)"
# repay --max needs the borrowed token's balance to cover (debt + accrued interest);
# faucet a small buffer first so testnet's high borrow APR doesn't underflow
wallet aave faucet LINK 0.5 --account main --broadcast --yes --wait --request-id "$(uuidgen)"
wallet approve set LINK 0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951 2 \
  --account main --broadcast --yes --wait --request-id "$(uuidgen)"
wallet aave repay LINK --max --account main --broadcast --yes --wait --request-id "$(uuidgen)"
wallet aave withdraw LINK --max --account main --broadcast --yes --wait --request-id "$(uuidgen)"

# 5. LP mint / increase / remove / collect on USDC/WETH 0.05%
#    Read current tick first so the range straddles it
CUR_TICK=$(wallet lp positions --account main | jq -r '.data.positions[0].current_tick')
echo "current tick = $CUR_TICK; use [CUR_TICK-75, CUR_TICK+75] aligned to spacing 10"
# Approvals to NFPM
wallet approve set USDC 0x1238536071E1c677A632429e3655c799b22cDA52 50 \
  --account main --broadcast --yes --wait --request-id "$(uuidgen)"
wallet approve set WETH 0x1238536071E1c677A632429e3655c799b22cDA52 0.01 \
  --account main --broadcast --yes --wait --request-id "$(uuidgen)"
# Mint — the recipe's amount-b is approximate; if tick has drifted it will
# revert with `Price slippage check`. The envelope's `data.suggestion`
# tells you exactly what to retry with, e.g.
#   "reduce --amount-b to ~0.00109738975071465 WETH (or widen the tick range)"
# Just re-run with that value (and a fresh request-id).
wallet lp mint USDC WETH --fee 500 --tick-lower <CUR-75> --tick-upper <CUR+75> \
  --amount-a 10 --amount-b 0.00145 --slippage-bps 500 \
  --account main --broadcast --yes --wait --request-id "$(uuidgen)"
# Find the token_id of the position we just minted. The `lp positions` list
# order is NFT enumeration (NOT creation time) so `.positions[-1]` is wrong
# for accounts with multiple positions. Filter on the tick range + non-zero
# liquidity. Parens around `(.liquidity_wei | tonumber)` are REQUIRED —
# jq otherwise tries to compare a string and errors with "Cannot index
# string". This is the most common jq footgun in the wallet's envelopes.
TOKEN_ID=$(wallet lp positions --account main \
  | jq -r '.data.positions
    | map(select((.liquidity_wei | tonumber) > 0
                 and .tick_lower==<CUR-75>
                 and .tick_upper==<CUR+75>))
    | .[0].token_id')
wallet lp increase $TOKEN_ID USDC WETH --amount-a 5 --amount-b 0.00075 --slippage-bps 500 \
  --account main --broadcast --yes --wait --request-id "$(uuidgen)"
wallet lp remove $TOKEN_ID --percent 100 --slippage-bps 500 \
  --account main --broadcast --yes --wait --request-id "$(uuidgen)"
wallet lp collect $TOKEN_ID --account main --broadcast --yes --wait --request-id "$(uuidgen)"
```

Branch hygiene during the run:

- After each broadcast, check `.data.wait.status == "success"` before
  moving on. If `"reverted"`, stop and surface the explorer URL.
- For Aave: if `aave rates` shows `supply_cap_used_pct >= 100` for the
  target reserve, switch reserves rather than catching the
  `SUPPLY_CAP_EXCEEDED` revert. Stablecoins on Sepolia are over-cap;
  LINK / WBTC / WETH / AAVE / EURS have null cap.
- For LP: if `lp mint` reverts with `Price slippage check`, read
  `data.lp_amount{0,1}_expected_wei` and `data.suggestion` — both tell
  you the exact retry command without needing sqrt math.
- For all approves: use the symbol form (`USDC`, `WETH`, `LINK`); the
  wallet picks the correct underlying when the spender is the Aave
  pool. Never hard-code mock token addresses in agent code — they
  change between Aave testnet redeployments.
