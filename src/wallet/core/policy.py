"""Policy: hard pre-broadcast gate that protects against agent abuse.

Loaded from `~/.wallet/policy.json` (alongside state.json). When the file is
absent, agents are denied by default — fail-closed; humans must run
`wallet policy init` once.

`evaluate(prepared, state, caller, *, bypass=False)` returns a `Decision`:
- `allowed=True` + severity="allow"  → proceed silently
- `allowed=True` + severity="warn"   → TTY: prompt confirmation; agent: blocked
- `allowed=False`                    → block; reason recorded in audit
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from wallet.core.config import atomic_write_text, data_root
from pydantic import BaseModel, Field, field_validator, model_validator

from wallet.core.tokens import MAX_UINT256
from wallet.storage.state import WalletState

__all__ = [
    "Decision",
    "Policy",
    "default_policy",
    "evaluate",
    "load_policy",
    "policy_path",
    "save_policy",
]


# Valid Uniswap V3 fee tiers in basis-points × 100 (matches FEE_TIERS in
# protocols/routes/uniswap_v3.py). Any pool-allowlist entry outside this set
# is a config bug — there are no pools at other fees.
_V3_FEE_TIERS = frozenset({100, 500, 3000, 10000})


class LpPoolAllowEntry(BaseModel):
    """One entry in `lp_pool_allowlist` — a specific (token0, token1, fee) pool.

    V3 invariant: token0 address (lowercased hex) must be < token1 address.
    NFPM rejects any other ordering, so storing entries the other way around
    would be a silent dead allowlist row. We enforce on load.
    """

    token0: str
    token1: str
    fee: int

    @field_validator("token0", "token1")
    @classmethod
    def _checksum_address(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("0x") and len(v) == 42):
            raise ValueError(f"not a 0x-prefixed 20-byte address: {v!r}")
        return v.lower()

    @field_validator("fee")
    @classmethod
    def _known_fee_tier(cls, v: int) -> int:
        if int(v) not in _V3_FEE_TIERS:
            raise ValueError(
                f"fee {v} is not a known V3 tier {sorted(_V3_FEE_TIERS)}"
            )
        return int(v)

    @model_validator(mode="after")
    def _token0_lt_token1(self) -> "LpPoolAllowEntry":
        if self.token0 >= self.token1:
            raise ValueError(
                f"V3 requires token0 < token1; got {self.token0} >= {self.token1}. "
                f"Sort by address (lowercase hex) before adding to the allowlist."
            )
        return self


class Policy(BaseModel):
    """Schema for ~/.wallet/policy.json."""

    max_per_tx: dict[str, str] = Field(default_factory=dict)
    """symbol -> Decimal-as-string. e.g. {"ETH": "0.005"}"""

    max_per_day: dict[str, str] = Field(default_factory=dict)
    """symbol -> Decimal-as-string. Computed by summing today's broadcasts in audit.log."""

    recipient_allowlist: list[str] = Field(default_factory=list)
    """Addresses (0x...) or aliases (@name, bare-name) acceptable as `to` for sends."""

    contract_allowlist: list[str] = Field(default_factory=list)
    """Contract addresses (0x...) acceptable as `spender` in approve."""

    deny_unlimited_approve: bool = True
    """Reject any approve where amount == 2^256-1."""

    first_send_warn: bool = True
    """Warn (TTY) / block (agent) on first send to an address never seen before."""

    sentinel_blocklist: list[str] = Field(default_factory=list)
    """Hard deny — overrides any allowlist match. For known scams / drainers."""

    lp_pool_allowlist: list[LpPoolAllowEntry] = Field(default_factory=list)
    """(token0, token1, fee) pools acceptable for `lp_mint` / `lp_increase`.

    NFPM-in-contract_allowlist already gates the manager contract, but a single
    NFPM serves every V3 pool — without this list, an attacker who can choose
    the (token0, token1, fee) inputs (e.g. a compromised agent) can route funds
    into a counterfeit pool through the legitimate NFPM. Empty list = no LP
    mint/increase allowed (fail-closed, same pattern as contract_allowlist).
    Exit ops (`lp_decrease` / `lp_collect`) are NOT gated by this — they pull
    funds OUT of a pool the user already owns NFT positions in."""

    min_health_factor: float | None = None
    """When set, borrow / withdraw is blocked if the estimated post-op
    health factor would drop below this value. Aave's own check reverts at
    HF < 1.0; setting `min_health_factor: 1.5` blocks before that, giving
    a comfortable margin against price-volatility liquidation."""


class Decision(BaseModel):
    allowed: bool
    reason: str = ""
    severity: Literal["allow", "warn", "block"] = "allow"


