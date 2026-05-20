# Wallet — Architecture & Code Map (for new LLMs)

> Drop-in reference for an LLM joining this repo cold. Dense, file-anchored,
> deterministic. Reads top-to-bottom in ~10 minutes; section-anchored so you
> can jump straight to the layer you're editing.

---

## 0. Identity in one paragraph

`wallet` is an **AI-agent-native EVM CLI wallet** written in Python 3.13. It
exposes single on-chain primitives (send / approve / swap / Aave
supply-withdraw-borrow-repay / Uniswap V3 LP positions-mint-increase-remove-collect
/ stuck-tx pending-cancel-replace) as `wallet <cmd>` subcommands, plus a **TTY-only generic escape hatch**
(`wallet contract call <to> <fn-sig> [args…]`) for long-tail protocols that
don't have a typed surface. Every write call passes through a four-layer
safety stack (skill guidance → policy gate → idempotency → audit log) so an
adversarial caller (compromised agent, prompt-injection, LLM hallucination)
cannot bypass the user's spending limits, drain via unlimited approve, double
broadcast on retry, or route to a non-allowlisted contract. The wallet does
NOT carry strategy: no auto-rebalance, no auto-compound, no scheduled loops.
Strategy lives in an external layer that drives the CLI; the litmus rule for
what may enter this repo is encoded as the skill at
`.claude/skills/wallet-scope-litmus/SKILL.md` — apply it before adding any
new command.

**Typed surface vs generic escape hatch.** Typed `prepare_*` commands
(`swap`, `aave *`, `lp *`, etc.) carry semantic policy depth — allowance
pre-checks, HF guards, slippage, pool allowlists, deny-unlimited-approve.
The generic `contract call` path is the long-tail escape hatch and is
intentionally policy-shallow: humans-only (agent hard-block), gated by
`contract_allowlist` + `sentinel_blocklist` + native value caps only. Add
typed `prepare_*` when you need agent access or deep policy; use `contract
call` for one-off human-signed ops.

---

## 1. Stack & runtime

| Concern | Choice | Where |
|---|---|---|
| Python | `>=3.13` | `pyproject.toml` |
| Package manager | `uv` (not pip; not venv) | `uv.lock` |
| EVM lib | `web3.py >=7,<8` (hard upper bound — 8.x will break) | `pyproject.toml` |
| Signing | `eth-account >=0.13,<1` | `pyproject.toml` |
| CLI framework | `typer` (built on Click) | `src/wallet/cli/app.py` |
| TTY rendering | `rich` (tables, panels, prompts) | `src/wallet/cli/_output.py` |
| Config models | `pydantic v2` | `WalletState`, `Policy`, `ChainConfig`, `CachedResult` |
| HTTP | `httpx` (0x quote API only) | `src/wallet/protocols/routes/zerox.py` |
| Platform dirs | `platformdirs` (`~/.local/share/wallet` on macOS/Linux) | `src/wallet/core/config.py:data_root` |
| Secrets | `agent-vault` (npm CLI, OS-keychain-derived) | `src/wallet/storage/vault.py` |
| Test framework | `pytest 9.x` + `pytest-mock`, all MagicMock, no fork | `tests/` |

Entry point: `wallet = "wallet.cli.app:app"` (`pyproject.toml:scripts`).

Run anything via `uv run wallet <cmd>` — never bare `python`.

---

## 2. Repository topology

