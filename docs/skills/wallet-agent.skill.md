---
name: wallet-agent
description: |
  Use the local DeFi wallet CLI (`wallet`) for ERC-20 / ETH operations on
  EVM-compatible chains. Read operations (balance, history, account list) are
  always safe; signing operations require explicit user authorization in the
  current turn AND must conform to the local policy at ~/.wallet/policy.json.
---

# Wallet usage rules for agents

The `wallet` CLI defends against agent abuse with a four-layer stack:
**this skill (guidance)** → **policy (hard limits)** → **idempotency (no
double-spend on retry)** → **audit log (forensics)**. Follow these rules so
the policy gate doesn't reject your operations.

## Always-safe (no signing, no money movement)

These can be invoked at will:

```
wallet balance [--account <name>] [--token <sym>] [--all]
wallet account list
wallet account show <name>
wallet history -n <N>
wallet token list
wallet book list
wallet watch list
wallet policy show           # see what limits apply
wallet info
```

## Sending ETH or ERC-20 (signing — needs user OK + policy compliance)

Workflow EVERY time:

1. **Confirm intent with the user in plain language** before touching anything.
2. **Dry-run first** — no `--broadcast`. Show the preview to the user.
3. **Wait for user's explicit "yes / go ahead / broadcast"** in the same turn.
4. **Generate a fresh request-id** (idempotency key — required for all
   broadcasts you initiate):
   ```
   python -c "import uuid; print(uuid.uuid4())"
   ```
5. **Broadcast with both `--broadcast` and `--request-id`**:
   ```
   wallet send <to> <amount> --broadcast --request-id <uuid>
   wallet approve set <token> <spender> <amount> --broadcast --request-id <uuid>
   wallet approve revoke <token> <spender> --broadcast --request-id <uuid>
   ```
6. **If the call fails with a transient network error**, RETRY using the SAME
   request-id (not a new one). The wallet will replay the result, not double-spend.
7. **If the call succeeds**, report the tx hash + explorer URL to the user.

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