def policy_path() -> Path:
    return data_root() / "policy.json"


def load_policy() -> Policy | None:
    p = policy_path()
    if not p.exists():
        return None
    return Policy.model_validate_json(p.read_text())


def save_policy(policy: Policy) -> None:
    atomic_write_text(policy_path(), policy.model_dump_json(indent=2))


def default_policy() -> Policy:
    """Safe starting policy generated by `wallet policy init`."""
    return Policy(
        max_per_tx={"ETH": "0.005"},
        max_per_day={"ETH": "0.05"},
        recipient_allowlist=[],
        contract_allowlist=[],
        deny_unlimited_approve=True,
        first_send_warn=True,
        sentinel_blocklist=[],
    )


# --- evaluation --------------------------------------------------------------


def _category(prepared) -> str:
    """Classify the prepared tx into 'send' / 'approve' / 'swap' / 'aave_*' /
    'lp_*' / 'cancel' / 'replace' / 'contract_call' / 'unknown'."""
    kind = prepared.description.get("kind", "")
    # `contract call <fn>` is the typed-policy escape hatch — match by prefix
    # since the suffix varies per function. Routed through a dedicated
    # category so we can hard-block it for agent callers below.
    if kind.startswith("contract call"):
        return "contract_call"
    # Stuck-tx ops have their own categories; replace delegates to the
    # original op's category so the replacement faces the same policy gates
    # the original did (recipient_allowlist, contract_allowlist, etc.).
    if kind == "tx cancel":
        return "cancel"
    if kind == "tx replace":
        original_kind = prepared.description.get("original_kind", "")
        if original_kind:
            class _ShimPrepared:
                description = {**prepared.description, "kind": original_kind}
            return _category(_ShimPrepared())
        return "replace"
    if kind == "swap":
        return "swap"
    if kind == "aave supply":
        return "aave_supply"
    if kind == "aave withdraw":
        return "aave_withdraw"
    if kind == "aave faucet":
        return "aave_faucet"
    if kind == "aave borrow":
        return "aave_borrow"
    if kind == "aave repay":
        return "aave_repay"
    if kind == "uniswap_v3 lp_mint":
        return "lp_mint"
    if kind == "uniswap_v3 lp_increase":
        return "lp_increase"
    if kind == "uniswap_v3 lp_decrease":
        return "lp_decrease"
    if kind == "uniswap_v3 lp_collect":
        return "lp_collect"
    if "approve" in kind:
        return "approve"
    if "transfer" in kind:
        return "send"
    return "unknown"


def _resolve_allowlist_targets(entries: list[str], state: WalletState) -> set[str]:
    """Resolve a mixed list of 0x addresses / @aliases / bare names to a
    set of lowercased addresses."""
    out: set[str] = set()
    for entry in entries:
        e = entry.strip()
        if e.startswith("0x") and len(e) == 42:
            out.add(e.lower())
            continue
        needle = e[1:] if e.startswith("@") else e
        if needle in state.book:
            out.add(state.book[needle].lower())
        for a in state.accounts:
            if a.name == needle:
                out.add(a.address.lower())
        for w in state.watch:
            if w.label == needle:
                out.add(w.address.lower())
    return out


def _today_outflow_wei(unit: str) -> int:
    """Sum amount_wei across audit.log entries with outcome=broadcast and
    matching unit, dated UTC-today. Returns 0 if no log."""
    from wallet.storage.audit import audit_path

    p = audit_path()
    if not p.exists():
        return 0

    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0
    with open(p) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("outcome") != "broadcast":
                continue
            if obj.get("unit") != unit:
                continue
            if not str(obj.get("ts", "")).startswith(today_prefix):
                continue
            try:
                total += int(obj.get("amount_wei", "0"))
            except (ValueError, TypeError):
                continue
    return total


def _has_seen_recipient(addr: str | None, state: WalletState) -> bool:
    """Is `addr` already known via accounts / book / watch / prior broadcasts?"""
    if not addr:
        return False

    addr_lower = addr.lower()
    if addr_lower in {a.address.lower() for a in state.accounts}:
        return True
    if addr_lower in {v.lower() for v in state.book.values()}:
        return True
    if addr_lower in {w.address.lower() for w in state.watch}:
        return True

    from wallet.storage.audit import audit_path

    p = audit_path()
    if not p.exists():
        return False
    with open(p) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                obj.get("outcome") == "broadcast"
                and (obj.get("to") or "").lower() == addr_lower
            ):
                return True
    return False