```
wallet/
├── src/wallet/
│   ├── cli/                    # one file per subcommand group; thin layer
│   │   ├── app.py             # typer root; registers subapps + single commands
│   │   ├── _common.py         # confirm_and_broadcast() — the broadcast pipeline
│   │   ├── _output.py         # OutputMode (JSON vs rich), emit / emit_error
│   │   ├── _caller.py         # caller_kind() — "agent" vs "tty"
│   │   ├── account.py         # account create / import / derive / list / show
│   │   ├── approve.py         # approve set / show / revoke
│   │   ├── balance.py portfolio.py history.py   # read-only queries
│   │   ├── send.py swap.py                       # transfer / DEX swap
│   │   ├── aave.py lp.py                         # protocol command groups
│   │   ├── contract.py                            # generic `wallet contract call` (TTY-only escape hatch)
│   │   ├── policy.py chain.py book.py watch.py   # config / address book
│   │   ├── token.py
│   │   └── tx.py                                  # stuck-tx recovery: pending / cancel / replace
│   ├── core/                   # protocol-agnostic primitives
│   │   ├── config.py          # ChainConfig, data_root, atomic_write_text, _tighten_data_root
│   │   ├── rpc.py             # make_web3 + chainId handshake, call_with_retry, redact_url, parse/format_units
│   │   ├── tokens.py          # ERC20_ABI, TokenInfo (with is_native flag), fetch_token_info, allowance, InsufficientAllowance, check_allowance_or_raise
│   │   ├── tx.py              # PreparedTx + _common_fields + finalize_tx (estimate-gas + simulate + strip-nonce + fee_wei) + broadcast; InsufficientFundsError when sender can't cover value + gas
│   │   ├── tx_replace.py      # PendingTx + prepare_cancel / prepare_replacement / list_pending — EIP-1559 mempool replacement at a pinned nonce
│   │   ├── slippage.py        # apply_slippage_floor — one source of truth for amount * (10000 - bps) / 10000
│   │   ├── signer.py          # sign_transaction wrapping eth-account; reads mnemonic via FIFO
│   │   ├── hd.py              # BIP-39/44 derivation; DerivedAccount with repr=False on private_key
│   │   ├── policy.py          # Policy schema + evaluate() — the pre-broadcast gate
│   │   └── uniswap_v3_math.py # Q96 TickMath / SqrtPriceMath / LiquidityAmounts (off-chain)
│   ├── protocols/             # one module per write-protocol; read+write helpers
│   │   ├── aave.py            # Aave V3 read + prepare_supply / withdraw / borrow / repay / faucet
│   │   ├── swap.py            # prepare_swap dispatcher (allowance pre-check via core.tokens.check_allowance_or_raise)
│   │   ├── uniswap_v3_lp.py   # NFPM read (get_positions) + prepare_mint / increase / decrease / collect
│   │   ├── contract_call.py   # prepare_contract_call — generic signature-parsed → calldata → PreparedTx
│   │   └── routes/            # swap route providers (RouteProvider ABC)
│   │       ├── base.py        # RouteProvider, Quote, NoRouteError
│   │       ├── uniswap_v3.py  # UniswapV3DirectRoute (QuoterV2 + SwapRouter02)
│   │       ├── zerox.py       # 0x aggregator (api.0x.org)
│   │       └── auto.py        # AutoFallbackRoute (0x → uniswap-v3)
│   ├── storage/               # on-disk durable state (under data_root())
│   │   ├── state.py           # WalletState (accounts, book, watch, tokens, default_*)
│   │   ├── policy.py          → core/policy.py owns the schema; storage handled there
│   │   ├── audit.py           # append-only JSONL writer; 0o600
│   │   ├── idempotency.py     # request_id → CachedResult; fingerprint()
│   │   └── vault.py           # agent-vault FIFO wrapper for mnemonic reads
│   └── services/
│       └── explorer.py        # Etherscan v2 API (history command)
├── tests/                      # ~5.8k LOC, pure MagicMock — no fork, no live RPC
├── docs/
│   ├── ARCHITECTURE.md         # ← this file
│   ├── features.md             # capability tour with Sepolia tx hashes
│   ├── security_review.md      # 2026-05-15 audit findings
│   ├── optimization_plan.md    # tier 0/1/2/3 backlog
│   ├── why_hard_wallet.md      # Ledger integration rationale
│   └── skills/wallet-agent.skill.md   # consumer-facing skill (how to USE the CLI)
├── .claude/skills/
│   └── wallet-scope-litmus/SKILL.md   # internal skill (what may be ADDED to the CLI)
├── pyproject.toml uv.lock ROADMAP.md README.md
```

Source: ~8.5k LOC. Tests: ~6.5k LOC. Test ratio ~0.75×.

---

## 3. Layered architecture

```
┌──────────────────────────────────────────────────────────┐
│  External agent / human / cron      (out of this repo)   │
│  ↓ argv + env (WALLET_JSON, WALLET_HOME, etc.)           │
├──────────────────────────────────────────────────────────┤
│  cli/<command>.py     argument parsing, token resolve,   │
│                       error envelope shaping             │
│  ↓ PreparedTx                                            │
├──────────────────────────────────────────────────────────┤
│  protocols/<proto>.py + routes/      prepare_*() builds  │
│                                      unsigned tx, runs   │
│                                      simulate, returns   │
│                                      PreparedTx          │
│  ↓ PreparedTx                                            │
├──────────────────────────────────────────────────────────┤
│  cli/_common.py:confirm_and_broadcast(...)               │
│    preview → policy.evaluate → idempotency.lookup        │
│    → user-confirm → nonce-refresh → sign → broadcast     │
│    → idempotency.record → audit.write                    │
├──────────────────────────────────────────────────────────┤
│  core/{tx,tokens,rpc,signer,hd,policy,uniswap_v3_math}   │
│  storage/{state,audit,idempotency,vault}                 │
├──────────────────────────────────────────────────────────┤
│  web3.py → HTTPProvider → RPC                            │
└──────────────────────────────────────────────────────────┘
```

Two things never get crossed:
- CLI commands NEVER call web3 directly to build txs; they always go through a `protocols/*.prepare_*` builder.
- Protocol builders NEVER call `sign_transaction` or `broadcast`; they return a `PreparedTx` and let `confirm_and_broadcast` decide.

---

## 4. The four safety layers (in order applied)

Each layer is independent — defeat one and the next still bites. Documented
in `README.md` as the "four safety layers" table; the code path is in
`cli/_common.py:confirm_and_broadcast`.

### 4.1 Skill guidance (advisory)
`docs/skills/wallet-agent.skill.md` — tells agents how to call the CLI (JSON
output, error codes, request-id discipline). Not enforced by code; if the
agent ignores it, the deeper layers still hold.

### 4.2 Policy gate (hard block, pre-broadcast)
`src/wallet/core/policy.py:evaluate()`. Loaded from `~/.wallet/policy.json`
(file-system path = `data_root() / "policy.json"`). Returns a `Decision`:
- `allowed=True severity=allow` → proceed
- `allowed=True severity=warn`  → TTY prompts confirmation; agent caller is auto-blocked
- `allowed=False`               → block (audited as `outcome=rejected`)

