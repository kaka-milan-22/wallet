# wallet — feature tour (verified end-to-end on Sepolia)

Every capability below is demonstrated with a real on-chain transaction from
the development account `0x34a910Df01b110E354dad7324E61462108Cb36c7`. Hashes
link to Sepolia Etherscan so you can verify the wallet did what it says.

The wallet is **agent-callable by design**: every command supports
`--json` (or `WALLET_JSON=1`), exits with structured error codes, and routes
every write through a policy / idempotency / audit pipeline. No browser, no
WalletConnect, no `window.ethereum` injection.

---

## 1. Account management

HD wallet (BIP-39 + BIP-44), mnemonic stored in `agent-vault` and read into
the signing process via a Unix FIFO so plaintext never lands on disk. The
`account create` and `account import` flows are TTY-only — they print the
mnemonic to stdout, which would enter an LLM's context window if invoked
under an agent.

```sh
# Generate a fresh wallet (TTY only)
wallet account create main
# →  prints 12-word mnemonic + instructs `agent-vault set wallet-main-mnemonic`

# Derive additional accounts from the same mnemonic
wallet account derive main --index 1 --as second

# Inspect
wallet account list
wallet account show main             # signed: yes (vault populated)
```

| State after creation | Value |
|---|---|
| `main` address | `0x34a910Df01b110E354dad7324E61462108Cb36c7` |
| `second` address | `0xFb0bD07524C7FBaa947CA4f7BBa445F9a749d126` |
| Vault key | `wallet-main-mnemonic` (shared between both — one mnemonic, many addresses) |
| Derivation path | `m/44'/60'/0'/0/0` (main), `…/0/1` (second) |

---

## 2. Balance and portfolio queries

```sh
wallet balance                       # native ETH on default account
wallet balance --token USDC          # ERC-20 by symbol
wallet balance --token 0x94a9...     # ERC-20 by address (works for Aave mocks)
wallet balance --all                 # every registered account + watched address
wallet portfolio                     # native + all known tokens, concurrent fetch
```

`portfolio` fans out balance reads in a thread pool so a 10-token sweep
costs ~one RTT total instead of N×RTT. JSON mode returns a stable schema:

```jsonc
// wallet --json portfolio | jq '.data.accounts[0].balances'
[
  {"symbol":"ETH","address":null,"amount_wei":"2191270666941913456","amount":"2.191270…","source":"native"},
  {"symbol":"USDC","address":"0x1c7D…","amount_wei":"15565555","amount":"15.565555","source":"builtin"},
  {"symbol":"WETH","address":"0xfFf9…","amount_wei":"0","amount":"0","source":"builtin"}
]
```

---

## 3. Transfers (ETH + ERC-20)

Default behaviour is **dry-run** — you must pass `--broadcast` to actually
send. Every broadcast goes through preview → policy → confirm → sign → audit.

```sh
wallet send 0xabc... 0.01                              # native, dry-run
wallet send @alice 0.01 --broadcast --yes              # native, broadcast (alias)
wallet send 0xabc... 50 --token USDC --broadcast --yes # ERC-20
```

**Demonstrated on-chain**:

| Op | Amount | Hash |
|---|---|---|
| Native, first ever | 0.001 ETH | [`0x5abce81b…`](https://sepolia.etherscan.io/tx/0x5abce81bf3b451515505d4d5a079283058de1d9280f53d438ee1ad9be22c2b6c) |
| Native, with `--request-id` | 0.0001 ETH | [`0xca5882b3…`](https://sepolia.etherscan.io/tx/0xca5882b33fb04af9ce7c4e38d6b8c3ff0d68e8b6bb2f65a3462c7ce4288c285c) |
| Native, larger | 0.02 ETH | [`0x3a1e1c16…`](https://sepolia.etherscan.io/tx/0x3a1e1c166d6f3ac7d53c216fb2721e1d6de26dc78d6ec707efadfbd045e7f51a) |

---

## 4. Approve / allowance management

```sh
wallet approve set USDC <spender> 100               # finite approve
wallet approve set USDC <spender> --unlimited       # blocked by default policy
wallet approve show USDC --spender 0x...            # current allowance
wallet approve revoke USDC <spender>                # set to 0
```

`deny_unlimited_approve: true` in policy refuses any approve of `2^256-1` —
common phishing pattern where a dApp asks for "unlimited" and an attacker
later drains it. Disabled by setting the policy field to `false` (TTY only).

---

## 5. Swap (DEX)

Default `--via auto` tries the **0x aggregator** first (best mainnet
pricing across Uniswap V2/V3/V4 + Curve + Balancer + ~100 DEXes) and
falls back to direct **Uniswap V3 SwapRouter02** when 0x has no route or
no API key configured (typical on testnets).

```sh
wallet swap ETH USDC 0.001                             # dry-run, auto route
wallet swap USDC WETH 100 --slippage-bps 50            # 0.5% slippage
wallet swap ETH USDC 0.001 --broadcast --yes           # real broadcast
wallet swap ETH USDC 0.001 --via uniswap-v3            # force direct UniV3
wallet swap ETH USDC 0.001 --via 0x                    # aggregator-only
```

**Demonstrated**: 0.001 ETH → 15.565555 USDC via Uniswap V3 0.05% fee tier
on Sepolia ([`0xe96c972c…`](https://sepolia.etherscan.io/tx/0xe96c972cf427380fceee841597af0b6e7300367c3bb1343b050ecadec2513058)).
The actual fill matched the quote exactly (thin Sepolia liquidity = no
price impact); on mainnet there would be small slippage which the
`min out` row in the preview caps.

Preview shows route + expected_out + min_out:

```
┃ swap route        ETH > 500bps > USDC                ┃
┃ expected out      15.565555 USDC                     ┃
┃ min out (0.5% slip) 15.487727 USDC                   ┃
```

---

## 6. Aave V3 lending

Full borrow / lend loop on Aave V3 Sepolia, no browser dApp needed.
Read operations (`positions`, `rates`) are free RPC calls; write
operations (`supply`, `withdraw`, `borrow`, `repay`, `faucet`) go through
the same confirm_and_broadcast pipeline as everything else.

### Read-only views

```sh
wallet aave positions                # your supplies / borrows + health factor
wallet aave rates                    # current supply / variable-borrow APRs
wallet aave rates --token USDC       # filter to one symbol
```

### Mock token faucet (no MetaMask required)

Aave's web faucet at staging.aave.com requires MetaMask. The on-chain
faucet contract is permissionless, so we expose it as a command:

```sh
wallet aave faucet WBTC 0.01 --broadcast --yes
```

**Demonstrated**:
- Mint 1000 mock USDC ([`0x7fb87d80…`](https://sepolia.etherscan.io/tx/0x7fb87d80588eabd20ddc59dcdccca304fd33a3104648178cec9578ac2a0509a8))
- Mint 0.01 mock WBTC ([`0x9b5264e7…`](https://sepolia.etherscan.io/tx/0x9b5264e73ee114d20f20edc4e705a2b31e5ae3bb61c2f46f93cd21eedbda9a5e))

### Supply / withdraw

```sh
wallet aave supply WBTC 0.005 --broadcast --yes              # supply (needs prior approve)
wallet aave withdraw WBTC 0.002 --broadcast --yes            # partial
wallet aave withdraw WBTC --max --broadcast --yes            # withdraw entire aToken balance
```

**Demonstrated** (one full cycle):

| Step | Op | Hash |
|---|---|---|
| 1 | approve WBTC → Aave Pool | [`0x3675d91e…`](https://sepolia.etherscan.io/tx/0x3675d91e1bad65fca28082152bf91f33b1058ea5a4497e4d7cca24ff69d8d377) |
| 2 | supply 0.005 WBTC | [`0x78ae3f30…`](https://sepolia.etherscan.io/tx/0x78ae3f30195a08312c523a82078fbacee139a23a94075a8c6c20549b4d7cceba) |
| 3 | withdraw 0.002 WBTC (partial) | [`0xd651449b…`](https://sepolia.etherscan.io/tx/0xd651449b277c315e0e01cec275d1884e294a573c3463d101d9840cacd9af415c) |
| 4 | withdraw WBTC --max (final) | [`0xc6dd9bad…`](https://sepolia.etherscan.io/tx/0xc6dd9bad0de4057fcda3cc53aebe921f760486649a4787e3976f5a301a42cead) |

Preview includes **current HF** with color (red < 1.1, yellow < 1.5, green ≥ 1.5).

### Borrow / repay

```sh
wallet aave borrow USDC 100 --broadcast --yes      # variable rate
wallet aave repay USDC --max --broadcast --yes     # repay entire variable debt
```

Preview adds **estimated HF after** and the **liquidation HF** line so the
delta is visible before signing:

```
┃ current HF            ∞                              ┃
┃ estimated HF after    2.250                          ┃
┃ liquidation HF        1.000 (Aave revert threshold)  ┃
```

The HF math reads Aave's oracle (`getAssetPrice` → USD × 1e8) and the
asset's per-reserve liquidation threshold via the data provider. For
borrow: `new_debt = current_debt + amount × price`. For withdraw:
`new_weighted_collateral = current_weighted - amount × price × asset_LT`.

**Demonstrated**:

| Step | Op | Hash | HF after |
|---|---|---|---|
| Borrow attempt 1 | 150 USDC | **blocked by policy** | predicted 1.500 < min 2.0 |
| Borrow attempt 2 | 100 USDC | [`0x9e4cbabe…`](https://sepolia.etherscan.io/tx/0x9e4cbabe57e1cb5762ba88fa00c5b0a8787d8891378480f26685db9d96452f61) | 2.250 ✓ |
| Repay USDC --max | $100.0003 | [`0x069b74d7…`](https://sepolia.etherscan.io/tx/0x069b74d7912166e8774d1cfbd4d148697dde04d154fd315fed1e4738a470400b) | ∞ (clean) |

After the full loop the position is back to zero — no residual debt.

---

## 7. Safety layers in action

Each demonstrated by a real on-chain reject:

### Policy fail-closed (no policy = deny all)

Before `wallet policy init`, every broadcast attempt returns:

```
error: policy_block — no-policy-configured-run-wallet-policy-init
```

Sets the precedent: **a fresh install can't lose money until the user
explicitly populates a policy** (`recipient_allowlist`, caps, etc.).

### recipient_allowlist

```sh
wallet send 0xUNKNOWN 0.001 --broadcast --yes
# → error: policy_block — recipient-not-in-allowlist
```

Only addresses listed by name or `0x…` in `recipient_allowlist` can
receive transfers; aliases (`@vitalik` from address book) work too.

### contract_allowlist

```sh
wallet swap USDC WETH 1 --broadcast --yes
# (if Uniswap router not in allowlist)
# → error: policy_block — swap-router-not-in-contract-allowlist
```

Applied to swap routers, Aave Pool, Aave Faucet — any contract the wallet
is about to call gets pre-flighted.

### `min_health_factor` gate (the killer feature for autonomous lending)

With `min_health_factor: 2.0` in policy, an agent trying to borrow more
than safe gets blocked **before** Aave's own HF >= 1 chain-level check:

```
wallet aave borrow USDC 150 --broadcast --yes
# preview shows estimated HF after = 1.500
# → error: policy_block — hf-would-drop-below-min:1.500<2.0
```

This is **strictly more conservative** than Aave's revert. Aave only
prevents instant liquidation (HF >= 1). The policy gate prevents
"borrowing right up to the cliff and getting liquidated by the next
price tick", which matters when an agent is operating without
real-time market judgment.

### Insufficient allowance pre-check

```sh
wallet aave repay USDC 99.5 --broadcast --yes
# (allowance was only 0.999 after a previous --max consumed most of it)
# → error: insufficient_allowance — allowance for USDC … is 999609 < 99500000 required
```

The envelope's `data.suggested_command` field tells agents exactly what to
do: `wallet approve set …`. Avoids burning gas on a guaranteed-revert tx.

### Aave error decoder

When Aave's pool reverts with a numeric error code, the wallet maps it
to a human-readable message:

```
wallet aave supply USDC 100 --broadcast --yes
# (supply cap saturated on Sepolia)
# → error: simulation_reverted — aave:51 (SUPPLY_CAP_EXCEEDED — Aave's supply cap is full
#   (common on testnets). Try a smaller amount or a different reserve.)
```

The JSON envelope's `data.aave_error_code: "51"` lets agents branch on the
numeric code while `data.aave_error_meaning` gives a description.

### Idempotency replay

```sh
RID=$(uuidgen)
wallet send second 0.0001 --broadcast --yes --request-id "$RID"
# → submitted: 0x12e1153375aeeeb4…  (broadcast)

wallet send second 0.0001 --broadcast --yes --request-id "$RID"
# → submitted: 0x12e1153375aeeeb4…  (replayed_idempotent, same hash, no second broadcast)
```

The store at `~/.wallet/idempotency.json` keeps `(request_id, fingerprint,
tx_hash)` triples. Replaying with the same id returns the cached result;
reusing the id with DIFFERENT params raises `idempotency_mismatch` so
agents don't accidentally double-spend on retry.

---

## 8. JSON output for agents

Set `WALLET_JSON=1` (or `--json` per call) and every command emits a
single-line JSON envelope:

```sh
WALLET_JSON=1 wallet send second 0.0001 --broadcast --yes --request-id "$(uuidgen)" | jq .
```

```jsonc
// success envelope
{
  "ok": true,
  "command": "send",
  "chain": "sepolia",
  "data": {
    "phase": "broadcast",
    "kind": "native_transfer",
    "from": "0x34a9…",
    "to": "0xFb0b…",
    "amount_wei": "100000000000000",
    "amount": "0.0001",
    "unit": "ETH",
    "tx_hash": "0xca5882b3…",
    "explorer_url": "https://sepolia.etherscan.io/tx/…",
    "request_id": "ab81ab82-f553-4722-a996-233c9c821fd9",
    "outcome": "broadcast",
    "nonce": 5,
    "gas": 21000,
    "estimated_fee": "0.000021"
  }
}

// error envelope — same shape across all error codes
{
  "ok": false,
  "command": "aave.borrow",
  "chain": "sepolia",
  "error": "policy_block",
  "code": "policy_block",
  "reason": "hf-would-drop-below-min:1.500<2.0",
  "data": {...}
}
```

Stable error codes: `validation_error`, `policy_block`,
`idempotency_mismatch`, `not_found`, `rpc_error`, `vault_error`,
`simulation_reverted`, `aborted`, `missing_request_id`,
`confirmation_required`, `tty_required`, `no_route`,
`insufficient_allowance`, `insufficient_funds`, `superseded`.

**stderr is never polluted** by JSON output, so `wallet --json X | jq`
always works. `--explain` (or `WALLET_EXPLAIN=1`) dumps policy /
idempotency decision details to **stderr**, keeping stdout clean.

---

## 9. Audit trail

Every signing attempt (broadcast, rejected, replayed, user_aborted)
appends one JSON-line to `~/.wallet/audit.log`. The file is `O_APPEND` so
concurrent writers never tear; mode is `0644`; **no CLI command reads it**
so an agent can't use the log to plan its next move.

Excerpt from the demonstration run:

```
{"kind":"aave_faucet","outcome":"broadcast","hash":"0x9b5264..."}     ← 0.01 WBTC minted
{"kind":"approve","outcome":"broadcast","hash":"0x3675d9..."}          ← WBTC → Aave Pool
{"kind":"aave_supply","outcome":"broadcast","hash":"0x78ae3f..."}      ← 0.005 WBTC supplied
{"kind":"aave_withdraw","outcome":"broadcast","hash":"0xd65144..."}    ← 0.002 WBTC out
{"kind":"aave_borrow","outcome":"rejected"}                            ← 150 USDC (policy blocked)
{"kind":"aave_borrow","outcome":"broadcast","hash":"0x9e4cba..."}      ← 100 USDC (allowed)
{"kind":"approve","outcome":"broadcast","hash":"0xebb830..."}          ← USDC → Aave Pool
{"kind":"approve","outcome":"broadcast","hash":"0x8995e8..."}          ← USDC → Aave Pool re-approve (buffer for interest)
{"kind":"aave_repay","outcome":"broadcast","hash":"0x069b74..."}       ← repay --max ($100.0003)
{"kind":"aave_withdraw","outcome":"broadcast","hash":"0xc6dd9b..."}    ← withdraw WBTC --max
```

The full log lets a human reconstruct **exactly** what the wallet did
and when, separate from chain explorers (which don't tag which caller
mode each tx came from).

---

## 9b. Stuck-tx recovery

When a broadcast sits in mempool unconfirmed (base-fee spiked above its
`maxFeePerGas`, or priority too low), `wallet tx` provides three commands
that work directly on the EIP-1559 mempool replacement protocol — same
mechanism MetaMask / Rabby surface as "Cancel" / "Speed Up":

```sh
wallet tx pending                              # list local broadcasts with no receipt yet
wallet tx cancel <nonce>  [--speedup-pct N]    # dry-run by default; preview the 0-value self-send
wallet tx cancel <nonce> --broadcast --request-id $(uuidgen)
wallet tx replace <nonce> --broadcast --request-id $(uuidgen)   # re-send original calldata, bumped gas
```

Mechanics: both build a new tx with same `from` + same `nonce` and gas
bumped to `max(old × 1.10, base_fee × 2 + new_priority)` so it clears the
EIP-1559 110% replacement floor AND current chain pricing. `cancel` is a
0-value self-send (the original never lands); `replace` rebroadcasts the
original `(to, value, data, gas)` recovered from the RPC.

Both flow through the standard `confirm_and_broadcast` pipeline so policy
/ idempotency / audit gate the recovery the same way they gate a fresh
write. Race outcomes are first-class:

- Replacement lands first → original is displaced, audit logs `recovery: cancel/replace` + `old_tx_hash` + `original_kind`.
- Original lands first → RPC returns `nonce too low`; envelope emits `code: superseded`, `reason: original_landed_first`, exit code 0. Idempotency cache records `outcome=superseded` so an agent retrying the same `request_id` gets the cached race outcome instead of a double-broadcast.

Policy: `tx cancel` bypasses `recipient_allowlist` (you're sending to
yourself) but only when `description.is_self_send_for_cancel=True` AND
`to == from` AND `amount_wei == 0` — any deviation is treated as an
attacker-forged label and blocked. `tx replace` delegates to the original
op's category so the replacement faces the same gates the original did.

---

## 10. What's NOT in scope (yet)

See [`ROADMAP.md`](../ROADMAP.md). Deferred items the wallet **deliberately**
does not do today:

- **Hardware wallet (Ledger / Trezor)** — the single remaining code gate
  before any non-trivial mainnet value should live on this wallet. See
  [`why_hard_wallet.md`](why_hard_wallet.md) for the full threat-model
  rationale.
- **WalletConnect / browser dApp injection** — by design. Use MetaMask if
  you need to interact with web dApps; this wallet talks to contracts /
  aggregator APIs directly.
- **Multi-RPC consensus reads** — single-RPC SPOF risk; ROADMAP Tier 2.
- **Permit2 / EIP-712 typed-data signing** — 0x v2 supports it but our
  policy explicitly denies unlimited approves, which is what Permit2
  needs; deferred to ROADMAP Tier 3.
- **Compound V3 / Spark / Morpho lending** — Aave V3 only for now.
- **Cross-chain bridges** — single-chain focus.

---

## Reproducing the test from scratch

```sh
# 0. Install
git clone git@github.com:kaka-milan-22/wallet.git && cd wallet && uv sync

# 1. Configure (TTY, one-time)
export WALLET_SEPOLIA_RPC=https://ethereum-sepolia.publicnode.com
export ETHERSCAN_API_KEY=…    # free at https://etherscan.io/myapikey
uv run wallet account create main
agent-vault set wallet-main-mnemonic    # paste the mnemonic
uv run wallet policy init
$EDITOR ~/Library/Application\ Support/wallet/policy.json
# add contract_allowlist:
#   Uniswap V3 SwapRouter02:  0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E
#   Aave V3 Pool:             0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951
#   Aave V3 Faucet:           0xC959483DBa39aa9E78757139af0e9a2EDEb3f42D

# 2. Get Sepolia ETH (any of these)
open https://sepolia-faucet.pk910.de/                                    # PoW, no account
open https://cloud.google.com/application/web3/faucet/ethereum/sepolia   # Google account, 0.05 ETH/day

# 3. Run the demo
uv run wallet swap ETH USDC 0.001 --broadcast --yes
uv run wallet aave faucet WBTC 0.01 --broadcast --yes
uv run wallet approve set 0x29f2D40B0605204364af54EC677bD022dA425d03 \
    0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951 0.01 --broadcast --yes
uv run wallet aave supply WBTC 0.005 --broadcast --yes
uv run wallet aave positions
uv run wallet aave borrow USDC 100 --broadcast --yes
uv run wallet aave repay USDC --max --broadcast --yes
uv run wallet aave withdraw WBTC --max --broadcast --yes
```

Each command emits an Etherscan link on success; click through to verify
on-chain. The whole demo costs ~$0 in ETH (Sepolia is free) and exercises
every safety layer at least once.