def evaluate(
    prepared,
    state: WalletState,
    caller: str,
    *,
    bypass: bool = False,
) -> Decision:
    """Run the policy decision tree. See module docstring for severity semantics."""
    policy = load_policy()

    # --- bypass handling ---
    if bypass:
        if caller == "agent":
            return Decision(
                allowed=False,
                reason="bypass:not-allowed-in-agent-mode",
                severity="block",
            )
        return Decision(
            allowed=True,
            reason="bypass:tty",
            severity="warn",
        )

    # --- no policy file ---
    if policy is None:
        return Decision(
            allowed=False,
            reason="no-policy-configured-run-wallet-policy-init",
            severity="block",
        )

    desc = prepared.description
    category = _category(prepared)
    amount_wei: int = int(desc.get("amount_wei", 0))
    unit: str | None = desc.get("amount_unit")
    decimals: int = int(desc.get("amount_decimals", 18))
    target: str | None = desc.get("to") or desc.get("spender")

    # --- 1. sentinel blocklist (highest priority) ---
    if target and target.lower() in {a.lower() for a in policy.sentinel_blocklist}:
        return Decision(allowed=False, reason="sentinel-blocklisted", severity="block")

    # --- 1a. cancel: 0-value self-send at a specific nonce. Bypasses
    # recipient_allowlist (recipient is the sender itself) but still must
    # satisfy structural invariants — otherwise an attacker who can mint a
    # "tx cancel" description label could route value to themselves under
    # the policy bypass.
    if category == "cancel":
        sender = desc.get("from")
        if not desc.get("is_self_send_for_cancel"):
            return Decision(
                allowed=False,
                reason="cancel-flag-missing",
                severity="block",
            )
        if not sender or not target or sender.lower() != target.lower():
            return Decision(
                allowed=False,
                reason="cancel-must-be-self-send",
                severity="block",
            )
        if amount_wei != 0:
            return Decision(
                allowed=False,
                reason="cancel-must-be-zero-value",
                severity="block",
            )
        return Decision(allowed=True, reason="cancel-allowed", severity="allow")

    # --- 1b. replace with unrecoverable original kind: agent must run
    # explicit cancel + re-prepare instead.
    if category == "replace":
        return Decision(
            allowed=False,
            reason="replace-original-kind-unknown",
            severity="block",
        )

    # --- 1c. contract_call category: agent-block + contract_allowlist floor ---
    # This is the typed-policy escape hatch. Per-op semantic gates (HF check,
    # pool allowlist, swap router allowlist, deny_unlimited_approve, etc.) do
    # NOT exist on this path — the wallet has no semantic model of what the
    # calldata does. So we hard-block for agent callers (humans-only) and
    # require the target contract to be explicitly allowlisted.
    if category == "contract_call":
        if caller == "agent":
            return Decision(
                allowed=False,
                reason="contract-call-not-allowed-for-agent",
                severity="block",
            )
        targets = {a.lower() for a in policy.contract_allowlist}
        if target and target.lower() not in targets:
            return Decision(
                allowed=False,
                reason="contract-call-target-not-in-contract-allowlist",
                severity="block",
            )

    # --- 2. approve-specific checks ---
    if category == "approve":
        if amount_wei == MAX_UINT256 and policy.deny_unlimited_approve:
            return Decision(
                allowed=False, reason="unlimited-approve-denied", severity="block"
            )
        spender = desc.get("spender")
        contracts = {a.lower() for a in policy.contract_allowlist}
        if spender and spender.lower() not in contracts:
            return Decision(
                allowed=False,
                reason="spender-not-in-contract-allowlist",
                severity="block",
            )

    # --- 3. send-specific recipient allowlist ---
    if category == "send":
        recipient = desc.get("to")
        allowed_addrs = _resolve_allowlist_targets(policy.recipient_allowlist, state)
        if recipient and recipient.lower() not in allowed_addrs:
            return Decision(
                allowed=False,
                reason="recipient-not-in-allowlist",
                severity="block",
            )

    # --- 3b. swap-specific: router must be in contract_allowlist ---
    if category == "swap":
        router = desc.get("to")
        contracts = {a.lower() for a in policy.contract_allowlist}
        if router and router.lower() not in contracts:
            return Decision(
                allowed=False,
                reason="swap-router-not-in-contract-allowlist",
                severity="block",
            )

    # --- 3c. aave supply / withdraw: pool must be in contract_allowlist ---
    if category in ("aave_supply", "aave_withdraw"):
        pool = desc.get("to")
        contracts = {a.lower() for a in policy.contract_allowlist}
        if pool and pool.lower() not in contracts:
            return Decision(
                allowed=False,
                reason="aave-pool-not-in-contract-allowlist",
                severity="block",
            )

    # --- 3d. aave faucet: faucet contract must be in contract_allowlist ---
    if category == "aave_faucet":
        faucet = desc.get("to")
        contracts = {a.lower() for a in policy.contract_allowlist}
        if faucet and faucet.lower() not in contracts:
            return Decision(
                allowed=False,
                reason="aave-faucet-not-in-contract-allowlist",
                severity="block",
            )

    # --- 3e. aave borrow / repay: pool in contract_allowlist ---
    if category in ("aave_borrow", "aave_repay"):
        pool = desc.get("to")
        contracts = {a.lower() for a in policy.contract_allowlist}
        if pool and pool.lower() not in contracts:
            return Decision(
                allowed=False,
                reason="aave-pool-not-in-contract-allowlist",
                severity="block",
            )

    # --- 3g. uniswap_v3 LP ops: NFPM (description.to) must be in contract_allowlist ---
    # Every NFPM call routes through the same address regardless of the LP
    # action, so a single allowlist entry covers mint / increase / decrease /
    # collect. Without this, an agent that can re-range could direct funds at
    # a counterfeit NFPM under a router-only allowlist.
    if category in ("lp_mint", "lp_increase", "lp_decrease", "lp_collect"):
        nfpm = desc.get("to")
        contracts = {a.lower() for a in policy.contract_allowlist}
        if nfpm and nfpm.lower() not in contracts:
            return Decision(
                allowed=False,
                reason="lp-nfpm-not-in-contract-allowlist",
                severity="block",
            )

    # --- 3h. uniswap_v3 LP funds-IN ops: (token0, token1, fee) must be in
    # lp_pool_allowlist. NFPM allowlist alone is insufficient because the same
    # NFPM serves every pool — an agent that can choose token addresses can
    # route funds into a scam pool through the legitimate manager. Only
    # enforced on ops that move funds INTO a pool (mint / increase); exit ops
    # (decrease / collect) operate on a position the user already holds and
    # don't need this gate.
    if category in ("lp_mint", "lp_increase"):
        t0 = (desc.get("lp_token0_address") or "").lower()
        t1 = (desc.get("lp_token1_address") or "").lower()
        fee_raw = desc.get("lp_fee")
        try:
            fee = int(fee_raw) if fee_raw is not None else None
        except (TypeError, ValueError):
            fee = None
        if not t0 or not t1 or fee is None:
            return Decision(
                allowed=False,
                reason="lp-pool-fields-missing-from-description",
                severity="block",
            )
        pool_allowed = any(
            e.token0 == t0 and e.token1 == t1 and e.fee == fee
            for e in policy.lp_pool_allowlist
        )
        if not pool_allowed:
            return Decision(
                allowed=False,
                reason=f"lp-pool-not-in-allowlist:{t0}/{t1}/{fee}",
                severity="block",
            )

    # --- 3f. min_health_factor enforcement for ops that reduce HF ---
    if (
        policy.min_health_factor is not None
        and category in ("aave_borrow", "aave_withdraw")
    ):
        hf_after_str = desc.get("aave_estimated_hf_after")
        if hf_after_str is not None and hf_after_str != "inf":
            try:
                hf_after = float(hf_after_str)
            except (ValueError, TypeError):
                hf_after = None
            if hf_after is not None and hf_after < policy.min_health_factor:
                return Decision(
                    allowed=False,
                    reason=(
                        f"hf-would-drop-below-min:{hf_after:.3f}<"
                        f"{policy.min_health_factor}"
                    ),
                    severity="block",
                )

    # --- 4. per-tx amount cap ---
    if unit and unit in policy.max_per_tx:
        cap_human = Decimal(policy.max_per_tx[unit])
        cap_wei = int(cap_human * (Decimal(10) ** decimals))
        if amount_wei > cap_wei:
            return Decision(
                allowed=False,
                reason=f"max-per-tx-exceeded:{unit}:{cap_human}",
                severity="block",
            )

    # --- 5. per-day amount cap (consults audit log) ---
    if unit and unit in policy.max_per_day:
        cap_human = Decimal(policy.max_per_day[unit])
        cap_wei = int(cap_human * (Decimal(10) ** decimals))
        prior = _today_outflow_wei(unit)
        if amount_wei + prior > cap_wei:
            return Decision(
                allowed=False,
                reason=f"max-per-day-exceeded:{unit}:{cap_human}:prior={prior}",
                severity="block",
            )

    # --- 6. first-send warn / block ---
    if category == "send" and policy.first_send_warn:
        recipient = desc.get("to")
        if not _has_seen_recipient(recipient, state):
            if caller == "agent":
                return Decision(
                    allowed=False,
                    reason="first-send-blocked-for-agent",
                    severity="block",
                )
            return Decision(
                allowed=True,
                reason="first-send-warn",
                severity="warn",
            )

    return Decision(allowed=True, reason="allow", severity="allow")