Rule families inside `evaluate()`:
1. sentinel blocklist (highest priority)
2. **`contract_call` (generic escape hatch): agent hard-block + target ∈ `contract_allowlist`** — no semantic gates exist on this path (no allowance check, no HF guard, no slippage, no pool match), so it's TTY-only by construction. `msg.value` flows through `amount_wei` / `unit=ETH` so `max_per_tx{ETH}` still binds below.
3. approve-specific: deny unlimited (`MAX_UINT256`) + spender ∈ `contract_allowlist`
4. send-specific: recipient ∈ `recipient_allowlist`
5. swap: router (`description.to`) ∈ `contract_allowlist`
6. aave supply/withdraw/borrow/repay/faucet: pool/faucet ∈ `contract_allowlist`
7. **LP NFPM ∈ `contract_allowlist`** (all 4 LP categories: `lp_mint`, `lp_increase`, `lp_decrease`, `lp_collect`)
8. **LP funds-in pool match: `(lp_token0_address, lp_token1_address, lp_fee)` ∈ `lp_pool_allowlist`** (categories `lp_mint`, `lp_increase` only; exit ops `lp_decrease` / `lp_collect` are NOT gated since they pull funds OUT of positions the user already holds). NFPM allowlist alone is insufficient because one NFPM serves every V3 pool; this is the LP-layer analogue of the swap symbol-confusion gate.
9. `min_health_factor` enforcement for borrow / withdraw
10. `max_per_tx` (per symbol)
11. `max_per_day` (per symbol, summed from `audit.log` entries dated UTC today)
12. first-send warn / block
13. **tx-recovery (`tx_cancel`, `tx_replace`)**: self-send cancel bypasses `recipient_allowlist` only when `description.is_self_send_for_cancel=True` AND `to == from` AND `amount_wei == 0` (any deviation is treated as an attacker-forged label and blocked); `tx_replace` delegates to the original op's category so the replacement faces the same allowlist / cap / HF gates the original did.

Fail-closed: missing `policy.json` → `no-policy-configured-run-wallet-policy-init`, no agent op can proceed.

Bypass: `--policy-bypass` is TTY-only; agents auto-rejected even with the flag.

### 4.3 Idempotency (retry safety)
`src/wallet/storage/idempotency.py`. Stripe-style:
- Same `request_id` + same `fingerprint` → return `CachedResult` with `replayed: true`
- Same `request_id` + different `fingerprint` → raise `IdempotencyMismatch` (programmer error: agent reused an ID across logically different ops)
- TTL 24h, swept on each `record()`.

`fingerprint(prepared, chain)` canonicalises everything that affects on-chain behavior. Hardened by `1.01` / commit `960087a` (security_review.md Vuln 2) to include `chain_id` (not just `chain.name`), lowercased addresses, all swap/aave op-differentiating fields. LP fields added with the LP primitives PR: `lp_action`, `lp_nft_token_id`, `lp_token0/1`, `lp_fee`, `lp_tick_lower/upper`, `lp_liquidity_wei`, `lp_amount{0,1}_{desired,min}_wei`, `lp_recipient`. `1.03` added `cc_calldata` for the generic contract-call path so two different fns to the same target with the same `msg.value` (e.g. `pause()` vs `unpause()`) don't collapse and silently replay. **Any new write protocol MUST add its op-differentiating fields here, otherwise two different ops collapse to the same hash and the second silently replays the first's tx_hash.**

Agent callers without `--request-id` are blocked (`missing_request_id`). TTY callers may omit.

### 4.4 Audit log (forensics)
`src/wallet/storage/audit.py`. JSONL at `data_root() / "audit.log"`, mode
`0o600`, append-only. One line per signing attempt with outcome ∈
`{broadcast, rejected, replayed_idempotent, user_aborted, user_aborted_after_warn, superseded}`.
`superseded` is the benign race outcome for `wallet tx cancel/replace` when
the original tx mined first (`nonce too low` from the RPC — see §5).

Schema fields written by `cli/_common.py:_audit_event`: `ts` (auto, UTC ISO),
`chain`, `from`, `to`, `spender`, `kind` (category), `amount_wei`, `unit`,
`token_address`, `nonce`, `gas`, `hash`, `caller`, `request_id`,
`policy_decision`, `outcome`. **Stuck-tx recovery rows additionally
emit `recovery` (`"cancel"` / `"replace"`), `old_tx_hash` (the original
broadcast that's being superseded), and `original_kind` (the kind of the
op being replaced, e.g. `"swap"`) so a cancel/replace is distinguishable
from a regular 0-value self-send in the log.**

Read-only by design — no CLI command exposes audit contents. The file is for
local forensics + the `max_per_day` policy computation.

---

## 5. The execution pipeline (one canonical flow)

Every write command (`send`, `approve set/revoke`, `swap`, `aave *`, `lp *`)
follows this exact sequence; the differences are confined to the
`prepare_*()` builder.

