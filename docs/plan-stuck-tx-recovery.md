# Plan: Stuck-tx recovery (cancel / speedup)

**Status**: draft — pending review before implementation
**Estimated work**: 1.5 days
**Roadmap reference**: ROADMAP.md Tier 2 "Stuck-tx recovery"
**Priority**: 高 — MetaMask / Rabby / Frame 都把这个当 table stakes，mainnet 基本必备

---

## Context

EIP-1559 下 `max_fee_per_gas` 必须 ≥ 当前 base_fee 才会被打包。Mainnet base-fee 在 mint event / 大盘异动 / MEV 高峰时段会从 10 gwei 突然 spike 到 50-100 gwei，**你之前用 15 gwei 广播的 tx 立刻被甩出有效区间**——卡在 mempool 里几分钟到几小时，nonce 占着位，后面所有 tx 全堵。

当前 wallet 完全没招：

- 没有列 pending tx 的命令
- 没有重广播逻辑
- 用户只能盯 Etherscan，靠 web 钱包（MetaMask）连同账户去 cancel——但 MetaMask 不能签 wallet 这边的 mnemonic 派生地址，**只能干等**

MetaMask / Rabby 都有 "Cancel" / "Speed Up" 按钮多年了，操作是 EIP-1559 mempool replacement 协议规定的标准动作：

- **Replacement rule**: 同 `from` + 同 `nonce` + `max_fee_per_gas` 和 `max_priority_fee_per_gas` 都 ≥ 旧值 × 110% → 新 tx 替换旧 tx
- **Cancel**: 用 0-value 自转 + 同 nonce + 高 gas，新 tx 落账占位，原 tx 永远没机会进
- **Speedup**: 用相同 calldata（同 `to` / `value` / `data`） + 同 nonce + 高 gas，新 tx 直接替换原 tx

本 plan 在 wallet CLI 加 `wallet tx pending / cancel / replace`，复用现有 `confirm_and_broadcast` 管道（policy / idempotency / audit 全过一遍）。

---

## 解决的问题

| 场景 | 当前后果 | 改进后 |
|---|---|---|
| Base-fee spike，原 tx 卡 mempool 30 min+ | 干等 / 去 Etherscan 手算 / 用 MetaMask 兜（但派生地址 MetaMask 没私钥） | `wallet tx cancel <nonce>` 5 秒清场 |
| nonce 占位导致后续 tx 全堵 | 整个账户瘫痪，所有 send / swap / aave 都发不出去 | cancel 占位 tx → nonce 释放 → 后续 tx 正常 |
| Swap 价差时间敏感，原 gas 给低了想加速 | 等到上链时价差已被吃掉 | `wallet tx replace <nonce> --speedup-pct 50` 重发同 calldata |
| 误操作刚发完想撤销 | 一旦签名广播无法撤回（除非 stuck） | 趁未上链时立刻 cancel，**仅在 tx 还在 mempool 时有效** |
| Agent 自动化场景，cron 发出去的 tx 卡死 | 没有自愈，cron 后续轮次撞 nonce 全失败 | agent 也能调 `wallet tx replace --request-id <new>` 自愈（前提 policy 允许该 nonce 的同等 op） |

**显式不解决**：

- 已上链的 tx 撤回（不可能，链上规则）
- mempool 之外的 tx propagation 问题（每个 RPC 看到的 mempool 不一样）
- gas war / front-running（这是 MEV 议题，由 `broadcast_rpc_url` flashbots 解决）

---

## 设计

### 命令面

```
wallet tx pending [--account <name>] [--chain <name>]
    列出当前账户的 pending tx（broadcast 后无 receipt）
    输出: nonce | tx_hash | submitted_at (age) | kind | gas (max_fee gwei) | to

wallet tx cancel <nonce> [--speedup-pct N] [--broadcast] [--request-id <id>]
    发 0-value 自转占位，替换该 nonce 上的 stuck tx
    默认 dry-run，--broadcast 实发

wallet tx replace <nonce> [--speedup-pct N] [--broadcast] [--request-id <id>]
    用同 calldata 重发，速度提 N% (默认 25%)
    必须从 idempotency.json / audit.log 能查回原 tx description 才能重建
```

