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
`tty_required`. Branch on these, not on `reason` text.

For debugging only, add `--explain` or `WALLET_EXPLAIN=1`. Decision details
go to **stderr** so stdout JSON stays clean.

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
wallet balance --token USDC | jq -r '.data.balances[0].amount'
wallet account list          | jq -r '.data.accounts[].name'
wallet account show <name>   | jq -r '.data.signed'        # true / false
wallet history -n 20         | jq '.data.transactions[] | {dir:.direction, hash}'
wallet token list            | jq -c '.data.tokens'
wallet book list             | jq '.data.entries'
wallet watch list            | jq '.data.entries'
wallet policy show           | jq '.data.policy'
wallet info                  | jq .
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

If you get `error: insufficient_allowance`, the envelope's `data` includes
a `suggested_command` field — just run that, then retry the swap with the
same logical params (use a NEW request-id for the approve, then ANOTHER
new request-id for the swap).

The swap **router** (e.g. `0x3bFA...` for Uniswap V3 on Sepolia) must be in
`policy.contract_allowlist`. If you get
`error: policy_block, code: swap-router-not-in-contract-allowlist`, the user
needs to add it in their terminal — you cannot modify the policy file.

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
