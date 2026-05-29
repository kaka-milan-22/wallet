# Wallet Mainnet 测试计划

**版本**: 2026-05-20 v1
**目标钱包**: `~/claude/wallet` (本仓库)
**目标链**: Ethereum mainnet (chain_id 1)
**ETH 行情**: $2100 (估算基准)
**Gas 基准**: 15 gwei (normal hour) — 起跑前必须用 `eth_gasPrice` 复核

---

## 1. 顶层目标

在 mainnet 用最小本金把 wallet 已实现的全部功能跑一遍真实签名 + 真实广播，覆盖三大类：

- **transfer**: ETH send、ERC-20 send、approve/revoke
- **swap**: Uniswap V3 direct 双向、0x aggregator (optional)
- **lending**: Aave V3 supply / borrow / repay / withdraw 完整闭环

同时验证四层安全护栏在 mainnet 也按 Sepolia 一样工作：policy 拦截、idempotency 重放、audit 落盘、FIFO 不落磁盘明文。

**非目标**: 不测 Ledger（未实现）；不测 Lido / Yearn / Pendle（未实现）；不在 mainnet 上压测性能。

---

## 2. 测试覆盖矩阵

| # | 功能 | 命令 | 路径 | 实测点 |
|---|---|---|---|---|
| T1 | 读 balance / portfolio / info | `wallet balance` etc | 只读 | 多 token 聚合、EIP-55 地址、price API（如启用） |
| T2 | ETH send 自转 | `wallet send` | EOA | 21k gas 基线 + history 落账 |
| T3 | ERC-20 approve | `wallet approve set USDC <router>` | ERC-20 | `policy.contract_allowlist` 命中 |
| T4 | Swap ETH→USDC (Uni V3) | `wallet swap ETH USDC --via uniswap-v3` | UniV3 SwapRouter02 | 直连 DEX、slippage_bps、quoter 预估 |
| T5 | Swap USDC→ETH (Uni V3) | `wallet swap USDC ETH --via uniswap-v3` | UniV3 + 预先 approve | 反向、需 ERC-20 input |
| T6 | Swap via 0x (可选) | `wallet swap ETH USDC --via 0x` | 0x AllowanceHolder | 需 `WALLET_ZEROX_API_KEY`；spender pinning |
| T7 | Aave supply USDC | `wallet aave supply USDC 30` | Aave Pool | `min_health_factor` policy、aToken mint |
| T8 | Aave borrow USDT | `wallet aave borrow USDT 5` | Aave Pool | HF 估算、variable rate |
| T9 | Aave repay USDT | `wallet aave repay USDT --max` | Aave Pool + approve | `--max` 含利息精确还清 |
| T10 | Aave withdraw USDC | `wallet aave withdraw USDC --max` | Aave Pool | HF 闸门、aToken burn |
| T11 | approve revoke | `wallet approve revoke USDC <spender>` | ERC-20 | 收尾、降低后续风险 |
| T12 | policy block | 故意试越界 | dry-run / policy | `recipient_allowlist` / `deny_unlimited_approve` / `min_health_factor` |
| T13 | idempotency replay | 同 `--request-id` 两次 send | EOA | 第二次返回缓存 tx_hash，不重复广播 |
| T14 | simulation_reverted | 取多于持仓的 USDC withdraw | dry-run | Aave HF 革命前提示 |
| T15 | audit log | 全程读 `~/.wallet/audit.log` | 文件 | 每次 broadcast / rejected 都有条目 |

T12-T15 几乎不烧 gas（T13 烧 1 笔 send）；T1 全免费；T2-T11 是主要 gas 消耗。

---

## 3. 费用估算（@ ETH $2100, 15 gwei）

