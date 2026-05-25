# Roadmap

## Current state

Phase 1 + Phase 2 DeFi primitives + agent-callable hardening shipped on
Sepolia. Full surface verified end-to-end:

- **Account / transfer / approve / history / book / watch / token** (Phase 1)
- **Swap** (Uniswap V3 direct + 0x aggregator + auto-fallback; auto-unwrap
  WETH→native-ETH on output)
- **Aave V3** supply / withdraw / borrow / repay / faucet + positions / rates
- **Uniswap V3 LP** positions / mint / increase / decrease / collect
- **Generic contract call** escape hatch (TTY-only; agent hard-block)
- **Stuck-tx recovery**: `wallet tx pending / cancel / replace` — EIP-1559
  mempool replacement, `outcome=superseded` race resolution, audit recovery
  fields. Verified on Sepolia (see `docs/wallet-sepolia-mainnet-rehearsal-report.md`).

Releases `1.01` → `1.06` track this; see `CHANGELOG.md` for the per-tag diff
and `git log` for commit-level detail. ~385 unit tests green.

The architecture is fully chain-agnostic: swapping the RPC URL and registering a
new chain in `core/config.py` produces a working wallet on any EVM-compatible
network. **However, technical capability ≠ production safety**. The gaps below
gate the decision to ever put real funds on this wallet.

---

## Mainnet readiness gaps

Split into **code work** (engineering hours) and **configuration setup** (you
just edit files / env vars). The only real engineering blocker is hardware
wallet integration; the other two Tier-1-feeling items are operator setup.

### Code work — engineering blocker

| Gap | Why it matters | Sketch |
|---|---|---|
| **Hardware wallet integration (Ledger / Trezor)** | Hot mnemonic in agent-vault is fine for testnet and small daily-spend hot accounts. Anything beyond a few hundred dollars belongs on a device that never exposes the seed. **Interim mitigation available since `1.09`:** `agent-vault require-presence wallet-main-mnemonic --on` (requires `@kaka-milan-22/agent-vault@0.5.0+`) gates every signature on macOS Touch ID — closes the largest part of the LLM-agent / supply-chain exposure without buying hardware, but does NOT raise the resilience threshold above ~$1.5k; the binary-replacement and post-auth memory-dump gaps remain. **Full rationale + threat model + integration sketch:** [`docs/why_hard_wallet.md`](docs/why_hard_wallet.md). | Refactor `core/signer.py` to dispatch on `account.account_type` (`hd_mnemonic` vs `ledger`). Wallet builds the unsigned tx; signing happens on-device with user button press. Keystore-backed accounts and Ledger accounts coexist in `state.json`. |

### Configuration setup — no code change needed

The wallet already supports both of these out of the box; the operator just
has to set them up before flipping to mainnet.

| Setup | What to do | Notes |
|---|---|---|
| **Configure read / broadcast RPC split** | In `~/.wallet/chains.json`, set `rpc_url` to a normal RPC (drpc / publicnode / Alchemy) and `broadcast_rpc_url` to a private relay (`https://rpc.flashbots.net` or `https://rpc.mevblocker.io`). Leave `mev_exposure` at the default `true` for mainnet. | **Enforced in code as of `1.07`** via the capability-driven policy gate — broadcasts on `mev_exposure: true` chains refuse unless `broadcast_rpc_url` is set and distinct from `rpc_url`. Reasons: `mev-exposed-broadcast-rpc-url-unset` / `mev-exposed-broadcast-equals-read`. See README "Read / broadcast RPC split + MEV protection". |
| **Populate `policy.json` for mainnet** | Edit `~/.wallet/policy.json` with mainnet allowlist addresses + caps. The wallet ships fail-closed; agents are denied until you allow specific recipients/contracts. | A typo here is permanent funds loss. Worth a code addition (Tier 2 below): `wallet policy verify` that round-trips each allowlisted address from Etherscan to confirm it's the contract you intended. |

