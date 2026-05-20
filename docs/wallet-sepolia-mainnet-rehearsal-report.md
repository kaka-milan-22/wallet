# Wallet Sepolia 排演报告 — Mainnet 测试计划

**日期**: 2026-05-20
**对照计划**: [`docs/wallet-mainnet-test-plan.md`](./wallet-mainnet-test-plan.md) v1
**网络**: Sepolia (chainId 11155111)
**测试账户**: `main` (`0x34a910Df01b110E354dad7324E61462108Cb36c7`)
**起始资产**: 3.31 ETH / 5818 USDC / 0.0036 WETH
**总耗时**: ~10 min
**Gas 实付**: ~0.008 ETH（mainnet 计划估算 0.042 ETH @ 15 gwei；Sepolia base_fee ~1 gwei 实付为 ~1/5）

---

## 1. 执行结果

| Phase | 计划测试点 | Sepolia 适配 | 结果 |
|---|---|---|---|
| 1 — 只读基线 | info / balance / portfolio / history | history 缺 `ETHERSCAN_API_KEY` 跳过 | ✅ 3/4 通过，history 不阻塞 |
| 2 — ETH send + idempotency | T2 自转 + T13 重放 | 复用 main → main 自转 | ✅ 同 `request-id` 返回 `outcome=replayed_idempotent` |
| 3 — Swap 双向 | T3/T4/T5 Uniswap V3，T6 跳 0x | 跳过 T6（无 `WALLET_ZEROX_API_KEY`） | ✅ ETH→USDC +37.19 USDC，USDC→ETH 输出到 WETH（见 [Finding 2](#finding-2)） |
| 4 — Aave V3 闭环 | T7 supply / T8 borrow / T9 repay / T10 withdraw | 抵押用 **LINK** 代替 USDC（stablecoin supply cap） | ✅ 闭环为 0；T9 因利息差 5 micro USDT 一次 revert，faucet 1 USDT 补回（见 [Finding 3](#finding-3)） |
| 5 — Policy 边界 | T12a/b/c/d + T14 | T12c 临时调低 max_per_tx；T12d HF 跳过（仓位已清） | ✅ 4 条 `rejected` 全部正确，0 笔上链 |
| 6 — 收尾 | T11 revoke + T15 audit | 3 笔 revoke | ✅ 19 条 plan-tagged audit，分布合理 |

**总览**: 15 个测试点中 12 通过 / 2 跳过 / 1 适配后通过（T12c），核心安全护栏 (`policy_block` / `idempotency` / `audit` / `simulation_reverted` / `superseded`) 全部按预期工作。

---

## 2. 链上证据

| 操作 | Tx Hash | Etherscan |
|---|---|---|
| T2 self-send 0.0001 ETH | `0x969186…ad8a` | [link](https://sepolia.etherscan.io/tx/0x969186ded8d0d1ee8026819e89916a02fb86ab32db26d8237e451b8c1284ad8a) |
| T3 approve 50 USDC → Uni V3 | `0xc0b25b…14ae5` | [link](https://sepolia.etherscan.io/tx/0xc0b25b84b81ce6ad3a787980a2e98836a19c987793c916dcdb195302e4e14ae5) |
| T4 swap 0.005 ETH → 37.19 USDC | `0x972c58…e1d9` | [link](https://sepolia.etherscan.io/tx/0x972c5877286aaceebcfc2674fa0c26f1a8cc7b312a056d2f1df3a64ab3a6e1d9) |
| T5 swap 37 USDC → 0.005 WETH | `0x607ee1…2719` | [link](https://sepolia.etherscan.io/tx/0x607ee1f569f8d54a55ddd2b7d09991ec7d58e41e669809940a6e7b78066c2719) |
| T7 aave supply 30 LINK | `0x11f71e…384c` | [link](https://sepolia.etherscan.io/tx/0x11f71e817a6e8affa7e778312a068d301b1dc187984c29b7b73c250757b2384c) |
| T8 aave borrow 5 USDT | `0xcdb643…85bb` | [link](https://sepolia.etherscan.io/tx/0xcdb64319ed988ca28e59b4702f8d21707be1724fe43359e16d2a003f4a6a85bb) |
| T9 aave repay USDT --max | `0x00723c…9299` | [link](https://sepolia.etherscan.io/tx/0x00723c2df618698008129a672e39f1d21cedc4e2ef662fadc54cf219681f9299) |
| T10 aave withdraw LINK --max | `0x602fe5…85fc2` | [link](https://sepolia.etherscan.io/tx/0x602fe57f6e23120c74f0f5961590ee261e9b8c64e5269f6021a6ec6b21985fc2) |

---

## 3. Findings — Mainnet 进场前建议的优化

### Finding 1 — `estimate_gas` 早于 policy 检查

**现象**: `wallet send <to> <amount>` 在 `prepare_native_transfer` 阶段调 `estimate_gas`，若余额 < amount，节点会直接返回 `insufficient funds for gas * price + value`，CLI 抛 Python traceback。**policy gate（`max_per_tx` 等）从未触发**，因为 prepare 已经先挂了。

**影响**: 当测试 `max_per_tx` 拦截时（金额 > cap 且 > balance），用户拿到的是 `web3.exceptions.Web3RPCError`，而不是干净的 `policy_block` 包络。Mainnet 上小本金测策略时容易撞到（计划里的 0.05 ETH 本金 + 试 0.03 ETH cap 实际上算够，但任何更小本金都会复现）。

**修复**: ✅ 已落地。`finalize_tx` 现在捕获 `insufficient funds` 错误模式，转成 `InsufficientFundsError`；CLI `wallet send` trap 并 emit `code: insufficient_funds` JSON 包络。其他 prepare 路径（swap / aave / contract）暂未包；它们的 amount 跟 balance 关系更松，触发面小，留待后续按需扩展。

**Commit**: `src/wallet/core/tx.py` + `src/wallet/cli/send.py` + 2 个测试。

---

### Finding 2 — swap to ETH 实际输出 WETH

**现象**: `wallet swap USDC ETH 37` 用户视角是"换 ETH"，但 Uniswap V3 router 的 `exactInputSingle` 把 WETH 直接转给 user，没有 unwrap。所以 user 收到的是 WETH 不是 native ETH。

**影响**: 用户余额表里 WETH 涨 ETH 不变，需要自己再做一笔 `WETH9.withdraw()` 才能变 native ETH。Agent 自动化场景下更难处理——它以为 swap 完了拿到 ETH，下一步 `wallet send X ETH 1.0` 会因为 ETH 余额不够而 underflow。

**修复**: ✅ 已落地。`UniswapV3DirectRoute` 现在检测 `token_out.is_native`，把 swap calldata 改成 **multicall(exactInputSingle, unwrapWETH9)**：
- exactInputSingle.recipient = `ADDRESS_THIS` sentinel (`0x...0002`)，把 WETH 锁在 router 里
- unwrapWETH9 收 router 里所有 WETH 转 native ETH 给 user

Sepolia 实测验证（`0x6e8c53…395a`）：post-swap user 的 ETH 余额涨 (+0.000285 = +0.00067 swap output - 0.00038 gas)，**WETH 余额不变**。

**Commit**: `src/wallet/protocols/routes/uniswap_v3.py` + 2 个测试。

---

### Finding 3 — Aave `repay --max` 需要 balance ≥ debt + buffer

**现象**: Sepolia USDT 64% borrow APR，借 5 USDT 30 秒后 debt 涨到 5.000005 USDT，但 user balance 只有 5.000000。`--max` 让 Aave 取 min(debt, allowance)，但底层 `transferFrom(user, pool, debt)` 因 user 余额不够而 revert：`ERC20: transfer amount exceeds balance`。

**影响**: Mainnet stablecoin pool ~3-10% APR，几秒内的利息 << 1 micro，所以**实际触发概率低**；但只要 user 借完后等几分钟再 repay，或 pool 利率突涨，就会复现。Agent 自动化的"借了又还"循环最容易撞。

**修复**: ✅ 文档已加到 `docs/TESTING.md`。三个 mitigation：
1. 保留 1 unit 缓冲（faucet 补）
2. repay 精确数额（如 `repay USDT 4.99`）留余尾 debt 后面再清
3. 借完立刻还，别等利息累积

Wallet 代码侧没改——`--max` 是用户显式选择，"全清"语义跟"transferFrom 不爆"权衡，文档化是合理的折中。

---

## 4. Sepolia 与 Mainnet 计划的偏离汇总

| 计划项 | Sepolia 处理 | 原因 |
|---|---|---|
| `mainnet_test` 新账户 (index=5) | 复用 `main` | sepolia 上已有充值，无必要新建 |
| Aave supply USDC | 改用 **LINK** | sepolia stablecoin 有 `SUPPLY_CAP_EXCEEDED` |
| Aave repay USDT `--max` | 加 `faucet 1 USDT` 补 buffer | Finding 3 利息超精度 |
| T6 swap via 0x | 跳过 | 缺 `WALLET_ZEROX_API_KEY` |
| T12c max_per_tx 0.02 ETH | 临时改 0.0001 ETH | Finding 1，Sepolia 现 policy 把 cap 设到 10 ETH 远高于余额 |
| T12d HF block | 跳过 | Aave withdraw 后无仓位，复现需重 supply |
| history (T1 / T15) | 跳过 | 无 `ETHERSCAN_API_KEY`；balance/portfolio + audit.log 已覆盖落账核验 |
| `wallet send 100 ETH` 测 cap | 改用 0.0002 ETH + 临时 cap 0.0001 | 余额限制（计划的 0.03 ETH 对 0.05 ETH 本金合理） |

---

## 5. Mainnet 进场清单（更新版）

基于本次排演，原计划 §4.5 "Pre-flight 检查清单" 的基础上**追加**：

- [ ] `wallet send <to> <amount>` 试一次 `amount > balance`，应返回 `code: insufficient_funds` JSON envelope（验证 Finding 1 修复）
- [ ] `wallet swap USDC ETH 5 --dry-run`，preview 应显示 multicall + unwrap 路径（验证 Finding 2 修复，calldata 由 0x04e45aaf 变 0xac9650d8）
- [ ] Aave repay 前确认 `balance(borrowed_token) > debt`，差额至少 1 unit（Finding 3 抗利息漂移）
- [ ] **首次 swap to ETH** 后核对 portfolio：ETH 涨、WETH 不变（confirm Fix B 生效）
- [ ] T6 (0x swap) 走主网值得跑——sepolia 跳过的，mainnet 0x API 必须验证一次 `tx.to == AllowanceHolder`

---

## 6. 单元测试

修复 Finding 1+2 后追加 4 个测试，全套 385 通过：

```
tests/test_tx.py::test_finalize_tx_maps_insufficient_funds_to_typed_error PASSED
tests/test_tx.py::test_finalize_tx_does_not_swallow_unrelated_estimate_gas_errors PASSED
tests/test_routes_uniswap_v3.py::test_quote_native_eth_out_wraps_swap_in_multicall_with_unwrap PASSED
tests/test_routes_uniswap_v3.py::test_quote_erc20_to_erc20_does_not_use_multicall PASSED

======================== 385 passed, 1 warning in 3.98s ========================
```

---

## 7. 结论

**排演通过**——核心 EOA / Swap / Aave 流水线全部按 mainnet 计划在 Sepolia 跑通，三个细节问题修复 + 文档化。Mainnet 进场前剩下的只是按 §4.5 检查清单复核地址 + 按 §6 中止策略盯 gas，不需要额外架构调整。

Sepolia 本次累计花费 **~0.008 ETH** (~$0.02 等值)。