| 阶段 | gas (units) | ETH | USD |
|---|---|---|---|
| T2 send ETH (自转 ×1) | 21,000 | 0.000315 | $0.66 |
| T3 approve USDC → Uni V3 router | 46,000 | 0.000690 | $1.45 |
| T4 swap ETH→USDC | 160,000 | 0.002400 | $5.04 |
| T5 swap USDC→ETH | 180,000 | 0.002700 | $5.67 |
| T6 swap via 0x (optional) | 220,000 | 0.003300 | $6.93 |
| T3' approve USDC → Aave Pool | 46,000 | 0.000690 | $1.45 |
| T7 Aave supply USDC | 270,000 | 0.004050 | $8.51 |
| T8 Aave borrow USDT | 380,000 | 0.005700 | $11.97 |
| T3'' approve USDT → Aave Pool | 46,000 | 0.000690 | $1.45 |
| T9 Aave repay USDT --max | 170,000 | 0.002550 | $5.36 |
| T10 Aave withdraw USDC --max | 280,000 | 0.004200 | $8.82 |
| T11 revoke approve (×2) | 60,000 | 0.000900 | $1.89 |
| T13 idempotency 重放 (额外 send) | 21,000 | 0.000315 | $0.66 |
| **小计（含 0x）** | **1,900k** | **0.0285** | **~$59.8** |
| **buffer 50%（gas 抖动 / 重试）** | — | 0.014 | ~$30 |
| **总 gas 预算** | — | **~0.042 ETH** | **~$90** |

**本金部分（基本可回收）**:

- swap 中转：`0.01 ETH` 拿去 swap → USDC → ETH，回收 ~99% (slippage 0.3% × 2 + LP 0.05% × 2)
- Aave 抵押：~`$30 USDC`（由上一步 swap 出来），全额可取回
- Aave 借出：`$5 USDT`，repay --max 含利息（1 小时持仓利息可忽略 < $0.01）

**结论**：

- **保守档**: 充值 **0.05 ETH ≈ $105**。覆盖正常 gas + buffer，预留少量本金；测完剩 ~$10-15（gas 烧掉）。
- **舒服档**: 充值 **0.08 ETH ≈ $168**。容忍 gas 抖到 30 gwei、容忍一次完整重跑；测完剩 ~$50。
- **触发中止线**: 实时 gas 超过 **40 gwei** 暂停，等回落；超过 **80 gwei** 直接放弃当日测试。

---

## 4. 前置准备（不烧 gas，必须全部完成才进 Phase 1）

### 4.1 环境变量

```sh
# 用 Alchemy/Infura 自己的 key；公共 RPC 也行但偶尔丢请求
export WALLET_ETH_RPC=https://eth.drpc.org
# 或者 https://ethereum.publicnode.com
# 或者 Alchemy: https://eth-mainnet.g.alchemy.com/v2/<KEY>

export ETHERSCAN_API_KEY=...                # 已有；wallet history 用
export WALLET_ZEROX_API_KEY=...             # 可选；做 T6 才需要
```

### 4.2 注册 ethereum chain

在 `~/Library/Application Support/wallet/chains.json` 写入（如已存在 `ethereum` 条目就跳过）：

```jsonc
{
  "ethereum": {
    "name": "ethereum",
    "chain_id": 1,
    "rpc_url": "https://eth.drpc.org",
    "explorer_api_url": "https://api.etherscan.io/v2/api",
    "explorer_tx_url": "https://etherscan.io/tx/{tx}",
    "native_symbol": "ETH",
    "builtin_tokens": {
      "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
      "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
      "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
      "DAI":  "0x6B175474E89094C44Da98b954EedeAC495271d0F"
    },
    "protocols": {
      "uniswap_v3": {
        "swap_router_v2": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "quoter_v2":      "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "factory":        "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        "allowance_holder": "0x0000000000001fF3684f28c67538d4D072C22734"
      },
      "aave_v3": {
        "pool":          "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
        "data_provider": "0x41393e5e337606dc3821075Af65AeE84D7688CBD",
        "oracle":        "0x54586bE62E3c3580375aE3723C145253060Ca0C2"
      }
    }
  }
}
```

验证：

```sh
uv run wallet chain list                              # 应有 ★ sepolia + ethereum
uv run wallet chain show ethereum                     # 完整 dump
uv run wallet info --chain ethereum                   # 不切默认，先 peek
```