### Pending 检测

**数据源**：复用 `~/.wallet/idempotency.json` —— 每个广播过的 tx 都有 `CachedResult { tx_hash, nonce, outcome, created_at, expires_at }`。

**判定逻辑**（`tx pending` 命令）：

```python
def list_pending(w3: Web3, account_address: str) -> list[PendingTx]:
    candidates = []
    for req_id, cached in load_idempotency().items():
        if cached.outcome != "broadcast" or cached.tx_hash is None:
            continue
        if _account_of(cached) != account_address:
            continue
        # 检查链上是否已落账
        try:
            receipt = w3.eth.get_transaction_receipt(cached.tx_hash)
            if receipt and receipt.blockNumber:
                continue  # 已上链，跳过
        except TransactionNotFound:
            pass  # 还没上链
        # 再检查 mempool（可选，可能慢）
        candidates.append(_to_pending_tx(cached, w3))
    return sorted(candidates, key=lambda p: p.nonce)
```

**Edge**：idempotency.json 24h TTL；超过 24h 还没上链的 tx 基本死透了，加 `--include-expired` 才显示。

### Cancel 实现

```python
# src/wallet/core/tx_replace.py (新文件)

def prepare_cancel(
    w3: Web3,
    chain: ChainConfig,
    account: AccountState,
    nonce: int,
    speedup_pct: int = 25,
) -> PreparedTx:
    """0-value self-send at given nonce with bumped gas."""
    # 1. 查现有 nonce 上的 tx (from mempool 或 idempotency cache)，拿到旧 gas
    old_max_fee, old_priority = _fetch_stuck_gas(w3, account, nonce)

    # 2. 计算新 gas: max(旧 × 1.1, 当前 base_fee × 2 + bumped_priority)
    bump = 1 + speedup_pct / 100
    new_priority = max(int(old_priority * 1.1), int(old_priority * bump))
    base_fee = w3.eth.get_block("latest").baseFeePerGas
    new_max_fee = max(int(old_max_fee * 1.1), int(base_fee * 2 + new_priority))

    tx = {
        "from": account.address,
        "to": account.address,        # 自转
        "value": 0,
        "data": "0x",
        "gas": 21000,
        "nonce": nonce,
        "maxFeePerGas": new_max_fee,
        "maxPriorityFeePerGas": new_priority,
        "chainId": chain.chain_id,
        "type": 2,
    }
    return PreparedTx(
        tx=tx,
        estimated_fee_wei=21000 * new_max_fee,
        description={
            "kind": "tx cancel",
            "cancel_nonce": nonce,
            "to": account.address,
            "amount_wei": 0,
            "amount_unit": chain.native_symbol,
            "amount_decimals": 18,
            "is_self_send_for_cancel": True,   # policy 例外标记
        },
    )
```

### Replace 实现