Suggested mainnet `policy.json` starter (edit before use):

```jsonc
{
  "max_per_tx":  { "ETH": "0.01", "USDC": "100", "USDT": "100" },
  "max_per_day": { "ETH": "0.05", "USDC": "500" },
  "recipient_allowlist": [
    // add your CEX deposit addresses, your other wallets, etc.
    // "0xYourBinanceDepositAddress",
    // "0xYourCoinbaseDepositAddress"
  ],
  "contract_allowlist": [
    // "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",  // Uniswap V3 SwapRouter02
    // "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",  // Aave V3 Pool (mainnet)
    // "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"   // Lido stETH
  ],
  "deny_unlimited_approve": true,
  "first_send_warn": true,
  "sentinel_blocklist": []
}
```

### Tier 2 — strongly recommended before scaling activity (all code work)

| Gap | Why it matters | Sketch |
|---|---|---|
| ~~**`require_private_rpc` policy enforcement**~~ | ~~Trust-but-verify: even if operator sets the right RPC URL today, a future config drift could route broadcasts back to public mempool without anyone noticing.~~ | **Shipped in 1.07** as capability-driven gate, not a policy flag. `ChainConfig` gains `broadcast_rpc_url` + `mev_exposure`; `policy.evaluate` blocks broadcasts on `mev_exposure: true` chains unless the read/broadcast URL split is honored. Per-chain capability instead of a per-policy boolean keeps testnet / L2 / anvil / CI quiet without `--policy-bypass` muscle memory destroying the gate. Phase 2 (hostname allowlist + capability probe) not done — see Known limitations in `docs/plan-mev-protection-and-policy-verify.md`. |
| **`wallet policy verify` command** | A typo in `recipient_allowlist` or `contract_allowlist` is a permanent funds loss. Manual review of a JSON file is unreliable. | New CLI subcommand: for each allowlist entry, fetch contract metadata from Etherscan (verified contract name, deployer, source-code hash) and display a confirmation table. TTY-only. Optionally caches the contract verification timestamp inside policy.json. **Plan written:** see `docs/plan-mev-protection-and-policy-verify.md`. |
| ~~**Stuck-tx recovery** (`wallet tx pending / cancel / replace`)~~ | ~~Mainnet base-fee spikes can leave txs pending for hours; nonce occupation blocks all subsequent ops.~~ | **Shipped in 1.06.** See `docs/plan-stuck-tx-recovery.md` and `CHANGELOG.md` `[1.06]` for the design and verification. |
| **Multi-RPC fallback + cross-check** | Single RPC = SPOF. A malicious / compromised RPC can spoof balances or censor broadcasts. | `chains.json` accepts `rpc_urls: [...]` array. Critical reads (balance, nonce, fee history) consensus across N=2 endpoints; mismatch logged + warned. Broadcast tries primary, falls back to next on `rpc_error`. |
| **agent-vault binary integrity** | `shutil.which("agent-vault")` follows PATH. Any `agent-vault` shim earlier in PATH could exfiltrate every secret on first call. | Pin absolute path in `~/.wallet/config.json` plus sha256 verification on every wallet startup. Fail loudly on mismatch. |
| **Tx simulation (Tenderly / forked node)** | `eth_call` doesn't catch MEV, frontrunning, or state changes between simulation and execution. Critical for swaps and complex DeFi. | Optional Tenderly API integration: simulate tx in a forked-state context, surface gas / state-diff / revert reason / asset changes before user confirms. Free tier covers low volume. |

### Tier 3 — defence-in-depth, lower priority