### 4.3 测试账户

复用现有 `main` 还是新建专用 `mainnet_test` 二选一。**推荐新建**，便于审计 audit log：

```sh
uv run wallet account derive main --index 5 --as mainnet_test
uv run wallet account show mainnet_test               # 拿到地址
# 复制地址，**人工**用 CEX 或主钱包打 0.05 ETH 到此地址
# 等 12 个 confirmation 后:
uv run wallet balance --account mainnet_test --chain ethereum
```

### 4.4 mainnet 专用 policy.json

**关键**：sepolia 那份 allowlist 和 mainnet 地址完全不一样，直接复用一定 `policy_block`。把当前 `~/.wallet/policy.json` 备份后改成：

```jsonc
{
  "max_per_tx": {
    "ETH":  "0.02",
    "USDC": "50",
    "USDT": "50"
  },
  "max_per_day": {
    "ETH":  "0.05",
    "USDC": "200",
    "USDT": "200"
  },
  "recipient_allowlist": [
    "<mainnet_test 地址自身,自转用>"
  ],
  "contract_allowlist": [
    "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",  // Uniswap V3 SwapRouter02
    "0x0000000000001fF3684f28c67538d4D072C22734",  // 0x AllowanceHolder
    "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"   // Aave V3 Pool
  ],
  "deny_unlimited_approve": true,
  "first_send_warn": true,
  "min_health_factor": 1.5,
  "sentinel_blocklist": []
}
```

```sh
# 备份
cp ~/.wallet/policy.json ~/.wallet/policy.sepolia.json.bak
# 写新版后:
uv run wallet policy lint                             # 应无 warning
uv run wallet policy show                             # 复核
```

### 4.5 Pre-flight 检查清单

- [ ] `uv run wallet info --chain ethereum` 显示 chain_id=1，RPC 通
- [ ] `uv run wallet balance --account mainnet_test --chain ethereum` 显示 0.05 ETH
- [ ] `uv run wallet policy show` allowlist 含 3 个合约
- [ ] `alice scan "$(uv run wallet info | awk '/state file/ {print $3,$4,$5}')"` 报 0 secrets（TTY only）
- [ ] gas 现价 `curl -s $WALLET_ETH_RPC -X POST -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"eth_gasPrice","params":[]}'`，转 gwei 后 ≤ 25
- [ ] `ps aux | grep wallet` 不含明文 mnemonic
- [ ] 默认 chain 仍是 sepolia（每步用 `--chain ethereum` 显式指定，避免误切）

任一条不通过 → 停在准备阶段，不进 Phase 1。

---

## 5. 阶段化测试步骤

每阶段独立，阶段间可暂停，下阶段开始前必须确认上阶段全部 PASS。命令统一带 `--chain ethereum --account mainnet_test`。下文为简洁省略，**实际跑时不能省**。

每个 broadcast 操作必须：

1. 先无 `--broadcast` 跑一次 dry-run，确认 preview 数字合理
2. 再加 `--broadcast --request-id <unique>`，看 `--yes` 之前的人工 confirm
3. 落账后核验 `wallet history` + `~/.wallet/audit.log`

### Phase 1 — 只读基线（免费）

```sh
uv run wallet info --chain ethereum
uv run wallet balance --account mainnet_test --chain ethereum
uv run wallet portfolio --account mainnet_test --chain ethereum
uv run wallet history --account mainnet_test --chain ethereum
```

**PASS 标准**: 全部命令 0 退出码，余额匹配充值金额，history 显示充值 tx。

### Phase 2 — ETH 自转 (T2, T13)