```
1.  cli/<cmd>.py: argv parse + token/chain/account resolve
        ↓
2.  protocols/<proto>.py:prepare_*(...) →
        a. resolve protocol addresses via core/config.py:get_protocol_address
        b. for write paths needing token approvals:
             check_allowance_or_raise(w3, token, sender, spender, required_wei)
             → no-op when token.is_native or required_wei == 0
             → otherwise raise InsufficientAllowance (CLI maps to error
               envelope "insufficient_allowance" with
               suggested_command="wallet approve set ...")
        c. build base tx fields via core/tx.py:_common_fields (chainId,
           from, type=2, EIP-1559 maxFeePerGas = base*2 + priority,
           NO nonce)
        d. encode calldata via web3 Contract.functions.<name>(...).build_transaction
           OR raw {"to","data","value"}
        e. fee_wei = finalize_tx(w3, tx) — runs estimate_gas (if not
           already set by build_transaction), pre-simulates via eth_call
           (ContractLogicError → RuntimeError("simulation reverted: ...")),
           strips nonce, and returns maxFeePerGas * gas. Single point of
           "tx is ready for signing" — never bypass.
        f. return PreparedTx(tx, fee_wei, description={kind, from, to, ...})
        ↓
3.  cli/_common.py:confirm_and_broadcast(w3, state, chain, sender, prepared, dry_run, yes, policy_bypass, request_id, preserve_nonce=False):
        # preserve_nonce=True is used only by `wallet tx cancel/replace` —
        # it keeps the stuck nonce on the prepared tx instead of refreshing,
        # because the whole point of replacement is to pin to that nonce.
        # On that path, an RPC `nonce too low` after broadcast is mapped to
        # outcome=superseded (the original landed first) — a benign race
        # outcome, not an rpc_error.
        if dry_run:
            emit envelope with data.phase="preview"; return
        render preview in rich mode
        decision = policy.evaluate(prepared, state, caller, bypass=policy_bypass)
        if not allowed: audit(rejected) + emit_error("policy_block") + exit 3
        if severity=warn:
            agent → audit + block; TTY → prompt unless --yes
        fp = idempotency.fingerprint(prepared, chain)
        if request_id:
            cached = idempotency.lookup(request_id, fp)  (raises IdempotencyMismatch on collision)
            if cached: emit envelope replayed:true + return  (no signing)
        elif caller == "agent":
            audit + emit_error("missing_request_id") + exit 3
        final confirm prompt unless --yes
        prepared.tx["nonce"] = w3.eth.get_transaction_count(from, "pending")   ← LATE: avoids stale
        raw = sign_transaction(sender_account, prepared.tx)                    ← agent-vault FIFO
        tx_hash = broadcast(w3, raw)
        if request_id: idempotency.record(request_id, fp, tx_hash, nonce, "broadcast")
        audit(broadcast)
        emit success envelope with tx_hash + explorer_url
```

Notes for new contributors:
- **Nonce is deliberately late-bound.** `finalize_tx` strips it from the
  prepared tx so the broadcast-time re-read of `pending` reflects any other
  tx that landed between prepare and broadcast. Don't move nonce-setting
  back into `prepare_*`.
- **`finalize_tx` is mandatory.** It's the only place revert reasons get
  surfaced as `simulation_reverted` envelopes (via the wrapped `eth_call`).
  Bypassing it lets an agent burn gas on a guaranteed-revert tx and miss
  `aave error code 51` style hints, AND leaves a stale nonce in the tx.
- **`description.kind` is the routing key.** `cli/_common.py:_CLASSIFY_TABLE`
  + `core/policy.py:_category` both look it up. Adding a new write op
  means adding a row in `_CLASSIFY_TABLE` AND a branch in `policy._category`
  AND (if applicable) a contract-allowlist check in `policy.evaluate`.
  Forgetting any one of these triggers either "unknown" classification or
  a missing security check. `_CLASSIFY_TABLE` is **order-sensitive**: the
  `contains "contract call"` row is placed before the `contains "transfer"`
  / `contains "approve"` rows so a function literally named `transfer(...)`
  or `approve(...)` can't slip into the typed `send` / `approve` category
  and dodge the `contract_call` agent-block.

---

## 6. Module reference (where to find what)

### core/

