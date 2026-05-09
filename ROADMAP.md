# Roadmap

## Current state

Phase 1 + agent-callable hardening complete on Sepolia. Four merged commits:

- `dc881a7` Phase 1 wallet (HD account / send / approve / history / book / watch / token)
- `736de1d` FIFO transport for mnemonic — kernel pipe, plaintext never on disk
- `466f098` Agent abuse defence — policy + idempotency + audit + skill
- `5cd7f95` Agent-friendly output — `--json` / `--quiet` / `--explain` (envvar-driven)

92 unit tests green; end-to-end verified on Sepolia (real signing, real broadcast,
idempotent replay, policy block, FIFO secret transit, audit log).

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
| **Hardware wallet integration (Ledger / Trezor)** | Hot mnemonic in agent-vault is fine for testnet and small daily-spend hot accounts. Anything beyond a few hundred dollars belongs on a device that never exposes the seed. | Refactor `core/signer.py` to swap in `eth_account.LedgerSignerMiddleware` (or `ledgereth`). Wallet builds the unsigned tx; signing happens on-device with user button press. Keystore-backed accounts and Ledger accounts coexist in `state.json`. |

### Configuration setup — no code change needed

The wallet already supports both of these out of the box; the operator just
has to set them up before flipping to mainnet.

| Setup | What to do | Notes |
|---|---|---|
| **Use a MEV-protected RPC for broadcasts** | `export WALLET_ETH_RPC=https://rpc.flashbots.net` (or MEV Blocker). Wallet broadcasts via this URL. | Optional code addition (Tier 2 below): `policy.json` field `require_private_rpc: true` to *enforce* this — refuses broadcast against public RPC. Without that field, you trust yourself to set the env var correctly. |
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
| **`require_private_rpc` policy enforcement** | Trust-but-verify: even if operator sets the right RPC URL today, a future config drift could route broadcasts back to public mempool without anyone noticing. | Add `require_private_rpc: bool` to `Policy`. `policy.evaluate` rejects broadcast when caller's RPC URL hostname matches a configurable public-RPC blocklist (publicnode, Infura HTTPS, etc.). |
| **`wallet policy verify` command** | A typo in `recipient_allowlist` or `contract_allowlist` is a permanent funds loss. Manual review of a JSON file is unreliable. | New CLI subcommand: for each allowlist entry, fetch contract metadata from Etherscan (verified contract name, deployer, source-code hash) and display a confirmation table. TTY-only. Optionally caches the contract verification timestamp inside policy.json. |
| **Stuck-tx recovery** | Mainnet base-fee spikes leave broadcast txs pending for hours. No way to cancel or speed up currently. | `wallet tx replace <nonce> --speedup-pct 25` (re-signs with same nonce, higher gas). `wallet tx cancel <nonce>` (sends 0-value to self at higher gas). Both go through the same policy / idempotency / audit pipeline. |
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

## Phase 2 — DeFi protocols (architecturally ready, not yet implemented)

Entry point planned: a new `src/wallet/protocols/` directory alongside `core/`,
each protocol module producing unsigned txs that flow through the existing
`core/tx.py` → `confirm_and_broadcast` pipeline.

| Module | Scope | Notes |
|---|---|---|
| `protocols/swap.py` | DEX aggregator (0x or 1inch). `wallet swap <from> <to> <amount> [--slippage-bps N]`. | Required: MEV-protected RPC, slippage cap from `policy.json`, multi-step proposal (`approve` + `swap`) with single user confirmation. |
| `protocols/aave.py` | Aave V3 supply / withdraw / borrow / repay. `wallet aave supply USDC 100`. | Health-factor monitoring + liquidation-line warning baked into preview. |
| `protocols/lido.py` | Lido staking — `wallet stake 0.5` (deposits to stETH) and `wallet unstake`. | Simplest DeFi entry; minimal new state. |
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
