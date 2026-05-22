# wallet

> An **AI-agent-native EVM wallet**. A Python CLI built for LLM agents,
> scripts, and cron jobs to call — with safety rails an autonomous caller
> can't bypass.

[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Testnet: Sepolia](https://img.shields.io/badge/testnet-Sepolia-green.svg)](https://sepolia.etherscan.io/)
[![Status: Beta](https://img.shields.io/badge/status-beta-orange.svg)](ROADMAP.md)

---

## Why this exists

In 2026, an LLM agent can already read your portfolio, decide to rebalance,
and broadcast the transaction. The wallets you trust today — MetaMask,
Frame, Rabby — were built for **humans clicking buttons in a browser**.
The moment you hand the keyboard to an agent, three failure modes show up
that no browser wallet was designed to handle:

| Failure mode | What happens |
|---|---|
| **Key leakage** | Mnemonic / private key flows through any process the agent can see — including LLM provider servers when the agent's tool output is fed back as context |
| **Unbounded action** | A prompt injection or off-by-one → "`send 1000 USDC to 0x...`" with no spending cap, no recipient check, no allowlist gate |
| **Retry double-spend** | Agent retries on RPC timeout → two on-chain transactions, one user intent |

`wallet` is built agent-first. The agent gets a CLI it can call with `--json`
output. Every signing path goes through a **four-layer safety stack** that
catches a different class of failure. The private key never crosses the
agent's process boundary.

## How it works

```
                 ┌─────────────────────────────────────────────────────────┐
                 │  LLM agent (Claude Code / Cursor / cron / arbitrary)    │
                 │      ↓  shell call: wallet send @alice 10 --json ...    │
                 └─────────────────────────────────────────────────────────┘
                                          │
   ┌──────────────────────────────────────┼──────────────────────────────────────┐
   │                       wallet CLI — 4-layer safety stack                     │
   │                                                                             │
   │   1. skill        agent-facing guidance: dry-run first, fresh --request-id  │
   │   2. policy       hard pre-broadcast block: caps, allowlists, HF gate       │
   │   3. idempotency  same --request-id → cached tx_hash, no double-spend       │
   │   4. audit        append-only JSON-lines record of every attempt            │
   └─────────────────────────────────────────────────────────────────────────────┘
                                          │
                  signer process (FIFO-piped mnemonic, never on disk)
                                          │
                                          ▼
                          ┌──────────────────────────────┐
                          │  EVM chain (Sepolia / L1 / L2) │
                          └──────────────────────────────┘
```

The mnemonic lives encrypted in [`agent-vault`](https://github.com/kaka-milan-22/agent-vault),
retrieved into the signing process via a Unix FIFO — kernel pipe, never written
to `/tmp` or any file the agent can read.

## How it compares

|                              | MetaMask + browser agent | `ethers.js` + raw key | **`wallet`** |
|------------------------------|:---:|:---:|:---:|
| Callable from terminal       | ✗ | ✓ | ✓ |
| JSON output for agent parsing| ✗ | partial | ✓ |
| Key never seen by agent      | partial | ✗ | ✓ |
| Pre-broadcast policy gate    | ✗ | ✗ | ✓ |
| Retry-safe (idempotency)     | ✗ | DIY | ✓ |
| Append-only audit log        | ✗ | DIY | ✓ |
| Health-factor gate (Aave)    | ✗ | ✗ | ✓ |
| LP pool allowlist            | n/a | ✗ | ✓ |
| Browser dApp interaction     | ✓ | ✗ | ✗ |

`wallet` is **not** a MetaMask replacement. It doesn't speak WalletConnect,
doesn't inject `window.ethereum`, doesn't render dApp UIs. Keep MetaMask
alongside for browser dApp interaction; they cover different use cases
(human-in-front-of-UI vs. agent-in-front-of-terminal).

## Who this is for

- ✓ Builders running LLM agents that touch DeFi — trading bots, treasury
  rebalancers, automated payments, on-chain cron jobs
- ✓ Anyone uneasy about giving a browser-based agent unrestricted access to a
  MetaMask popup
- ✓ Teams that need an audit trail of every signing attempt, not just
  successful broadcasts
- ✗ Not a MetaMask replacement
- ✗ Not yet mainnet-recommended for non-trivial value — see [`ROADMAP.md`](ROADMAP.md);
  the single remaining blocker is Ledger integration ([rationale](docs/why_hard_wallet.md))

## Quick demo (Sepolia, ~5 minutes)

```sh
# 1. Install
uv sync

# 2. Set RPC + Etherscan
export WALLET_SEPOLIA_RPC=https://ethereum-sepolia.publicnode.com
export ETHERSCAN_API_KEY=...

# 3. Generate an HD wallet (mnemonic shown once; you store it in agent-vault)
uv run wallet account create main
agent-vault set wallet/main/mnemonic        # paste mnemonic; agent will never see it again

# 4. Fund the address from a Sepolia faucet, then:
uv run wallet balance
uv run wallet portfolio                     # all tokens at once
uv run wallet send 0x... 0.001 --broadcast  # asks y/N; --yes to skip

# 5. Try a swap and a lend, agent-style (--json output for parsing)
uv run wallet --json swap ETH USDC 0.001 --broadcast --yes
uv run wallet --json aave supply USDC 10 --broadcast --yes
```

For the full command tree see [Daily commands](#daily-commands) below.

## What you get today (Sepolia tested end-to-end)

- **Accounts** — BIP-39 HD wallet; mnemonic encrypted in
  [`agent-vault`](https://github.com/kaka-milan-22/agent-vault), retrieved
  into the signing process via Unix FIFO (kernel pipe, never on disk)
- **Transfers** — ETH + ERC-20 send, ERC-20 approve / allowance / revoke
- **Swap** — `wallet swap` via 0x aggregator (spender + `transaction.to`
  pinned to the chain-known AllowanceHolder, defending against
  quote-tampering attacks) with automatic fallback to direct Uniswap V3.
  When the output is native ETH, the Uniswap V3 route emits a multicall
  with `unwrapWETH9` so the user receives real ETH instead of WETH
- **Lending** — `wallet aave supply / withdraw / borrow / repay` against
  Aave V3, with a configurable `min_health_factor` policy gate that blocks
  risky ops *before* Aave's chain-level HF >= 1 revert
- **Liquidity** — `wallet lp mint / increase / remove / collect / positions`
  for Uniswap V3 LP NFTs, gated by an `lp_pool_allowlist` policy that names
  which `(token0, token1, fee)` pools the agent may touch. Rebalance is a
  compose-of-primitives, not a built-in strategy (see
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md))
- **On-chain faucet** — `wallet aave faucet` mints testnet mock tokens
  without needing a browser wallet
- **Sepolia operational notes** — testnet footguns (two USDC reserves, Aave
  supply caps, LP mint ratio math, NFPM/pool allowlist schema) collected in
  [`docs/TESTING.md`](docs/TESTING.md)

## Install

```sh
uv sync
```

The CLI is exposed as `wallet`:

```sh
uv run wallet --help
```

## One-time setup (Sepolia)

```sh
# 1. RPC endpoint (any of these works)
export WALLET_SEPOLIA_RPC=https://ethereum-sepolia.publicnode.com
# or use Alchemy / Infura with your own key

# 2. Etherscan v2 API key for `wallet history`
export ETHERSCAN_API_KEY=...   # https://etherscan.io/myapikey

# 3. Confirm config
uv run wallet info
```

## Account creation flow

```sh
# 1. Generate a new HD wallet
uv run wallet account create main

# Output shows the mnemonic ONCE. Write it down.
# It also prints the next command:

# 2. (in your terminal — NOT inside an LLM agent)
agent-vault set wallet/main/mnemonic
# paste the mnemonic when prompted

# 3. Verify
uv run wallet account show main
# `signed: yes (vault populated)` confirms vault wiring.
```

To **import** an existing mnemonic instead:

```sh
uv run wallet account import main           # prompts hidden input
agent-vault set wallet/main/mnemonic        # paste the same mnemonic
```

To **derive** more addresses from the same mnemonic:

```sh
uv run wallet account derive main --index 1 --as second
```

Both `main` and `second` share the same vault key. One mnemonic, many addresses.

## Daily commands

```sh
# Balance
uv run wallet balance                       # default account, native
uv run wallet balance --token USDC
uv run wallet balance --all                 # every account + watched address
uv run wallet balance --account 0xd8dA...   # one-shot lookup by bare 0x address (no watch add needed)

# Portfolio — all tokens at once (native + builtin + user-registered)
uv run wallet portfolio                     # default account, every known token
uv run wallet portfolio --all               # every account + watched, all tokens
uv run wallet --json portfolio | jq '.data.accounts[].balances[] | select(.amount != "0")'

# Transfer (DRY-RUN by default — pass --broadcast to actually send)
uv run wallet send @alice 0.01
uv run wallet send 0xabc... 50 --token USDC
uv run wallet send @alice 0.01 --broadcast  # asks y/N before signing
uv run wallet send @alice 0.01 --broadcast --yes   # skip confirmation

# ERC-20 approval
uv run wallet approve set USDC <spender> 100              # dry-run
uv run wallet approve set USDC <spender> 100 --broadcast
uv run wallet approve set USDC <spender> --unlimited --broadcast
uv run wallet approve show USDC --spender <spender>
uv run wallet approve revoke USDC <spender> --broadcast

# Swap — default `--via auto` tries 0x aggregator, falls back to direct Uniswap V3
uv run wallet swap ETH USDC 0.001                          # dry-run preview, auto route
uv run wallet swap USDC WETH 100 --slippage-bps 50         # 0.5% slippage
uv run wallet swap ETH USDC 0.001 --broadcast --yes        # real broadcast
uv run wallet swap ETH USDC 0.001 --via uniswap-v3         # force direct UniV3 (skip 0x)
uv run wallet swap ETH USDC 0.001 --via 0x                 # force aggregator (will fail without API key)
# 0x aggregator needs WALLET_ZEROX_API_KEY (free at dashboard.0x.org)
# Router addresses (Uniswap V3 router; 0x AllowanceHolder) must be in policy.contract_allowlist
# ERC-20 input requires prior `wallet approve set <token> <router> <amount>`

# Aave V3 — read-only views (Phase 2 PR3)
uv run wallet aave positions                # your supplies / borrows + health factor
uv run wallet aave positions --account second
uv run wallet aave rates                    # current supply / variable-borrow APRs for every reserve
uv run wallet aave rates --token USDC       # filter to one symbol
uv run wallet --json aave positions | jq '.data.summary.health_factor'

# Aave V3 — supply / withdraw + on-chain faucet (Phase 2 PR4)
# Aave's testnet uses its own mock tokens. To claim them without a browser
# (skip staging.aave.com), the faucet contract is callable directly:
uv run wallet aave faucet USDC 1000                            # dry-run
uv run wallet aave faucet USDC 1000 --broadcast --yes          # mint 1000 mock USDC to your address
# Sepolia Pool 0x6Ae43...8951 and Faucet 0xC959...3f42D must both be in policy.contract_allowlist.
uv run wallet aave supply USDC 10                              # dry-run
uv run wallet approve set USDC 0x6Ae43...8951 10 --broadcast   # one-time approve to Aave Pool
uv run wallet aave supply USDC 10 --broadcast --yes            # supply 10 Aave-USDC
uv run wallet aave withdraw USDC 5 --broadcast --yes           # partial withdraw
uv run wallet aave withdraw USDC --max --broadcast --yes       # withdraw entire aToken balance
# Aave reverts if a withdraw would drop your HF < 1.0; our `_simulate` surfaces this
# as a clean `simulation_reverted` error before any signing.

# Aave V3 — borrow / repay (Phase 2 PR5)
uv run wallet aave borrow USDC 50                              # dry-run, shows current + estimated HF
uv run wallet aave borrow USDC 50 --broadcast --yes            # borrow $50 at variable rate
uv run wallet approve set 0x94a9...8 0x6Ae43...8951 50 --broadcast --yes  # one-time approve to repay
uv run wallet aave repay USDC 50 --broadcast --yes             # repay partial
uv run wallet aave repay USDC --max --broadcast --yes          # repay entire variable debt
# Set `min_health_factor` in policy.json to block borrow/withdraw before HF drops
# below your comfort line (Aave's own threshold is 1.0; setting 1.5+ gives margin).

# Uniswap V3 LP — position management
uv run wallet lp positions                                     # list NFT positions + in-range status
uv run wallet lp positions --account second
uv run wallet lp mint ETH USDC --fee 500 \
    --tick-lower -887270 --tick-upper 887270 \
    --amount-a 0.0037 --amount-b 50 \
    --slippage-bps 300                                         # dry-run; full-range USDC/WETH
uv run wallet lp mint ETH USDC --fee 500 \
    --tick-lower -887270 --tick-upper 887270 \
    --amount-a 0.0037 --amount-b 50 --slippage-bps 300 \
    --broadcast --yes --request-id "$(uuidgen)"
uv run wallet lp increase <tokenId> --amount-a 0.001 --amount-b 10 --broadcast --yes ...
uv run wallet lp remove <tokenId> --percent 100 --broadcast --yes ...   # burn liquidity
uv run wallet lp collect <tokenId> --broadcast --yes ...                 # sweep fees + freed principal
# NFPM (0x1238...CDA52 on Sepolia) goes in policy.contract_allowlist.
# Every pool you touch goes in policy.lp_pool_allowlist as a
# {"token0": "0x...", "token1": "0x...", "fee": N} object — NOT a pool address
# string. token0 < token1 lowercased (V3 invariant; loader rejects otherwise).
# The CLI does NOT compute amount0/amount1 for you — wrong ratio reverts with
# "Price slippage check". See docs/TESTING.md "LP mint amount ratio".

# Generic contract call (TTY-only escape hatch — agent callers blocked)
uv run wallet contract call --help

# History
uv run wallet history                       # default account, native + contract calls
uv run wallet history --tokens              # ERC-20 transfers
uv run wallet history --address 0x...       # arbitrary lookup

# Address book / watch-only
uv run wallet book add alice 0xabc...
uv run wallet watch add 0xdef... --label vitalik
uv run wallet token add 0x779877A7B0D9E8603169DdbD7836e478b4624789  # adds LINK

# Stuck-tx recovery (when base-fee spikes and a broadcast is jammed in mempool)
uv run wallet tx pending                                          # list broadcasts with no receipt yet
uv run wallet tx cancel 42 --broadcast --request-id cxl-42-$(date +%s)
                                                                  # 0-value self-send at nonce 42, frees the slot
uv run wallet tx replace 42 --broadcast --request-id rpl-42-$(date +%s)
                                                                  # re-broadcasts original calldata at +25% gas
# Both default to dry-run; --speedup-pct overrides the 25% bump on top of the
# 110% EIP-1559 mempool replacement floor. `pending` reads idempotency.json
# locally — only txs originally broadcast through this wallet are recoverable.

# Chain inspection + multi-chain
uv run wallet chain list                    # builtin (sepolia) + user-added chains, ★ marks default
uv run wallet chain show ethereum           # full ChainConfig dump including protocol addresses
uv run wallet info                          # active chain (reads state.default_chain)
uv run wallet info --chain ethereum         # peek at another chain without switching default
uv run wallet balance --chain ethereum      # any command takes --chain to override per-call
```

## Multi-chain support

Builtin only ships **Sepolia** out of the box. To add Ethereum mainnet (or
Base / Arbitrum / any EVM chain), write a JSON entry to
`~/Library/Application Support/wallet/chains.json` in your terminal:

```jsonc
{
  "ethereum": {
    "name": "ethereum",
    "chain_id": 1,
    "rpc_url": "https://eth.drpc.org",                  // read path: balance, nonce, simulate, gas, receipts
    "broadcast_rpc_url": "https://rpc.flashbots.net",   // write path: eth_sendRawTransaction only
    "mev_exposure": true,                                // declare: this chain has a public mempool
    "explorer_api_url": "https://api.etherscan.io/v2/api",
    "explorer_tx_url": "https://etherscan.io/tx/{tx}",
    "native_symbol": "ETH",
    "builtin_tokens": {
      "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
      "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
      "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
      "DAI":  "0x6B175474E89094C44Da98b954EedeAC495271d0F"
    },
    "protocols": {
      "uniswap_v3": {
        "swap_router_v2": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "quoter_v2":      "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "factory":        "0x1F98431c8aD98523631AE4a59f267346ea31F984"
      },
      "aave_v3": {
        "pool":          "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
        "data_provider": "0x41393e5e337606dc3821075Af65AeE84D7688CBD",
        "oracle":        "0x54586bE62E3c3580375aE3723C145253060Ca0C2"
      }
    }
  }
}
```

`wallet chain list` lists every available chain; user-added entries are
marked `user-added`, and an entry whose name matches a builtin is marked
`user-override` (your fields win).

### Read / broadcast RPC split + MEV protection

Mainnet (and any EVM chain with a public mempool) routes
`eth_sendRawTransaction` through a **separate** endpoint than reads, so the
signed tx never enters the public mempool where sandwich bots can frontrun
it. Two related fields on each chain config:

- **`broadcast_rpc_url`** — URL used only for `eth_sendRawTransaction`.
  When `None`, broadcast falls back to `rpc_url`. Recommended:
  [`https://rpc.flashbots.net`](https://docs.flashbots.net/flashbots-protect/overview)
  (Flashbots Protect — neutral builder, free) or
  [`https://rpc.mevblocker.io`](https://mevblocker.io/) (MEV Blocker — refund
  model). These endpoints only accept `sendRawTransaction`; reads will fail,
  which is why we use a separate URL for them.
- **`mev_exposure`** — boolean capability declaration: "this chain exposes
  pending tx to a public mempool visible to MEV searchers." Defaults to
  `true` (fail-closed). Set `false` for sequencer-controlled chains
  (Arbitrum / Optimism / Base / most L2s) and testnets where there is no
  public mempool MEV threat.

When `mev_exposure: true`, the policy gate **refuses to broadcast** unless
`broadcast_rpc_url` is set AND distinct from `rpc_url`. This is the
fail-closed enforcement: an env-var typo, a forgotten export, or a
copy-paste mistake that collapses the split all get caught before signing.
Reasons: `mev-exposed-broadcast-rpc-url-unset` /
`mev-exposed-broadcast-equals-read`.

JSON broadcast envelopes include `data.broadcast_path: "private_relay" |
"public_rpc"` so agents and humans can tell at a glance whether a tx will
sit in a private relay queue (1–3 block inclusion delay typical, not
visible on Etherscan until included) or hit the public mempool.

Built-in `sepolia` ships with `mev_exposure: false` because testnets have
no MEV threat — the gate stays quiet, current behavior preserved. The
same holds for sequencer-controlled L2s (Base, Arbitrum, Optimism): with
`mev_exposure: false` the MEV path is fully inert — no gate check fires,
`eth_sendRawTransaction` reuses `rpc_url`, `broadcast_path` is tagged
`public_rpc` for the audit trail. The fail-closed default is there to
catch future mainnet-class integrations from operator drift, not to add
overhead on chains without a public mempool.

**Public RPC choice for the read path** — for "use without registering
with anyone": `https://eth.drpc.org`, `https://ethereum.publicnode.com`,
`https://eth.api.onfinality.io/public` are all reliable as of testing.
`cloudflare-eth.com` is **deprecated** as a Web3 Gateway — most methods
return `-32603` errors.

**Switching default**: any command accepts `--chain <name>`; to make a
chain the implicit default, edit `state.json` in your terminal:

```sh
P="$(uv run wallet info | awk '/state file/ {print $3,$4,$5}')"
jq '.default_chain = "ethereum"' "$P" > "$P.tmp" && mv "$P.tmp" "$P"
```

⚠️ Before switching default to a non-sepolia chain, **re-populate
`policy.json`'s `contract_allowlist`** with that chain's protocol
addresses. Sepolia's Uniswap router / Aave Pool addresses are different
from mainnet, and policy is global (not per-chain). A wrong allowlist
means every write is `policy_block`.

## End-to-end test (Sepolia)

Once you have an account funded from a faucet (e.g.
[https://sepoliafaucet.com](https://sepoliafaucet.com)):

```sh
uv run wallet balance                       # see the faucet ETH
uv run wallet account derive main --index 1 --as second
uv run wallet book add me_second $(uv run wallet account show second | grep address | awk '{print $2}')
uv run wallet send @me_second 0.001 --broadcast
uv run wallet history                       # see the tx land
```

## Security checks

Run these before trusting the wallet with anything beyond a faucet:

```sh
# (1) Process args — no secrets visible
ps aux | grep -v grep | grep wallet

# (2) State file — only addresses + vault key references, no plaintext
cat "$(uv run wallet info | awk '/state file/ {print $3,$4,$5}')"

# (3) Independent secret audit
agent-vault scan "$(uv run wallet info | awk '/state file/ {print $3,$4,$5}')"
# Should report 0 secrets in the file.

# (4) Confirm no temp files leak during a real signing (run in another shell)
while true; do ls /tmp/wallet-secret-* 2>/dev/null && echo LEAK; done &
uv run wallet send <to> 0.0001 --broadcast
# Watcher should print no LEAK lines — the FIFO transport keeps plaintext off
# disk. Only if the FIFO path fails will you see /tmp/wallet-secret-* appear
# briefly (the fallback path).
```

## JSON output mode (for agents)

Every command supports a global `--json` flag (or `WALLET_JSON=1` env var)
that emits a single-line JSON envelope on stdout instead of rich tables:

```sh
WALLET_JSON=1 wallet balance --token USDC | jq .
# {"ok":true,"command":"balance","chain":"sepolia","data":{...}}
```

**Standard envelope**:

```jsonc
// success
{"ok":true,"command":"<name>","chain":"<chain>","data":{...command-specific...}}

// error
{"ok":false,"command":"<name>","chain":"<chain>","error":"<code>","code":"<code>","reason":"<details>"}
```

**Error codes** (machine-enumerable): `validation_error`, `policy_block`,
`idempotency_mismatch`, `not_found`, `rpc_error`, `vault_error`,
`simulation_reverted`, `aborted`, `missing_request_id`,
`confirmation_required`, `tty_required`, `no_route`,
`insufficient_allowance`, `insufficient_funds`, `superseded`.

`insufficient_funds` is emitted when `wallet send` (or any `prepare_*`
path) can't estimate gas because the sender's balance < value + gas fee
— replaces a raw web3.py traceback with a typed JSON envelope.
`superseded` is the recovery-path equivalent of "already settled" —
`wallet tx cancel/replace` raises it when the original tx mines before
the replacement lands (race lost, no-op).

**`swap` envelope** (additional fields under `data`):

```jsonc
{
  "kind": "swap",
  "swap_provider": "uniswap_v3",
  "swap_route": "ETH > 500bps > USDC",
  "swap_token_in_address": "0xfFf9...",
  "swap_token_out_address": "0x1c7D...",
  "swap_token_out_symbol": "USDC",
  "swap_amount_out_expected_wei": "15565555",
  "swap_amount_out_expected": "15.565555",
  "swap_amount_out_min_wei": "15487727",
  "swap_amount_out_min": "15.487727",
  "swap_slippage_bps": 50
}
```

**`insufficient_allowance` error** carries the exact corrective command:

```jsonc
{
  "ok": false,
  "error": "insufficient_allowance",
  "data": {
    "token_symbol": "USDC",
    "spender": "0x3bFA...",
    "current_wei": "0",
    "required_wei": "100000000",
    "suggested_command": "wallet approve set USDC 0x3bFA... 100"
  }
}
```

**Amount fields** (always strings to avoid JS bigint loss): `amount_wei`,
`amount` (human-readable), `unit`, `decimals`. Addresses are EIP-55
checksummed.

Two companion flags:

- `--quiet` / `WALLET_QUIET=1` — rich mode only: suppresses status lines
  ("dry-run", "submitted:", etc.) but keeps tables and errors. No-op under
  `--json`.
- `--explain` / `WALLET_EXPLAIN=1` — emits decision-trace details (policy
  evaluation, idempotency lookup) to **stderr** in both modes. Stdout JSON
  stays clean for `jq`.

**stdout/stderr discipline** (so `wallet --json X | jq` always works):

| stream | rich mode | json mode |
|---|---|---|
| stdout | tables + status lines (unless `--quiet`) | one JSON envelope per command |
| stderr | warnings, `--explain` | warnings, `--explain` |

## Agent-callable usage

The wallet defends against agent abuse with a four-layer stack:

1. **Skill** (`docs/skills/wallet-agent.skill.md`) — soft guidance for the agent
2. **Policy** (`~/.wallet/policy.json`) — hard pre-broadcast limits
3. **Idempotency** (`~/.wallet/idempotency.json`) — same `--request-id` returns the cached result, no double-spend on retry
4. **Audit log** (`~/.wallet/audit.log`) — append-only JSON-lines record of every broadcast attempt; not exposed via the CLI

### Initialize the policy (do this once, in your terminal)

```sh
uv run wallet policy init        # writes default ~/.wallet/policy.json
$EDITOR "$(uv run wallet info | grep state | awk '{print $3,$4,$5}' | xargs dirname)/policy.json"
# add specific addresses to recipient_allowlist / contract_allowlist
uv run wallet policy lint        # warns about weak / empty fields
```

Default policy starts with caps but **empty allowlists** — agents are denied
until you explicitly add recipients/contracts you trust. This is intentional:
wide-open default + opt-out is unsafe.

`lp_pool_allowlist` has a non-obvious schema: each entry is an object
`{"token0": "0x...", "token1": "0x...", "fee": N}`, not a pool address. The
NFPM serves every V3 pool, so allowlisting NFPM alone in `contract_allowlist`
is necessary but not sufficient — an attacker who can shape the (token0,
token1, fee) inputs could otherwise route funds into a counterfeit pool. See
[`docs/TESTING.md`](docs/TESTING.md) for the Sepolia USDC/WETH 0.05% example.

### Install the agent skill (Claude Code)

```sh
mkdir -p ~/.claude/skills
ln -s "$(pwd)/docs/skills/wallet-agent.skill.md" ~/.claude/skills/wallet-agent.skill.md
```

The skill tells the agent the right call patterns (always dry-run first, fresh
request-id on every broadcast, never `--unlimited` / `--policy-bypass`).

### What the agent can / cannot do

| Operation | Agent | TTY |
|---|---|---|
| `balance` / `portfolio` / `history` / `account list` / `aave positions` / `lp positions` | ✓ | ✓ |
| `send` / `approve set` / `revoke` / `swap` / `aave *` / `lp *` (dry-run) | ✓ | ✓ |
| `send` / `approve` / `swap` / `aave *` (broadcast) | ✓ if within policy + has `--request-id` | ✓ |
| `lp mint / increase / remove / collect` (broadcast) | ✓ if pool in `lp_pool_allowlist` + has `--request-id` | ✓ |
| `tx pending` (read-only) | ✓ | ✓ |
| `tx cancel / replace` (broadcast) | ✓ if within policy + has `--request-id` | ✓ |
| `--policy-bypass` | rejected | warns, then proceeds |
| `--unlimited` approve | rejected by default policy | rejected by default policy |
| `account create` / `import` | refuse — runs in TTY only | ✓ (mnemonic shown to terminal) |
| `policy init` | refuse | ✓ |
| `contract call` (generic escape hatch) | refuse | ✓ |

## Tests

```sh
uv run pytest
```

Covers BIP-39 derivation against Hardhat's fixed mnemonic, state file
roundtrip + 0600 permissions, EIP-1559 tx field construction, fee floor
behaviour, simulation revert surfacing, fixed-point amount math, the FIFO
vault transport with tempfile fallback, **policy decision tree across all
branches** (sentinel / unlimited approve / cap exceeded / first-send),
**idempotency lookup / record / mismatch / TTL sweep**, audit log atomicity
across concurrent appends, and TTY/agent caller classification.

## Architecture

```
src/wallet/
  cli/                 typer command tree + presentation (rich)
    _caller.py         TTY vs agent classification (used by every gate)
    _common.py         confirm_and_broadcast — preview / policy / idempotency / sign / audit
    policy.py          `wallet policy show / init / lint`
  core/
    config.py          ChainConfig — chain_id, RPC, explorer, builtin tokens
    hd.py              BIP-39 / BIP-44 (eth-account)
    policy.py          Policy schema + evaluate(prepared, state, caller, *, bypass)
    rpc.py             Web3 factory, format_units / parse_units
    signer.py          mnemonic → private key → sign (in-memory only)
    tokens.py          ERC-20 ABI + helpers (balanceOf, allowance, fetch)
    tx.py              build/simulate/estimate pipeline; EIP-1559 fee policy
  storage/
    audit.py           ~/.wallet/audit.log JSON-lines append-only writer (no CLI read)
    idempotency.py     ~/.wallet/idempotency.json — request_id → cached result
    state.py           pydantic schema for ~/.wallet/state.json
    vault.py           agent-vault wrapper: FIFO transport + tempfile fallback
  services/
    explorer.py        Etherscan v2 client
```

Sending commands all go through `cli/_common.py:confirm_and_broadcast`:
preview → policy.evaluate → idempotency.lookup → confirm → sign → broadcast →
idempotency.record + audit.write. Every layer can short-circuit and writes
its own audit entry.

## What's next

See [`ROADMAP.md`](ROADMAP.md) for the current backlog. The biggest gating
item is **Hardware wallet (Ledger) integration** — until that lands, this is
a hot wallet appropriate for testnet experimentation and small daily-spend
mainnet use; see [`docs/why_hard_wallet.md`](docs/why_hard_wallet.md) for
the threat-model rationale.