| File | Exports | Notes |
|---|---|---|
| `config.py` | `ChainConfig`, `get_chain`, `list_chains`, `get_protocol_address`, `data_root`, `chains_config_path`, `atomic_write_text`, `_tighten_data_root` | Sepolia preset is in-source. Mainnet config comes from `~/.wallet/chains.json` if present. `data_root()` is the single source of truth for state-file paths. |
| `rpc.py` | `make_web3`, `RpcConnectError`, `call_with_retry`, `redact_url`, `scrub_message_of_url`, `format_units`, `parse_units` | `make_web3` does a chainId handshake and converts every failure into `RpcConnectError`. URL redaction guards API keys in JSON envelopes and logs. |
| `tokens.py` | `ERC20_ABI`, `TokenInfo`, `MAX_UINT256`, `erc20`, `fetch_token_info`, `clear_token_info_cache`, `balance_of`, `allowance`, `resolve_token`, `InsufficientAllowance`, `check_allowance_or_raise` | `TokenInfo.is_native` is THE security gate for native ETH routing — set ONLY by `cli/swap.py:_resolve_token_or_native` when user literally types the chain's native symbol. `InsufficientAllowance` lives here (not in `protocols/swap.py`) because swap / aave / lp all raise it; `check_allowance_or_raise` is the one helper to call — it short-circuits on `is_native` and `required_wei == 0`. |
| `tx.py` | `PreparedTx`, `prepare_native_transfer`, `prepare_erc20_transfer`, `prepare_erc20_approve`, `broadcast`, `finalize_tx`, `_common_fields`, `_simulate`, `_strip_nonce` | EIP-1559 fees floored at 1 gwei priority. Nonce intentionally never set inside this module. `finalize_tx` is the **public** wrapper for `estimate_gas (if missing) → _simulate → _strip_nonce → return maxFeePerGas * gas`; the underscore-prefixed helpers stay internal and shouldn't be called directly from new code. |
| `slippage.py` | `apply_slippage_floor` | Single source of truth for `amount * (10_000 - slippage_bps) // 10_000` with `[0, 10_000]` validation. Replaces the identical-but-separate `_apply_slippage` / `_apply_slippage_floor` that used to live in `routes/uniswap_v3.py` and `uniswap_v3_lp.py`. |
| `signer.py` | `sign_transaction` | Reads mnemonic via `agent-vault` Unix FIFO into the signing process. Never lands on disk. |
| `hd.py` | `derive_account`, `DerivedAccount` | BIP-39/44. `DerivedAccount.private_key` has `repr=False` so `print()` / debug traces / `rich.log` never leak it. |
| `policy.py` | `Policy`, `Decision`, `LpPoolAllowEntry`, `evaluate`, `default_policy`, `load_policy`, `save_policy`, `policy_path` | The actual gate. Categories enumerated in `_category()` (includes `contract_call` — agent hard-block + TTY-only). `LpPoolAllowEntry` is the `(token0, token1, fee)` schema for `Policy.lp_pool_allowlist`, with pydantic validators enforcing the V3 invariant `token0 < token1` and known fee tiers `{100, 500, 3000, 10000}`. **Every new `description.kind` you add must be classified here, otherwise security checks silently skip your new op.** |
| `uniswap_v3_math.py` | `MIN_TICK`, `MAX_TICK`, `MIN_SQRT_RATIO`, `MAX_SQRT_RATIO`, `Q96`, `MAX_UINT128`, `FEE_TIER_TICK_SPACING`, `tick_spacing_for_fee`, `align_to_tick_spacing`, `get_sqrt_ratio_at_tick`, `get_tick_at_sqrt_ratio`, `get_amount0_for_liquidity`, `get_amount1_for_liquidity`, `get_amounts_for_liquidity` | Pure Python, no third-party dep. Anchor ticks (MIN/0/MAX) match on-chain constants exactly; interior values within 1 ulp of on-chain TickMath. **Off-chain estimation only — do NOT use for on-chain equality checks; read `pool.slot0()` instead.** |

### protocols/

| File | Pattern | Exports |
|---|---|---|
| `aave.py` | Read functions (`get_all_reserves`, `get_account_summary`, `get_user_positions`, `get_all_rates`, `get_asset_price`) + write builders (`prepare_supply`, `prepare_withdraw`, `prepare_borrow`, `prepare_repay`, `prepare_faucet_mint`) + HF projection helpers (`estimate_hf_after_borrow`, `estimate_hf_after_withdraw`). `WITHDRAW_MAX_AMOUNT` / `REPAY_MAX_AMOUNT` sentinels. Aave error codes mapped to human strings in `cli/aave.py:_AAVE_ERROR_CODES`. | |
| `swap.py` | Orchestrator: `prepare_swap(w3, chain, sender, token_in, token_out, amount_in_wei, slippage_bps, provider)`. Pre-flight allowance gate via `core.tokens.check_allowance_or_raise` (skipped for `is_native` input). | |
| `uniswap_v3_lp.py` | Reads: `get_positions`, `fetch_position`. Writes: `prepare_collect`, `prepare_decrease_liquidity`, `prepare_mint`, `prepare_increase_liquidity`. Native ETH path wraps the action call in NFPM `multicall([action, refundETH])` so unused ETH bounces back atomically. Token order auto-sorted to (token0 < token1). Tick misalignment raises (no silent rounding). Slippage minimums via `core.slippage.apply_slippage_floor`; allowance pre-checks via `core.tokens.check_allowance_or_raise`. | |
| `contract_call.py` | Generic escape hatch: `parse_function_signature`, `coerce_arg`, `build_calldata`, `prepare_contract_call(w3, chain, sender, to, fn_sig, args, value_wei)`. Supports simple types + 1D arrays; tuples explicitly rejected with a "use typed `prepare_*`" hint. No token resolution, no allowance check, no slippage — intentionally bare since this path has no semantic policy depth. `kind = "contract call <fn>"` so `_CLASSIFY_TABLE` / `policy._category` route it to the `contract_call` category. | |
| `routes/base.py` | `RouteProvider` ABC, `Quote` dataclass, `NoRouteError` | |
| `routes/uniswap_v3.py` | `UniswapV3DirectRoute` — `QuoterV2.quoteExactInputSingle` across fee tiers `[100, 500, 3000, 10000]`, encodes `SwapRouter02.exactInputSingle`. **When `token_out.is_native`, the encoded calldata is a `multicall([exactInputSingle(recipient=ADDRESS_THIS), unwrapWETH9(amountMin, user)])` so the user receives native ETH instead of WETH** — without this, swap-to-ETH leaves WETH in the user's account and an agent that follows up with `wallet send X ETH` underflows. | |
| `routes/zerox.py` | `ZeroExRoute` — 0x aggregator API; honours `WALLET_ZEROX_API_KEY` env | |
| `routes/auto.py` | `AutoFallbackRoute` — tries 0x first (best mainnet liquidity), falls back to direct UniV3 (Sepolia / no API key) | |