```sh
# T2: 自转 0.0001 ETH dry-run
uv run wallet send mainnet_test 0.0001 \
    --chain ethereum --account mainnet_test
# 看 preview, 估算 gas, 应有 first_send_warn (因为 receipient_allowlist 已含自己?
# 若 allowlist 命中则无 warn — 这正是预期)

# 真广播
RID=test-$(date +%s)-self-send
uv run wallet send mainnet_test 0.0001 \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id "$RID"

# T13: 同 request-id 再发一次 — 应直接返回缓存 tx_hash，不上链
uv run wallet send mainnet_test 0.0001 \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id "$RID"
# 预期: 同一个 tx_hash 返回，nonce 不变；audit.log 多一条 outcome=replay
```

**PASS 标准**: tx 上链；第二次调用返回相同 tx_hash；`audit.log` 含两条记录（broadcast / replay）。

### Phase 3 — Swap 双向（T3, T4, T5, optionally T6）

#### T3 + T4：approve + swap ETH → USDC

```sh
# approve dry-run
uv run wallet approve set USDC 0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45 50 \
    --chain ethereum --account mainnet_test

# approve broadcast (cap 给 50 USDC，避免 unlimited)
uv run wallet approve set USDC 0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45 50 \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id approve-usdc-uniswap-$(date +%s)

# swap dry-run
uv run wallet swap ETH USDC 0.005 \
    --chain ethereum --account mainnet_test \
    --via uniswap-v3 --slippage-bps 50

# swap broadcast
uv run wallet swap ETH USDC 0.005 \
    --chain ethereum --account mainnet_test \
    --via uniswap-v3 --slippage-bps 50 \
    --broadcast --yes --request-id swap-eth-usdc-$(date +%s)
```

**PASS 标准**: dry-run 报价 ≈ $10.5 USDC (0.005 × $2100)，实际到账 ≥ `amount_out_min`；`wallet portfolio` 显示 USDC 余额非零。

#### T5：swap USDC → ETH

```sh
# 先把上一步的 USDC 大部分 swap 回 ETH（留 ~$30 给 Aave）
USDC_AMT=$(uv run --quiet wallet --json balance --token USDC \
              --chain ethereum --account mainnet_test \
              | jq -r '.data.amount')
SWAP_BACK=$(echo "$USDC_AMT - 30" | bc)

# dry-run
uv run wallet swap USDC ETH "$SWAP_BACK" \
    --chain ethereum --account mainnet_test \
    --via uniswap-v3 --slippage-bps 50

# broadcast
uv run wallet swap USDC ETH "$SWAP_BACK" \
    --chain ethereum --account mainnet_test \
    --via uniswap-v3 --slippage-bps 50 \
    --broadcast --yes --request-id swap-usdc-eth-$(date +%s)
```

**PASS 标准**: ETH 余额回升约对应金额（扣 gas + slippage）；USDC 余额回到 ~$30；audit.log 有 swap 条目。

#### T6（可选）：swap via 0x

仅当 `WALLET_ZEROX_API_KEY` 已设置：

```sh
uv run wallet swap ETH USDC 0.002 \
    --chain ethereum --account mainnet_test \
    --via 0x --slippage-bps 50
# 然后 broadcast 同上模式。
# 验证点: tx.to == 0x0000000000001fF3684f28c67538d4D072C22734 (chain-pinned)
```

### Phase 4 — Aave V3 完整闭环（T3', T7, T8, T3'', T9, T10）