```python
def prepare_replacement(
    w3: Web3,
    chain: ChainConfig,
    account: AccountState,
    nonce: int,
    speedup_pct: int = 25,
) -> PreparedTx:
    """Re-send original calldata at higher gas, same nonce."""
    # 1. 找原 tx：先查 idempotency cache，cache 里记的 description 不够还原完整 tx,
    #    所以还要从 mempool/RPC 查 raw tx
    cached = _find_cached_by_nonce(account.address, nonce)
    if cached is None:
        raise StuckTxError(f"no cached record for nonce {nonce}")

    raw_tx = w3.eth.get_transaction(cached.tx_hash)
    if raw_tx.blockNumber is not None:
        raise StuckTxError(f"tx {cached.tx_hash} already mined at block {raw_tx.blockNumber}")

    # 2. 鼓泡 gas
    new_priority = max(int(raw_tx.maxPriorityFeePerGas * 1.1),
                       int(raw_tx.maxPriorityFeePerGas * (1 + speedup_pct/100)))
    base_fee = w3.eth.get_block("latest").baseFeePerGas
    new_max_fee = max(int(raw_tx.maxFeePerGas * 1.1),
                      int(base_fee * 2 + new_priority))

    tx = {
        "from": account.address,
        "to": raw_tx.to,
        "value": raw_tx.value,
        "data": raw_tx.input,
        "gas": raw_tx.gas,
        "nonce": nonce,
        "maxFeePerGas": new_max_fee,
        "maxPriorityFeePerGas": new_priority,
        "chainId": chain.chain_id,
        "type": 2,
    }
    return PreparedTx(
        tx=tx,
        estimated_fee_wei=raw_tx.gas * new_max_fee,
        description={
            "kind": "tx replace",
            "replace_nonce": nonce,
            "original_tx_hash": cached.tx_hash,
            "original_kind": cached.outcome_kind,    # 从 cache 拿原 op kind
            "to": raw_tx.to,
            "amount_wei": raw_tx.value,
            "amount_unit": chain.native_symbol,
            "amount_decimals": 18,
        },
    )
```

### Policy 集成

**关键设计**：replace / cancel **必须走完整 `confirm_and_broadcast` 管道**——包括 policy.evaluate、idempotency.lookup/record、audit.write。原因是 wallet 的整个安全模型就建在这条管道上，开 escape hatch 会破坏 invariant。

具体怎么过 policy：

- **`cancel`**: `description.kind = "tx cancel"`，新 category `"cancel"`。policy 应允许这一类，但需要明确约束：
  - `to == from`（自转）
  - `value == 0`
  - 不需要 `recipient_allowlist` 命中（毕竟是自己）——加一条 evaluate 分支：`is_self_send_for_cancel=True` 时跳过 recipient 检查
  - 仍受 `max_per_tx`（自转 0 值不会触发）+ `sentinel_blocklist` 限制
- **`replace`**: 重新走原 tx 的 category 的 policy 检查。例如原是 `aave borrow`，replacement 也按 aave borrow 走一遍 policy。**等价于"这笔操作 wallet 再批一次"**——这是正确的，因为 replacement 是新签名，policy 应再判一次（policy 文件这期间可能被改严）。

**Policy 字段不加**——cancel 的例外通过 `description` 标记走，不在 policy.json 暴露开关。

### Idempotency

Replace / cancel 自己也走 idempotency：

- 用户必须传新 `--request-id`（agent caller 强制；TTY 可省）
- 缓存 `outcome="broadcast"`、`detail="replacement_of:<原 tx_hash>"`
- 失败模式（"nonce too low" — 原 tx 在 replacement 广播过程中落账了）→ `outcome="superseded"`，audit 单独一条

### Audit

新增 outcome 类型：

```
{"ts": "...", "command": "tx.cancel", "outcome": "broadcast", "nonce": 42, "old_tx_hash": "0x...", "new_tx_hash": "0x...", ...}
{"ts": "...", "command": "tx.replace", "outcome": "broadcast", "nonce": 42, "old_tx_hash": "0x...", "new_tx_hash": "0x...", ...}
{"ts": "...", "command": "tx.cancel", "outcome": "superseded", "reason": "original_landed_first", ...}
```

---

## 触碰文件