### cli/

Every subcommand file follows the same skeleton:
```python
def <cmd>(<typer args+options>):
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="<cmd>")
    sender = resolve_account(state, account)
    # ... resolve tokens / parse amounts via core.rpc.parse_units ...
    prepared = prepare_<op>(w3, cfg, sender.address, ...)
    confirm_and_broadcast(w3, state, cfg, sender, prepared,
                          dry_run=not broadcast, yes=yes,
                          policy_bypass=policy_bypass, request_id=request_id)
```

Read-only commands skip `confirm_and_broadcast` and call `emit(envelope, render)` directly.

**Critical idiom — monkey-patch target for tests:**
A CLI module imports `make_web3_or_exit` from `wallet.cli._common`, which
binds the name into the CLI module's own namespace. Tests must patch the
**importing** module (`wallet.cli.lp.make_web3_or_exit`, not
`wallet.cli._common.make_web3_or_exit`) — see `tests/test_send.py` for the
explanation of why patching the source module is a vacuous test.

### storage/

| File | Purpose |
|---|---|
| `state.py` | `WalletState` pydantic model (`accounts`, `book`, `watch`, `tokens`, `default_account`, `default_chain`); `state.json` |
| `audit.py` | `audit.write(dict)`; auto-stamps `ts`; mode 0o600 |
| `idempotency.py` | `fingerprint(prepared, chain)` (sha256 over canonical JSON of behavior-affecting fields); `lookup(request_id, fp)`; `record(...)`; 24h TTL |
| `vault.py` | `read_mnemonic_via_fifo(vault_key)`; spawns `agent-vault get` and reads its stdout through a kernel pipe |

---

## 7. Command catalog

`wallet --help` is the source of truth; this table is a quick-lookup index.
Every write command supports `--account/-a`, `--chain`, `--broadcast/--dry-run`
(default dry-run), `--yes/-y`, `--policy-bypass` (TTY only), `--request-id`
(required for agent broadcasts).

| Group | Commands | Type |
|---|---|---|
| (root) | `version`, `info` | read |
| `account` | `create` (TTY), `import` (TTY), `derive`, `list`, `show`, `use` | mixed |
| `balance` | `balance [--token X] [--all]` | read |
| `portfolio` | `portfolio [--all]` | read |
| `send` | `send <to> <amount> [--token X]` | write |
| `approve` | `approve set <token> <spender> <amount> [--unlimited]`, `approve show`, `approve revoke` | write |
| `swap` | `swap <in> <out> <amount> [--via auto/0x/uniswap-v3] [--slippage-bps 50]` | write |
| `aave` | `positions`, `rates`, `supply`, `withdraw [--max]`, `borrow`, `repay [--max]`, `faucet` | mixed |
| `lp` | `positions`, `collect`, `remove --percent N`, `mint --fee --tick-lower --tick-upper --amount-a --amount-b`, `increase --amount-a --amount-b` | mixed |
| `contract` | `call <to> "<fn-sig>" [args…] [--value ETH]` — generic escape hatch; **TTY-only (agent hard-block)**; target must be in `contract_allowlist` | write |
| `policy` | `init`, `show`, `lint` | local config |
| `chain` | `list`, `show`, `add`, `remove` | local config |
| `book`, `watch`, `token` | name / address-book management | local config |
| `tx` | `pending`, `cancel <nonce>`, `replace <nonce>` — stuck-tx recovery via EIP-1559 mempool replacement. `cancel` is a 0-value self-send at the pinned nonce with bumped fees; `replace` re-broadcasts the original calldata. Both flow through `confirm_and_broadcast(preserve_nonce=True)`. | mixed |
| `history` | `history [--n N]` (Etherscan v2) | read |

Output discipline:
- `--json` (or `WALLET_JSON=1`) emits `{"ok": bool, "command": ..., "chain": ..., "data" or "error"+"reason"}` on stdout, one line.
- `--explain` / `WALLET_EXPLAIN=1` writes decision traces to **stderr**, never pollutes stdout JSON.
- `--debug` / `WALLET_DEBUG=1` turns on web3 + urllib3 request logging, with a credential-scrub filter inline (`cli/app.py:_global._CredentialScrubFilter`) so API keys in RPC URLs don't reach stderr.

Error codes (enumerable; branch on `code`, not `reason`): `validation_error`,
`policy_block`, `idempotency_mismatch`, `not_found`, `rpc_error`,
`vault_error`, `simulation_reverted`, `aborted`, `missing_request_id`,
`confirmation_required`, `tty_required`, `no_route`,
`insufficient_allowance`, `insufficient_funds`, `superseded`.
`insufficient_funds` is emitted when `finalize_tx` can't estimate gas
because the sender's balance < `value + gas * maxFeePerGas` (replaces a
raw web3.py traceback). `superseded` is the recovery-path equivalent of
"already settled" — see §4.4 and the `tx` row in the command catalog.

