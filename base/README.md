# Base LP 自动化方案

Uniswap V3 ETH/USDC 0.05% pool on Base，全自动 rate-limited re-mint + Telegram 通知。

## 文件清单

| 文件 | 用途 |
|---|---|
| `architecture.html` | **主文档** — 架构图 / 机器配置 / 服务清单 / 部署步骤 / 代码骨架 / 运维 runbook |
| `v3-lp-range-strategy-base.html` | 策略数学推导（ranges, vol-adaptive, 收益模型） |

## 核心数据

| 项 | 值 |
|---|---|
| 仓位 | 2 ETH + 4222 USDC（$8444） |
| Pool | Uniswap V3 ETH/USDC 0.05% on Base |
| 初始 range | ±5% (中心 = 当前 ETH 价) |
| Re-mint 频率上限 | 2 次 / 天 (hard rate limit) |
| Pause 阈值 | σ_14d > 10% → 全转 Aave USDC (Base) |
| 期望年化净 APR | 25% (中位数) |
| 期望年化收益 | $2100 (中位数) |

## 实施时间线

- **Week 1**: 写 scraper / strategy / alert rules，本地 dry-run
- **Week 2**: Sepolia E2E 测试，跑通完整 cycle 含 out-of-range 触发
- **Week 3+**: Base 上线，policy.json caps 紧锁，5 天观察后放宽

详见 `architecture.html`。