| 文件 | 改动 |
|---|---|
| `src/wallet/core/tx_replace.py` | **新文件**：`prepare_cancel` / `prepare_replacement` / `list_pending` / `_fetch_stuck_gas` |
| `src/wallet/cli/tx.py` | **新文件**：typer app `pending` / `cancel` / `replace` 三个命令 |
| `src/wallet/cli/__init__.py` | 注册 `tx` 子命令 |
| `src/wallet/core/policy.py` | `_category` 加 `"cancel"` / `"replace"` 分支；`evaluate` 对 `is_self_send_for_cancel=True` 跳过 recipient 检查；replace 走原 category 复用 |
| `src/wallet/cli/_common.py` | 无改动（管道完全复用） |
| `src/wallet/storage/idempotency.py` | `CachedResult` 加 `outcome_kind: str = ""` 字段（用于 replace 还原原 kind） |
| `tests/test_tx_replace.py` | **新文件**：cancel / replace 单元测试 |
| `tests/test_policy.py` | 加 `"cancel"` 分支测试（self-send 0-value 通过、非 self-send 拒绝） |
| `README.md` | 加 `wallet tx pending/cancel/replace` 示例段 |
| `docs/skills/wallet-agent.skill.md` | 加"卡 tx 时如何调用 replace"流程指引 |
| `ROADMAP.md` | 移除 Tier 2 "Stuck-tx recovery"，进 commit history |

---

## 可复用现有代码

- `core/tx.py:PreparedTx` — 不动，cancel/replace 产出的也是 PreparedTx
- `core/tx.py:broadcast` — 实际广播逻辑直接复用
- `core/signer.py:sign_transaction` — 签名复用
- `core/rpc.py:web3(chain)` — RPC 客户端复用（broadcast 走 `web3_broadcast` 如已有 MEV plan 落地）
- `storage/idempotency.py:lookup/record` — replace/cancel 自己也用 idempotency
- `storage/audit.py:write` — 审计写入复用
- `cli/_common.py:confirm_and_broadcast` — **整条管道复用**，是这个设计能成立的关键

---

## Tradeoff / Risk

**Tradeoff 1: 单线程 nonce 模型**

当前 wallet 假设 nonce 严格递增、单一来源。如果用户在 MetaMask 同时操作同账户、或多个 wallet 进程并发，nonce 排错会更复杂。**第一版**：cancel/replace 只允许对 wallet 自己 idempotency cache 里有记录的 nonce 操作（外部来源的 stuck tx 不接），fail-closed。**后续**：加 `wallet tx pending --include-external` 允许操作外部 tx，但要先链上查 transaction 确认 from 是自己。

**Tradeoff 2: replacement tx 失败模式多**

- 原 tx 在 broadcast 过程中落账 → `nonce too low` → 抓住转成 `outcome=superseded`
- 用户已经在外部 wallet 手工 replace 过 → 同上
- 新 gas 仍不够（base-fee 继续涨）→ replacement 也卡住，递归 replace（用户重跑 `wallet tx replace`，speedup_pct 更大）

需要清晰的 error code 区分：`replacement_underpriced` / `nonce_too_low` / `tx_not_found` / `tx_already_mined`。

**Tradeoff 3: MEV-protected RPC 对 replacement 的支持**

Flashbots Protect、MEV Blocker、Titan、SecureRPC 全部声明支持同 nonce 高 gas 替换，但实际行为有差异（有的需要新 `bundle`，有的接受裸 `eth_sendRawTransaction`）。**实施时必须对每个 endpoint 跑一次集成测试**，把不支持的 endpoint 在 `mev_protected_rpc_allowlist` 注释里标记。

**Risk 1: nonce 撞车**

如果用户在两个进程同时 `wallet tx replace <nonce>`，两个新 tx 都用同 nonce 高 gas。EIP-1559 replacement 规则要求新 max_fee ≥ 旧 × 110%，所以**只有更高 gas 的那个会替换**，另一个被 mempool 拒绝。结果可控但要在 docs 里讲清楚。

**Risk 2: 自转 cancel 仍消耗 gas**

cancel 不是免费的——21000 gas × 高 gwei 可能花 $5-10。Mainnet 拥堵时尤其贵。**这是 EIP-1559 协议代价**，无法避免；UI 上要明确显示 cancel 的 gas 估算（已有逻辑复用）。