---

## 8. On-disk state

All files live under `data_root()`:
- Env override: `$WALLET_HOME` (preferred) or `$WALLET_DATA_DIR` (legacy)
- Default: `platformdirs.user_data_dir("wallet")` → on macOS `~/Library/Application Support/wallet/`, on Linux `~/.local/share/wallet/`

Tightened on every `data_root()` call to 0o700 directory + 0o600 files (see `config.py:_tighten_data_root`).

| File | Owner | Schema | Purpose |
|---|---|---|---|
| `state.json` | `storage/state.py` | `WalletState` | accounts, address book, watch list, registered tokens, default account/chain |
| `policy.json` | `core/policy.py` | `Policy` | spending limits + allowlists |
| `idempotency.json` | `storage/idempotency.py` | dict[request_id, CachedResult] | retry-safety cache, 24h TTL |
| `audit.log` | `storage/audit.py` | JSONL | append-only signing-attempt record |
| `chains.json` (optional) | `core/config.py:get_chain` | dict[name, ChainConfig kwargs] | override / extend built-in chain presets |

Secrets are NOT in any of the above. Mnemonics live in `agent-vault` (OS keychain-derived); private keys are derived in-process and dropped.

---

## 9. Security invariants (DO NOT BREAK)

These were paid for by past incidents (commit `960087a`, `docs/security_review.md`).
Any future PR that touches one of them must explicitly justify the change in
its description.

1. **Never route on ERC-20 `symbol()`.** A malicious ERC-20 can return any
   string including `"ETH"`. Native-vs-token routing is gated on
   `TokenInfo.is_native`, set only by `cli/swap.py:_resolve_token_or_native`
   when the user literally types the chain's `native_symbol`. (Vuln 1.)

2. **Idempotency fingerprint must include every field that changes on-chain
   effect, plus `chain_id`.** Adding a new write op without extending the
   fingerprint causes silent replay of the wrong cached tx_hash. (Vuln 2.)
   All addresses lowercased; nonce intentionally NOT included (retry with
   refreshed nonce is the same logical op).

3. **Every write builder must call `core.tx.finalize_tx(w3, tx)` before
   returning the `PreparedTx`.** `finalize_tx` runs `estimate_gas` (if
   missing) → `_simulate` → `_strip_nonce` → returns `fee_wei` as one
   atomic step. Bypassing it skips revert detection AND leaks a stale
   nonce into broadcast. The underscore-prefixed helpers in `core/tx.py`
   are internal — new code should not call them directly.

4. **Every new `description.kind` must be classified in BOTH
   `cli/_common.py:_CLASSIFY_TABLE` AND `core/policy.py:_category`.** Missing
   either side: audit/daily-cap blind, OR policy gate degrades to "unknown"
   and silently allows. `_CLASSIFY_TABLE` is order-sensitive — the
   `contains "contract call"` row must stay above the `contains "transfer"`
   / `contains "approve"` rows.

5. **Every protocol with a target contract must have an allowlist branch in
   `policy.evaluate`.** (Swap router, Aave pool, Aave faucet, NFPM, generic
   `contract call` target all have one. Adding a new protocol = adding a
   new branch.)

5b. **LP funds-in ops are pool-allowlisted, not just NFPM-allowlisted.**
    `lp_mint` and `lp_increase` must satisfy `(lp_token0_address,
    lp_token1_address, lp_fee) ∈ Policy.lp_pool_allowlist`. NFPM allowlist
    is necessary but insufficient — one NFPM address serves every V3 pool,
    so a compromised agent that controls token inputs can route funds into
    a counterfeit pool through the legitimate manager. Exit ops
    (`lp_decrease` / `lp_collect`) intentionally skip this gate so users
    can exit a pool that was later removed from the allowlist.

5c. **`contract_call` is humans-only by construction.** Agent callers
    (`caller_kind() == "agent"`) are hard-blocked in `policy.evaluate`
    regardless of any other config. This is the typed-policy escape hatch
    — by design no per-op semantic gate exists on this path (no allowance
    check, no HF guard, no pool match), so allowing agents would be
    handing them a backdoor around every typed protection. If you want
    agent access to a specific contract, ship a typed `prepare_*` for it.

6. **Agent callers (`caller_kind() == "agent"`) must:**
   - have `--request-id` (else `missing_request_id`, exit 3)
   - fail-closed when `policy.json` is missing
   - be auto-blocked on `severity=warn` (TTY only can prompt past warnings)
   - never see TTY-only `--policy-bypass` succeed

7. **`audit.log` is forensic, not retrievable via the CLI.** No `wallet
   audit show` command. The agent must not be able to learn what the user
   has signed historically.

8. **Mnemonics never on disk.** Reads go through Unix FIFO. The dataclass
   storing derived keys has `repr=False` on `private_key` to prevent
   exception traces / debug logs / `rich.log` accidentally exfiltrating.

9. **`audit.log`, `state.json`, `policy.json`, `idempotency.json`, `chains.json`
   are mode 0o600. The data root is 0o700.** Enforced on every `data_root()`
   call so old installs migrate forward.

10. **No strategy in wallet.** The litmus rule in
    `.claude/skills/wallet-scope-litmus/SKILL.md` is the durable check for
    any new command proposal. Apply it BEFORE writing code; reject anything
    that requires strategic judgment to decide *when* / *how-wide* /
    *which-pool* / *whether*.