```sh
# 4.1: approve USDC → Aave Pool
uv run wallet approve set USDC 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2 30 \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id approve-usdc-aave-$(date +%s)

# 4.2: 读取 Aave 利率，记录基线
uv run wallet aave rates --chain ethereum --token USDC
uv run wallet aave rates --chain ethereum --token USDT

# 4.3: supply 30 USDC
uv run wallet aave supply USDC 30 \
    --chain ethereum --account mainnet_test
# dry-run 看 HF 预估

uv run wallet aave supply USDC 30 \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id aave-supply-$(date +%s)

# 4.4: 查持仓
uv run wallet aave positions --chain ethereum --account mainnet_test
# 应显示 supply 30 USDC, HF 极高 (无 debt)

# 4.5: borrow 5 USDT (变量利率)
uv run wallet aave borrow USDT 5 \
    --chain ethereum --account mainnet_test
# dry-run, 估算 HF (应 ≥ 5)

uv run wallet aave borrow USDT 5 \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id aave-borrow-$(date +%s)

# 4.6: 等 5 分钟（让利息累积可观察）
uv run wallet aave positions --chain ethereum --account mainnet_test
# variable debt 应略高于 5 USDT

# 4.7: approve USDT → Aave Pool，repay --max
uv run wallet approve set USDT 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2 10 \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id approve-usdt-aave-$(date +%s)

uv run wallet aave repay USDT --max \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id aave-repay-$(date +%s)

# 4.8: withdraw --max
uv run wallet aave withdraw USDC --max \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id aave-withdraw-$(date +%s)

# 4.9: 验证
uv run wallet aave positions --chain ethereum --account mainnet_test
# 预期: 0 supply / 0 borrow
uv run wallet balance --account mainnet_test --chain ethereum --token USDC
# 应回到 ~30 USDC (略少，因为借出的 USDT 利息算入 repay)
```

**PASS 标准**: positions 闭环为 0；balance 大致守恒；audit.log 4 条 broadcast。

### Phase 5 — Policy / 边界（T12, T14；几乎不烧 gas）

全部用 dry-run，验证 policy 提前拦截：

```sh
# T12a: recipient_allowlist 拦截 — 发往未允许地址
uv run wallet send 0x000000000000000000000000000000000000dEaD 0.0001 \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id test-block-recipient
# 预期: error=policy_block, reason 含 "recipient-not-in-allowlist"

# T12b: unlimited approve 拦截
uv run wallet approve set USDC 0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45 \
    --unlimited \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id test-block-unlimited
# 预期: error=policy_block, reason 含 "deny-unlimited-approve"

# T12c: max_per_tx 超额
uv run wallet send mainnet_test 0.03 \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id test-block-cap
# 预期: error=policy_block, reason 含 "exceeds max_per_tx"

# T12d: min_health_factor 拦截 (需要先有少量持仓)
# 在 Phase 4 持仓还没 withdraw 时执行；如已 withdraw 这步跳过
# 借一个会把 HF 压到 < 1.5 的金额
uv run wallet aave borrow USDC 25 \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id test-block-hf
# 预期: error=policy_block, reason 含 "min_health_factor"

# T14: simulation_reverted
uv run wallet aave withdraw USDC 1000 \
    --chain ethereum --account mainnet_test
# 预期: error=simulation_reverted, 不发交易
```

**PASS 标准**: 全部返回非零退出码 + 对应 error code；**没有一笔上链**；audit.log 多 4 条 outcome=rejected。

### Phase 6 — 收尾（T11, T15）

```sh
# T11: 把所有 approve 收掉
uv run wallet approve revoke USDC 0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45 \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id revoke-usdc-uniswap-$(date +%s)

uv run wallet approve revoke USDC 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2 \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id revoke-usdc-aave-$(date +%s)

uv run wallet approve revoke USDT 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2 \
    --chain ethereum --account mainnet_test \
    --broadcast --yes --request-id revoke-usdt-aave-$(date +%s)

# T15: audit log 整体检查
wc -l ~/.wallet/audit.log
# 预期: T2(2) + T3(1) + T4(1) + T5(1) + [T6 1] + T3'(1) + T7(1) + T8(1) + T3''(1) + T9(1) + T10(1) + T12(4 rejected) + T14(1 sim) + T11(3) ≈ 19-20 条

# 把剩余 USDC swap 回 ETH，然后整体回收到主钱包 (人工)
uv run wallet portfolio --account mainnet_test --chain ethereum

# 还原 policy.json
cp ~/.wallet/policy.sepolia.json.bak ~/.wallet/policy.json
uv run wallet policy show
```

---

## 6. 中止 / 回退策略

