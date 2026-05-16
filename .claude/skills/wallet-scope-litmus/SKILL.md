---
name: wallet-scope-litmus
description: Product-positioning gate for the wallet project. MUST be invoked before proposing, planning, or implementing ANY new wallet CLI command, subcommand, or capability — including "convenience" composite operations, scheduled loops, auto-anything features, or strategy logic. Also triggers when the user asks "should we add X to wallet", when reviewing PRs that introduce new commands, or when designing the wallet's API surface. The wallet is a security-first execution tool that exposes only on-chain primitives; strategy lives in external agent layers. This skill defines the litmus test that gates what may enter the wallet codebase.
---

# Wallet Scope Litmus — Product Positioning

## The three product axioms (USER-DEFINED, NON-NEGOTIABLE)

1. **Security is the #1 priority.** Wallet operates in an adversarial environment — agents can be prompt-injected, LLMs can hallucinate, data sources can be compromised. Wallet must NEVER trust the caller; it relies only on its own machine-verifiable guardrails (`policy.contract_allowlist`, `policy.max_per_tx`, `policy.max_per_day`, `policy.first_send_warn`, `policy.deny_unlimited_approve`, `policy.min_health_factor`, idempotency fingerprints, `simulate`, audit log).

2. **Wallet does ONLY primitive on-chain actions** a wallet should do: send / approve / swap / Aave supply / withdraw / borrow / repay / faucet / LP positions read / LP mint / LP increase / LP decrease / LP collect / portfolio + balance reads. Nothing else.

3. **Strategy lives outside.** External agents — rule scripts, LLM-driven analyzers, schedulers — read on-chain and web data, decide what to do, and drive wallet via the CLI. Wallet stays strategy-agnostic so the agent layer can be iterated, replaced, or run in parallel without re-auditing wallet.

## The litmus test

Before adding ANY new command to the wallet, ask:

> **"Does deciding whether to execute this action require strategic judgment?"**

- **Yes** → REJECT. Belongs in the external agent layer, not wallet.
- **No** → may be considered (still subject to the security review pipeline).

"Strategic judgment" means deciding *when* to act, *how much* to commit, *which* pool / asset / range to use, or *whether* current market conditions warrant the action.

## Concrete classifications

**PASS the litmus (single-decision primitives — these are the kind of things that belong):**
- `wallet send`, `wallet approve`, `wallet swap`
- `wallet aave supply / withdraw / borrow / repay / faucet`
- `wallet lp positions / mint / increase / remove / collect`
- `wallet balance / portfolio / history` (pure reads, no signing)
- `wallet account / book / watch / token` (local config management)

**FAIL the litmus (strategy disguised as ergonomics — do NOT add):**
- `wallet lp rerange` — *when* to rerange and *how wide* the new range is, are strategy decisions
- `wallet lp auto-compound` — *when* to compound is a strategy decision
- `wallet pool-migrate` — *which* pool to switch to is a strategy decision
- `wallet aave auto-deleverage` — *when* HF is "too low" is a strategy decision (the existing `policy.min_health_factor` is a SAFETY refusal, not auto-action — that's correct)
- Any `wallet loop` / `wallet schedule` / `wallet daemon` style long-running orchestrator
- Any command that reads off-chain data sources (subgraph, DeFiLlama, news) — agents do that, wallet does not

**Grey zone (acceptable because they don't sign anything):**
- Aggregator reads across protocols (`wallet portfolio`)
- Help / introspection / chain config commands

## Why this rule defends itself

The natural failure mode for layered architectures is "convenience creep": agent authors will keep asking for composite commands because chaining 4 CLI calls "feels clunky". Each composite they get smuggles strategy into wallet, balloons the policy / audit / idempotency surface, and erodes the "wallet is dumb" property that lets the agent layer iterate freely. The right answer to "this is 4 calls, can wallet just do all 4?" is **"yes, that's the point — each step gets its own `policy / simulate / audit / idempotency` check, and you can stop / inspect / replay between them"**.

## LLM placement rule

The same axiom #1 ("security first") applies to LLMs as data:

- **LLMs MAY**: explain a strategy decision after the fact, suggest range widths / parameter values to a human, summarize daily activity, write changelogs, classify on-chain events.
- **LLMs MUST NOT** sit in the broadcast path. The thing that decides "call `wallet lp mint --broadcast`" must be deterministic (rule script with a finite, backtestable state machine). LLM-as-prime-mover is unreviewable, untestable, and prompt-injection-prone.

Agents may use an LLM as a *recommendation layer* that emits parameters for a deterministic core to validate and execute, but the LLM is never the last hop before `--broadcast`.

## How to use this skill

When the user (or you, reasoning about future work) proposes a new wallet feature:

1. State the proposed feature in one sentence.
2. Apply the litmus question literally: "Does this require strategic judgment?"
3. If yes → recommend rejection; sketch how it should live in the agent layer instead.
4. If no → it may proceed to the normal design / security review pipeline; still subject to the security invariants documented elsewhere (no `symbol()`-based routing, idempotency fingerprint must include all behavior-changing fields + `chain_id`, every write must pass `_simulate` and `_strip_nonce`, every new `kind` must be classified in `_CLASSIFY_TABLE` and recognized by `policy.evaluate`).
5. Resist ergonomic counter-arguments. "It would be more convenient" is not a reason to break the layering.

This skill overrides ergonomic-improvement requests. It does NOT override explicit user instructions to break the rule — if the user, knowing this skill, still says "add it", surface the rule one more time and then defer to their call.