---

## 10. Testing strategy

- **Framework:** `pytest 9`, `pytest-mock` 3.x, no other plugins.
- **Pattern:** MagicMock everywhere. No fork, no Anvil, no VCR cassettes, no
  live RPC. Each test builds its own w3 mock inline (no `conftest.py` with
  shared fixtures — verified absent).
- **Style:** Look at `tests/test_uniswap_v3_lp.py` for the canonical w3-mock
  builder pattern (`_W3Spec` + `_make_w3_mock`); `tests/test_send.py` for
  the NameError-class guard pattern.
- **Run:** `uv run pytest tests/` (352 tests, sub-4-second wall time).
- **Coverage of write paths:** at minimum 3 cases per `prepare_*` —
  happy path, allowance/precondition failure, simulate revert.
- **CLI tests:** use `typer.testing.CliRunner.invoke(app, [...])` with
  `WALLET_HOME=tmp_path` to isolate state.

When adding a test for a new write command, you MUST also add a
NameError-class guard (a smoke test that actually enters the function body
and asserts the stub fires). `tests/test_send.py` documents WHY: the
historical bug was a missing `from … import make_web3_or_exit` line that
`--help` doesn't trigger.

---

## 11. Adding a new protocol — recipe

Suppose you've passed the scope litmus (it's a primitive, not strategy) and
want to add `wallet morpho supply <token> <amount>`.

1. `src/wallet/protocols/morpho.py` — inline ABI (the convention is
   module-level `list[dict]`, see `aave.py:AAVE_POOL_ABI` or
   `uniswap_v3_lp.py:NFPM_ABI`). Implement:
   - read helpers (`get_market_data`, `get_user_position`)
   - `prepare_supply(w3, chain, sender, market, amount_wei) → PreparedTx`
     - Resolve protocol address via `get_protocol_address(chain, "morpho", "blue")`
     - `check_allowance_or_raise(w3, token, sender, spender, amount_wei)` —
       no-op when `token.is_native` or `amount_wei == 0`
     - `_common_fields` → `contract.functions.<fn>(...).build_transaction(base)`
       (or raw `{to, data, value}` dict)
     - `fee_wei = finalize_tx(w3, tx)` — single call covers estimate_gas +
       simulate + strip_nonce + fee calc
     - `description.kind = "morpho supply"` (or similar)
     - If your op takes slippage, use `core.slippage.apply_slippage_floor`
2. `src/wallet/core/config.py` — extend Sepolia preset's `protocols` dict
   with `"morpho": {"blue": "0x..."}`. (Mainnet via `~/.wallet/chains.json`.)
3. `src/wallet/cli/morpho.py` — typer subapp. Mirror `cli/aave.py:supply`.
4. `src/wallet/cli/app.py` — `app.add_typer(morpho_cli.app, name="morpho")`.
5. `src/wallet/cli/_common.py:_CLASSIFY_TABLE` — add row for the new
   `kind` → `(category, machine_kind)`. Mind the row order (see
   security invariant #4).
6. `src/wallet/core/policy.py:_category` — add branch returning the new
   category.
7. `src/wallet/core/policy.py:evaluate` — add a contract_allowlist check
   branch for the new category (mirror the `aave_supply` / `lp_mint`
   branches). If your op moves user funds INTO a pool / market / vault
   that's distinct from the contract entry point, add a market-allowlist
   field too (see `lp_pool_allowlist` for the canonical example —
   invariant #5b).
8. `src/wallet/storage/idempotency.py:fingerprint` — add any new
   op-differentiating description fields to the canonical JSON.
9. `tests/test_morpho.py` — protocol unit tests (MagicMock w3).
10. `tests/test_cli_morpho.py` — CLI integration tests; include the
    NameError-class guard.
11. Run `uv run pytest tests/` — full suite must stay green; new tests
    expected.
12. PR description must cite the scope litmus rule as evidence the new
    command is a primitive, not strategy.

If you find yourself wanting to skip step 5, 6, 7, or 8 — STOP. Those are
load-bearing for the security model.

**If you can't justify a typed `prepare_*` for the op** (one-off, long-tail,
human-only), the answer is `wallet contract call` — don't ship a
ceremonial typed surface just to expose one function. The escape hatch
exists exactly for that case.

---

## 12. Pointers to other docs

- `README.md` — user-facing intro, install, daily commands
- `ROADMAP.md` — backlog with ship dates; Ledger integration is the gating item before mainnet promotion
- `docs/features.md` — capability tour with Sepolia tx hashes (regenerate after major releases)
- `docs/security_review.md` — 2026-05-15 findings (Vuln 1 + Vuln 2 fixed); read before touching swap / idempotency
- `docs/optimization_plan.md` — tier 0/1/2/3 backlog with file:line anchors
- `docs/why_hard_wallet.md` — Ledger integration rationale
- `docs/skills/wallet-agent.skill.md` — **consumer-facing** skill: how an agent should USE the CLI
- `.claude/skills/wallet-scope-litmus/SKILL.md` — **internal** skill: what may be ADDED to the CLI

If something here disagrees with the actual code, the code wins — update
this doc in the same PR.