| 触发条件 | 处置 |
|---|---|
| RPC 5xx / timeout 连续 3 次 | 切到备用 RPC (`publicnode` / `onfinality`)；如全失败暂停 30 分钟 |
| gas > 40 gwei | 暂停，等 `eth_gasPrice` 回落到 25 gwei 以下再续 |
| gas > 80 gwei | 当日测试放弃，剩余 ETH 提回主钱包 |
| 任意 broadcast 后 5 分钟未上链 | 用 Etherscan 查 nonce 状态；如 stuck → 跳过 Phase 5 stuck-tx 测试（功能未实现），直接等 mempool 出清；最坏情况手工发同 nonce 高 gas self-send 取消 |
| swap 实际滑点超过 `slippage-bps` 上限 | 链上 revert，自动退款；audit 应有 simulation_reverted 或 rpc_error |
| Aave HF 异常下降（非预期） | 立即 `aave repay --max`；如果 USDT 余额不够借入 USDT 应急 |
| Policy 误拦合法操作 | 不绕过；先 stop，回头核对 allowlist；**永远不要用 `--policy-bypass`** |

---

## 7. 下次执行流程（给我的指令模版）

下次你只需说类似这样的话，我就能照此计划开跑：

```
按 ~/work/doc/wallet-mainnet-test-plan.md 测 mainnet
- 测试账户：mainnet_test (已存在 / 请新建 index=5)
- 已充值: 0.05 ETH 到 <地址>
- 跳过的 phase: <none / T6 / T12d, etc>
- gas 上限: 30 gwei (超过暂停)
- 完成后写报告到 ~/work/doc/wallet-mainnet-test-report-<日期>.md
```

我会按 Phase 0 → 6 顺序执行，每个 broadcast 前停下来给你看 dry-run preview，你确认后我再加 `--broadcast`。每个 Phase 结束打一个 checkpoint，等你 `ok` 再进下一阶段。任何中止条件触发我自动停在原地并报告状态。

---

## 8. 已知风险摘要（来自 ROADMAP）

- **无 Ledger**: 私钥仍是 alice（AnB）里的 mnemonic，master key 在 bob 内。测试用 0.05 ETH 在风险阈以下，可接受。签名前确认 bob 已 serve 且 unlocked。
- **公共 RPC**: 不走 Flashbots，sandwich 风险存在但金额小（每笔 swap < $15）影响可忽略。
- **policy verify 未实现**: allowlist 地址是手工抄的，**进 Phase 1 前必须用 Etherscan 二次核对** 每个合约地址的合约名（Uniswap V3 SwapRouter02 / Aave Pool / AllowanceHolder）。
- **stuck-tx recovery 未实现**: 一旦 mempool 拥堵，没有内置 cancel/speedup，只能手工。

---

## 9. 附录 A — 关键地址核对表

测试前请在 [etherscan.io](https://etherscan.io) 查验每一行：

| 用途 | 地址 | Etherscan 合约名应为 |
|---|---|---|
| Uniswap V3 SwapRouter02 | `0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45` | `SwapRouter02` |
| Uniswap V3 QuoterV2 | `0x61fFE014bA17989E743c5F6cB21bF9697530B21e` | `QuoterV2` |
| Uniswap V3 Factory | `0x1F98431c8aD98523631AE4a59f267346ea31F984` | `UniswapV3Factory` |
| 0x AllowanceHolder | `0x0000000000001fF3684f28c67538d4D072C22734` | `AllowanceHolder` |
| Aave V3 Pool | `0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2` | `InitializableImmutableAdminUpgradeabilityProxy` (代理), impl 应为 `Pool` |
| Aave V3 DataProvider | `0x41393e5e337606dc3821075Af65AeE84D7688CBD` | `AaveProtocolDataProvider` |
| Aave V3 Oracle | `0x54586bE62E3c3580375aE3723C145253060Ca0C2` | `AaveOracle` |
| USDC | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` | `FiatTokenProxy` (impl `FiatTokenV2_2`) |
| USDT | `0xdAC17F958D2ee523a2206206994597C13D831ec7` | `TetherToken` |
| WETH | `0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2` | `WETH9` |

任一地址 Etherscan 上对应合约名对不上 → **停**，不要继续。
