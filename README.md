# wallet — DeFi CLI wallet (Phase 1)

A Python CLI for Ethereum-compatible chains. Phase 1 covers the **non-DeFi
basics**: accounts, balances, transfers, ERC-20 approvals, transaction history,
address book, and watch-only addresses.

Mnemonics live in `agent-vault`; the wallet process retrieves them through a
**Unix named pipe (FIFO)** — `agent-vault` writes the substituted plaintext
into the pipe inode, this process reads it from the kernel buffer, and the
inode is unlinked. The plaintext **never lands on disk**, only in two process
memories briefly. A legacy 0600 temp-file path (`_reveal_via_tempfile`) is
kept as a fallback for platforms where the FIFO transport fails.

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

# History
uv run wallet history                       # default account, native + contract calls
uv run wallet history --tokens              # ERC-20 transfers
uv run wallet history --address 0x...       # arbitrary lookup

# Address book / watch-only
uv run wallet book add alice 0xabc...
uv run wallet watch add 0xdef... --label vitalik
uv run wallet token add 0x779877A7B0D9E8603169DdbD7836e478b4624789  # adds LINK
```

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
`confirmation_required`, `tty_required`, `no_route`, `insufficient_allowance`.

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
| `balance` / `history` / `account list` | ✓ | ✓ |
| `send` / `approve set` / `revoke` (dry-run) | ✓ | ✓ |
| `send` / `approve` (broadcast) | ✓ if within policy + has `--request-id` | ✓ |
| `--policy-bypass` | rejected | warns, then proceeds |
| `--unlimited` approve | rejected by default policy | rejected by default policy |
| `account create` / `import` | refuse — runs in TTY only | ✓ (mnemonic shown to terminal) |
| `policy init` | refuse | ✓ |

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

## Phase 2 (not in this repo yet)

DEX swap (0x / 1inch aggregator), Aave V3 supply / borrow / repay, Lido
staking, and a `protocols/` directory that drops in alongside `core/`. The
existing tx pipeline is the entry point — protocol modules just produce
unsigned txs and hand them to `core/tx.py`.
