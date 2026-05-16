# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses a flat `1.XX` versioning convention — each shipped PR
that changes runtime behavior gets the next tag. Doc-only PRs (e.g.
`ARCHITECTURE.md` updates) do not bump the version; they live in git history.

Pre-`1.01` development (initial CLI, account management, send / approve / swap
/ Aave / Uniswap V3 LP primitives, audit / policy / idempotency stack) is
captured in `git log` — `1.01` is the first formal release tag.

---

## [Unreleased]

Nothing pending. Next planned tracks are tracked outside the CHANGELOG:
- **Ledger / hardware-wallet integration** — the documented mainnet > $1.5k
  gating item; see `docs/why_hard_wallet.md`.
- **Strategy daemon** — separate repo, consumes the now-stable primitive
  surface; out of scope per `.claude/skills/wallet-scope-litmus/SKILL.md`.

---

## [1.05] — 2026-05-16

Closes the last open item from `docs/security_review.md` (F7).

### Security
- **0x quote spender + `transaction.to` pinned to chain-known AllowanceHolder.**
  In 0x v2 allowance-holder mode every legitimate quote routes through the
  same CREATE2-deployed AllowanceHolder per chain. A compromised
  `api.0x.org` (or a TLS-stripped MITM that survives our handshake) could
  otherwise return `spender` = a router the user already approved for
  another protocol (stale `UniswapV3Router` approval is the canonical
  example), or set `transaction.to` to such a router with malicious
  calldata. Pinning both fields rejects either variant before signing.
  (PR #9.)

### Added
- `Policy.protocols.zerox.allowance_holder` chain config field. Sepolia
  preset ships with the deterministic v2 address
  (`0x0000000000001fF3684f28c67538d4D072C22734`); the same value is valid
  on every chain 0x supports.

### Changed
- `routes/zerox.py` fails closed when a chain has no AllowanceHolder
  configured — previously any spender was accepted silently. Migration:
  add the protocol entry to `~/.wallet/chains.json` (or the built-in
  preset) for each chain you route 0x through.

---

## [1.04] — 2026-05-16

Pure refactor pass — `352 tests pass unchanged`. Net −21 LOC while collapsing
three patterns that had grown duplicate copies. No behavior change. (PR #7.)

### Added
- `core/slippage.py` — single `apply_slippage_floor(amount, bps)` helper.
- `core/tokens.py:check_allowance_or_raise(w3, token, owner, spender, required_wei)` —
  one source of truth for the pre-broadcast allowance gate. Short-circuits
  on `token.is_native` and `required_wei == 0`.
- `core/tokens.py:InsufficientAllowance` — moved here from
  `protocols/swap.py`; the exception had outgrown the swap module since
  Aave and LP both raise it.
- `core/tx.py:finalize_tx(w3, tx) -> fee_wei` — wraps the four-line
  `estimate_gas (if missing) → simulate → strip_nonce → fee_calc` sequence
  every `prepare_*` used to repeat verbatim.

### Changed
- `routes/uniswap_v3.py` and `protocols/uniswap_v3_lp.py` now import
  `apply_slippage_floor` from `core/slippage.py`; the two identical local
  copies are deleted.
- 10+ call sites in `core/tx.py`, `protocols/swap.py`,
  `protocols/contract_call.py`, `protocols/uniswap_v3_lp.py`, and
  `protocols/aave.py` collapsed to a single `finalize_tx(w3, tx)` call.
- `_simulate` / `_strip_nonce` are now internal helpers — new code should
  call `finalize_tx` rather than reaching for the underscored functions.

### Migration notes
- Import path: any external code referencing `InsufficientAllowance` should
  now import from `wallet.core.tokens` (was `wallet.protocols.swap`).

---

## [1.03] — 2026-05-16

Generic contract-call escape hatch — covers the long tail of protocols that
don't justify their own typed `prepare_*` surface. (PR #6.)

### Added
- `wallet contract call <to> "<fn-sig>" [args…] [--value ETH]`. Examples:
  - `wallet contract call 0xToken "transfer(address,uint256)" 0xRecipient 100`
  - `wallet contract call 0xVault "deposit()" --value 0.01 --broadcast`
  - `wallet contract call 0xNFT "setApprovalForAll(address,bool)" 0xOperator false`
- `protocols/contract_call.py`: Solidity-style signature parser (simple
  types + 1D arrays; tuples explicitly rejected with a "use typed
  `prepare_*`" hint), per-type arg coercion, calldata via `eth_abi` + 4-byte
  selector. Routes through the existing `finalize_tx` pipeline.
- `cli/contract.py`: new `wallet contract` typer group.
- `policy.contract_call` category — **hard-blocked for agent callers**
  (escape hatch is humans-only by design — no semantic policy gates exist
  on this path). TTY callers floor-gated by `contract_allowlist` +
  `sentinel_blocklist`; `msg.value` flows through `amount_wei` / `unit=ETH`
  so `max_per_tx{ETH}` still binds.
- Rich preview shows the canonical function signature, decoded args, and
  (truncated) calldata so the human signer can diff against intent.
- `idempotency.fingerprint` includes `cc_calldata` so two different
  functions to the same target with the same `msg.value` (e.g. `pause()` vs
  `unpause()`) don't collapse to the same hash and silently replay.

### Changed
- `_CLASSIFY_TABLE` row order — the `contains "contract call"` row is
  placed **before** the `contains "transfer"` / `contains "approve"` rows
  so a function literally named `transfer(...)` or `approve(...)` cannot
  slip into the typed `send` / `approve` category and dodge the
  `contract_call` agent-block. Adding new rows to the table requires
  maintaining this ordering.

---

## [1.02] — 2026-05-16

Per-pool allowlist for LP funds-in operations — closes the LP-layer
analogue of the swap symbol-confusion attack class. (PR #5.)

### Added
- `Policy.lp_pool_allowlist: list[LpPoolAllowEntry]`. Each entry is a
  canonical V3 pool tuple `(token0, token1, fee)`. Pydantic validators
  reject:
  - unknown fee tiers (must be `{100, 500, 3000, 10000}`)
  - unsorted pairs (V3 invariant: `token0 < token1` by lowercase address —
    NFPM rejects the other ordering, so unsorted entries would be silent
    dead allowlist rows).

### Changed
- `lp_mint` / `lp_increase` now require `(lp_token0_address,
  lp_token1_address, lp_fee) ∈ lp_pool_allowlist` in addition to the
  existing NFPM-in-`contract_allowlist` check. NFPM allowlist alone is
  insufficient because one NFPM serves every V3 pool — an agent that can
  choose `(token0, token1, fee)` could otherwise route funds into a
  counterfeit pool through the legitimate manager.
- Exit ops (`lp_decrease` / `lp_collect`) intentionally remain ungated
  by `lp_pool_allowlist`; they pull funds OUT of positions the user
  already holds, and gating them would block exiting a pool that was
  later removed from the allowlist.
- Empty `lp_pool_allowlist` = no LP mint/increase allowed (fail-closed,
  matches the existing `contract_allowlist` pattern).

### Migration notes
- Existing `policy.json` files without `lp_pool_allowlist` will block all
  `lp_mint` / `lp_increase` after upgrade until at least one entry is
  added. The block reason explicitly says what's missing.

---

## [1.01] — 2026-05-16

First formal release. Closes the two ≥8/10 findings from
`docs/security_review.md` (tier-0 round-4). (PR #2.)

### Security
- **Swap routing no longer uses ERC-20 `symbol()` to decide native-vs-token.**
  A malicious ERC-20 can return any string including `"ETH"`, which on the
  `--via uniswap_v3` and `--via auto` paths would have skipped the
  allowance pre-check AND set `value = amount_in_wei` real native ETH.
  Trust boundary is now a single explicit `TokenInfo.is_native` flag set
  only by `cli/swap.py:_resolve_token_or_native`; routes and orchestrators
  route on the flag, never on symbol. (Vuln 1.)
- **Idempotency fingerprint hardened.** Previously `prepare_swap` wrote
  output token to `swap_token_out_address` but `fingerprint()` only read
  `desc["token_address"]`, so two different swaps with the same router +
  amount + input-symbol collapsed to the same hash and the second silently
  replayed the first's `tx_hash`. Fingerprint now includes `chain_id`
  (not just `chain.name`), `swap_token_in_address`,
  `swap_token_out_address`, `swap_amount_out_min_wei`, `aave_action`,
  `aave_asset_address`; all addresses lowercased so checksum casing cannot
  fork the hash. (Vuln 2.)

### Added
- `TokenInfo.is_native: bool` (defaults False; set True only by the CLI's
  native-symbol resolution path).
- Top-level `replayed: true` flag on idempotency cache-hit JSON envelopes
  so agents can distinguish cache hits from fresh broadcasts without
  parsing `data.phase`.
- Swap preview shows resolved input/output token addresses (defense in
  depth — signers can now diff against the intended token, since symbol
  alone is unsafe).

### Changed
- `protocols/swap.py`, `routes/uniswap_v3.py`, `routes/zerox.py`: switched
  `is_native_*` decisions from `symbol == chain.native_symbol` to
  `token_in.is_native` / `token_out.is_native`.

### Migration notes
- Existing on-disk `idempotency.json` entries computed under the old
  fingerprint will now produce `IdempotencyMismatch` if the same
  `request_id` is reused after upgrade. Entries TTL out in 24h. This is
  a safer outcome than the old silent replay.