| Gap | Why it matters | Sketch |
|---|---|---|
| **Memory zeroization for derived `private_key`** | CPython `bytes` GC is unpredictable; a process memory dump (crash / debugger) can recover keys briefly. Real-world threat is small but non-zero. | `core/hd.py` returns `bytearray` instead of `bytes`; `core/signer.py` zero-fills after signing. Documented as best-effort due to immutable-string limits in interpreted Python. |
| **Audit log encryption-at-rest** | Plain JSON-lines audit log reveals address activity to anyone with file-system access. | Optional age encryption with a key stored in agent-vault. `wallet audit decrypt` (TTY-only) for forensic review. |
| **EIP-7702 / smart-account support** | Modern UX patterns (gas sponsorship, batched ops, session keys) require smart-account semantics. Pure EOA limits future protocol integrations. | Long-term work; gate behind a Phase 3 milestone. |
| **Multisig / Safe integration** | For team or higher-stakes accounts, single-key custody is insufficient. | `wallet safe propose` builds a Safe transaction; existing pipeline handles policy / audit. Co-signer flow out-of-band. |
| **EIP-712 typed-data signing** | Required by most DeFi frontends (Permit, OpenSea bids, etc.). | Add `wallet sign-typed-data <file.json>` going through policy and audit. |
| **Notification channels for audit anomalies** | Audit log is passive; humans need to be paged on `caller=agent + outcome=rejected` patterns. | `~/.wallet/notify.json` configures webhooks (Slack / Discord / email via SMTP). A tiny daemon tails audit log and fires on configured patterns. |
| **Reorg-aware confirmation tracking** | Sepolia rarely reorgs; mainnet is fast-finality post-merge but not zero. Currently we trust the first inclusion. | After broadcast, poll N confirmations before declaring final; track receipts in a local SQLite. |

---

## Phase 2 — DeFi protocols

Shipped (entry point `src/wallet/protocols/`, each protocol module produces
unsigned txs that flow through `core/tx.py` → `confirm_and_broadcast`):

| Module | Status | Notes |
|---|---|---|
| `protocols/swap.py` + `routes/{auto,zerox,uniswap_v3}.py` | **Shipped** (`1.02` / `1.05`) | 0x aggregator + Uniswap V3 direct with auto-fallback. Slippage cap, allowance pre-check, AllowanceHolder pinning, auto-unwrap WETH→ETH on native output. |
| `protocols/aave.py` | **Shipped** (`1.03`) | Aave V3 supply / withdraw / borrow / repay / faucet. HF-projection in preview; `min_health_factor` policy gate. |
| `protocols/uniswap_v3_lp.py` | **Shipped** (`1.04`) | NFPM positions / mint / increase / decrease / collect. Native ETH wraps action in `multicall([action, refundETH])` for atomic refund. |
| `protocols/contract_call.py` | **Shipped** (`1.03`) | Generic escape hatch. TTY-only; agent hard-block. Allowlist-gated. |

Future protocol candidates (no plan written; in priority order):

| Module | Scope | Notes |
|---|---|---|
| `protocols/lido.py` | Lido staking — `wallet stake 0.5` / `wallet unstake`. | Simplest DeFi extension; minimal new state. |
| `protocols/yearn.py` | ERC-4626 vault deposits. | Per-strategy risk parameters in policy. |
| `protocols/pendle.py` | Pendle PT/YT positions. | Complex; later. |

---

## Out of scope (intentionally deferred)

User-acknowledged items the security review surfaced but explicitly chose to
not implement now:

- **TTY guard for `account create / import`**: user creates wallets manually
  once, then agents only consume. JSON-mode refuse + skill discipline cover
  the remaining LLM-context leak surface.
- **WebSocket / streaming JSON output**: only single-envelope JSON for now;
  streaming reserved for Phase 2 long-running ops (e.g., DCA strategies).
- **Per-account spending caps in policy**: current caps are global per-symbol;
  per-account differentiation can be added if multi-account agent flows
  emerge.

---

## Updating this roadmap

Items move out of this file into commit history once shipped. Tier 1 items
should never be marked "shipped" without an end-to-end mainnet smoke test on
a low-balance hot wallet first.