**Risk 3: agent 滥用 replace 翻倍花钱**

理论上 agent 可以无限 `replace` 提 gas。Mitigation：

- policy 加 `max_replacement_attempts_per_nonce: int = 3`（默认 3 次）
- 每次 replace 时数 audit log 里对该 nonce 的 replace 次数，超限 reject
- 这个加可选；如果用户对 agent 已经设了 `max_per_day` 上限，多次 replace 也会被那个挡住

第一版不加 `max_replacement_attempts_per_nonce`，观察实际使用频率决定。

---

## Verification

### 单元测试

```sh
uv run pytest tests/test_tx_replace.py -v
uv run pytest tests/test_policy.py -v       # cancel 分支
uv run pytest                                # 全套应仍绿
```

测试覆盖点：

- cancel 产生 PreparedTx，tx 字段都对（self-send / 0 value / 同 nonce / +25% gas）
- replace 从 mocked web3 拉原 tx，重建 PreparedTx，calldata 一致
- `_fetch_stuck_gas` 走 idempotency cache vs 走 mempool 两条路径都覆盖
- policy.evaluate 对 cancel 的 0-value self-send 放行
- policy.evaluate 对非自转的 "cancel" 类（攻击者伪造）拒绝
- idempotency 同 request-id 重放返回缓存
- replacement gas 计算正确：max(旧×1.1, base×2+priority)

### Sepolia 集成验证

零成本场景：

1. 故意广播一笔超低 gas（priority=1 wei）的 send → mempool 等
2. `wallet tx pending` 应列出该 nonce
3. `wallet tx cancel <nonce>` dry-run，看 preview 数字合理
4. `wallet tx cancel <nonce> --broadcast --request-id cancel-test-1`
5. 等几秒，再次 `wallet tx pending` —— 应消失
6. `wallet history` —— 应看到自转 0 ETH 的 tx 落账

replace 测试：

1. 广播一笔正常 send（合理 gas，应在 1 个 block 内落账）
2. **快速**在落账前 `wallet tx replace <nonce>` —— 抢 race condition
3. 实际能否抢到看运气；抢不到也是 PASS（`outcome=superseded` 正确报）
4. 抢到时 `wallet history` 看到新 tx_hash 替换旧 tx_hash

agent 路径：

1. `WALLET_JSON=1 wallet tx pending` —— 返回 JSON 数组
2. `WALLET_JSON=1 wallet tx cancel 42 --broadcast --request-id ...` —— 标准 envelope，含 `data.old_tx_hash` 和 `data.new_tx_hash`

---

## 出口标准

- `pytest` 全绿
- Sepolia 完成上述集成场景
- README 加 `wallet tx pending/cancel/replace` 示例段
- ROADMAP 把 Tier 2 该项移除
- 不引入新的非可选依赖

---

## 实施顺序（半天 + 1 天）

**Day 1 上午**（核心数据 + cancel）：

1. `core/tx_replace.py`：`PendingTx` dataclass、`list_pending`、`_fetch_stuck_gas`、`prepare_cancel`
2. `core/policy.py`：加 `cancel` category + self-send 例外分支
3. `tests/test_tx_replace.py` 头 5 个测试
4. sepolia 跑 cancel 流程

**Day 1 下午**（CLI + pending 命令）：

5. `cli/tx.py`：`pending` / `cancel` 命令，复用 `confirm_and_broadcast`
6. `cli/__init__.py` 注册
7. `tests/test_tx_replace.py` 补 pending 测试

**Day 2 上午**（replace）：

8. `prepare_replacement` + 从 `web3.eth.get_transaction` 还原原 calldata
9. `cli/tx.py:replace` 命令
10. `tests/test_tx_replace.py` 补 replace 测试

**Day 2 下午**（集成 + 文档）：

11. sepolia replace 集成
12. README / skill / ROADMAP 更新
13. `pytest` 全绿
