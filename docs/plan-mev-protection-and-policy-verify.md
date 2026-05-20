# Plan: MEV-protected broadcast 强制 + policy verify

**Status**: draft — pending review before implementation
**Estimated work**: 1.5 days total（半天 + 1 天）
**Roadmap reference**: ROADMAP.md Tier 2 "require_private_rpc policy enforcement" + "wallet policy verify command"

---

## Context

当前 mainnet 路径上有两条"软约定"，依赖操作员手工配置正确、且 **持续保持正确**。任何漂移（env 重置、policy.json typo、Etherscan 上原本合约被替换）都不会在 broadcast 前被拦截，签名后才暴露：

1. **Broadcast RPC**：用户契约是 `export WALLET_ETH_RPC=https://rpc.flashbots.net`。一次 shell 重启没 export 就回退到公共 mempool，sandwich bot 立刻拿走 swap 价差。代码层面完全没感知。
2. **policy.json 的 allowlist**：合约地址是手抄的。一行 typo / 一个钓鱼合约伪装地址（差一个字符）= 永久失资。当前 `wallet policy lint` 只警告空字段，不验证地址身份。

两个 gap 都属于 **execution-level safety**（不是 strategy 逻辑）——和 `deny_unlimited_approve` / `min_health_factor` 是同类，自然归 `core/policy.py` 管。

本 plan 把两条约定升级成 wallet 自检的硬门——broadcast 前自动验证，不依赖操作员的肌肉记忆。

---

## 这两项改进解决了哪些问题

### Change 1（MEV-protected broadcast）解决：

| 问题 | 当前后果 | 改进后 |
|---|---|---|
| env 漂移：`WALLET_ETH_RPC` 不小心指回 publicnode / Alchemy 公共端点 | 所有 swap 暴露在公共 mempool → sandwich attack 吃掉 0.5%-3% 价差 | broadcast 前 hostname 检查不命中 allowlist → `policy_block`，根本不发 |
| 误用 RPC：从 sepolia 切到 mainnet 忘改 broadcast URL | 公共 mempool 广播 | 同上，强制 hostname 校验 |
| 单 endpoint 故障：Flashbots 临时挂了 | 没 fallback，操作员手动改 env | `mev_protected_rpc_allowlist` 是数组，操作员预填多个，wallet 任选其一即可（多 RPC fallback 单独做） |
| 跨链一致性：mainnet 走 Flashbots，sepolia 不需要 | 全靠操作员记得切 | policy 字段 `require_mev_protected_broadcast` 可在不同 policy 文件中分别设置；测试环境 false，生产 true |

### Change 2（`wallet policy verify`）解决：

| 问题 | 当前后果 | 改进后 |
|---|---|---|
| allowlist 地址 typo（少 / 多一位字符） | 校验和合法时直接走、不合法时 `policy_block` 没解释——operator 不知道是 typo 还是逻辑 bug | 每个 allowlist entry 都被 Etherscan 反向核对合约名；`verify` 命令明确报"地址 0xABC...的合约名是 FooDrainer，不是预期的 SwapRouter02" |
| 钓鱼合约伪装：攻击者诱导操作员把仿冒合约加入 allowlist（差 1-2 个字符） | 仿冒地址通过所有 policy 检查，allowance 流到攻击者 | 强制人工对照合约名 + deployer，仿冒合约的合约名/deployer 对不上 |
| 合约升级：Aave / Uniswap 部署新版本，旧地址被弃用 | 旧 allowlist 仍允许，但实际 swap 失败 / 资金锁死 | `verified_at` 戳超过 30 天自动失效，强制重新验证 |
| 长期信任假设：policy.json 是"一次写完不再看"的文件 | 配置漂移 → 监控失明 | verify 命令给 CI/cron 用，每月跑一次出 0/非 0 退出码 |

### 联合效果：

mainnet 操作链路变成 **fail-closed 三道闸**：

1. RPC URL 不对 → `policy_block` (Change 1)
2. allowlist 合约身份不对 / 过期 → `policy_block` (Change 2)
3. 既有 caps / HF / unlimited / sentinel → `policy_block`

任何一道挂掉，wallet 都自检出来；不依赖操作员记忆。把 ROADMAP "Mainnet readiness gaps" 里 Tier 1 配置项和 Tier 2 两条一起收掉。

---

## Change 1: MEV-protected broadcast 强制

### 数据模型

**`core/config.py:ChainConfig`** 加 1 个可选字段：

```python
broadcast_rpc_url: str | None = None
"""Override RPC for `eth_sendRawTransaction` only. When None, falls back to
rpc_url. Used to route broadcasts through MEV-protected endpoints (Flashbots
Protect, MEV Blocker, Titan, SecureRPC) while keeping read traffic on a
regular RPC. Reads via these endpoints don't work — Flashbots et al only
expose sendRawTransaction."""
```

**`core/policy.py:Policy`** 加 2 个字段：

```python
require_mev_protected_broadcast: bool = False
"""When True, broadcast RPC hostname must match an entry in
`mev_protected_rpc_allowlist`. Off by default for sepolia compatibility;
mainnet policy template sets True."""

mev_protected_rpc_allowlist: list[str] = Field(default_factory=lambda: [
    "rpc.flashbots.net",
    "rpc.mevblocker.io",
    "rpc.titanbuilder.xyz",
    "api.securerpc.com",
])
"""Hostnames acceptable for broadcast RPC. Compared case-insensitively
against urllib.parse.urlparse(broadcast_url).hostname. Operators with
self-hosted nodes append their own hostname."""
```

### 评估钩入点

`policy.evaluate()` 签名扩展（**轻微 breaking change**，调用方只有 1 处在 `cli/_common.py`）：

```python
def evaluate(
    prepared,
    state: WalletState,
    caller: str,
    *,
    chain: ChainConfig | None = None,   # NEW
    bypass: bool = False,
) -> Decision:
```

新增检查段，放在 sentinel_blocklist 之后、category 分发之前（最高优先级中的最低 — 已知 drainer > 配置错误）：

```python
# --- 1.5. MEV protection (broadcast-side configuration sanity) ---
if policy.require_mev_protected_broadcast and chain is not None:
    broadcast_url = chain.broadcast_rpc_url or chain.rpc_url
    host = urlparse(broadcast_url).hostname or ""
    allowlist = {h.lower() for h in policy.mev_protected_rpc_allowlist}
    if host.lower() not in allowlist:
        return Decision(
            allowed=False,
            reason=f"broadcast-rpc-not-mev-protected:{host}",
            severity="block",
        )
```

### Web3 工厂分离

`core/rpc.py` 当前 `web3(chain)` 返回单实例。新增：

```python
def web3_broadcast(chain: ChainConfig, *, timeout: int = 20) -> Web3:
    """Web3 instance for sendRawTransaction only.

    When chain.broadcast_rpc_url is set, returns a Web3 against that URL;
    otherwise falls back to chain.rpc_url (current behaviour).

    Caller is responsible for using this only for broadcast — calling
    eth_call / eth_getBalance through this instance will fail against
    MEV-protected endpoints.
    """
    url = chain.broadcast_rpc_url or chain.rpc_url
    return Web3(HTTPProvider(url, request_kwargs={"timeout": timeout}))
```

### 调用方修改

`cli/_common.py:confirm_and_broadcast` —— 唯一调用 `evaluate` 的地方。改两处：

1. 传 `chain=chain` 给 `evaluate`
2. 实际 `send_raw_transaction` 用 `web3_broadcast(chain)`，不是当前的 `web3(chain)`

### 默认 policy 模板

`default_policy()` 不动（兼容 sepolia）。新增 `default_mainnet_policy_template()` 返回带 `require_mev_protected_broadcast=True` 的版本，由 `wallet policy init --mainnet` 触发。

### 测试

`tests/test_policy.py` 加：

- `evaluate` 传 `chain=mainnet_chain_with_publicnode_url` + `require_mev_protected_broadcast=True` → `policy_block`
- 同上但 `broadcast_rpc_url=https://rpc.flashbots.net` → `allowed`
- `require_mev_protected_broadcast=False` 时 hostname 不命中也 `allowed`（保留 sepolia 路径）
- `chain=None` 时跳过此检查（向后兼容）
- 自定义 hostname 加入 allowlist 后命中
- `chain.broadcast_rpc_url=None` 时 fallback 到 `chain.rpc_url` 做检查

代码：~150 行 src + ~30 行 tests，工作量 **半天**。

---

## Change 2: `wallet policy verify` 命令

### 数据模型

`Policy` 加 1 个字段：

```python
verified_contracts: dict[str, "VerificationRecord"] = Field(default_factory=dict)
"""Lowercase address -> verification metadata. Populated by
`wallet policy verify`. Compared at evaluate() time against
verification_max_age_days to fail closed when stale."""

verification_max_age_days: int = 30
"""Maximum age in days for verified_contracts entries. After this,
contract_allowlist hits without a fresh verified_at trigger policy_block."""
```

```python
class VerificationRecord(BaseModel):
    verified_at: str          # ISO-8601 UTC
    contract_name: str         # Etherscan-reported name
    is_proxy: bool             # True if proxy contract
    implementation: str | None # impl address if proxy
    deployer: str | None       # original deployer EOA
```

### Etherscan 调用

`services/explorer.py` 加：

```python
def get_contract_source(chain: ChainConfig, address: str) -> dict[str, Any]:
    """Return Etherscan `getsourcecode` result for `address`. Keys of
    interest: ContractName, Proxy, Implementation, CompilerVersion."""
    return _call(chain, {
        "module": "contract",
        "action": "getsourcecode",
        "address": address,
    })

def get_contract_creation(chain: ChainConfig, address: str) -> dict | None:
    """Return deployer + tx hash for `address` via `getcontractcreation`."""
    res = _call(chain, {
        "module": "contract",
        "action": "getcontractcreation",
        "contractaddresses": address,
    })
    return res[0] if res else None
```

复用现有 `_call` / `_api_key()`、不重写客户端。

### 命令实现

新增 `cli/policy.py:verify`：

```python
@app.command("verify")
def verify(
    chain: str = typer.Option("ethereum", "--chain", help="Chain to query Etherscan against"),
    save: bool = typer.Option(False, "--save", help="Persist verified_at stamps to policy.json"),
) -> None:
    """Verify every allowlist entry against Etherscan. TTY-only when --save."""
```

流程：

1. 拒绝 agent caller（`is_agent()` → `tty_required`）
2. `load_policy()` + `get_chain(chain)`
3. 对 `contract_allowlist` ∪ `recipient_allowlist`（recipient 中是合约的也要核） 每个地址：
   - `get_contract_source` → 取 ContractName / Proxy / Implementation
   - `get_contract_creation` → 取 deployer
   - 渲染一行：`地址 | 合约名 | proxy? | impl | deployer | verified_at（旧的，若有）`
4. 操作员人工 confirm（TTY，y/N 全表通过；当前版本不支持单条 toggle —— 全 yes 或全 no）
5. 若 `--save` + 全 yes → 把每条 `VerificationRecord` 写回 `policy.verified_contracts`，原子写
6. 出口码：所有合约都 verified → 0；任一未通过 / 未在 Etherscan → 1（CI 友好）

### evaluate() 集成

在 contract_allowlist 命中分支加入"verified_at 是否过期"检查：

```python
# In the approve/swap/aave branches that check contract_allowlist:
if target_lower in contract_allowlist_lower:
    rec = policy.verified_contracts.get(target_lower)
    if rec is None:
        return Decision(
            allowed=False,
            reason=f"contract-allowlist-entry-unverified:{target}",
            severity="block",
        )
    age = _age_days(rec.verified_at)
    if age > policy.verification_max_age_days:
        return Decision(
            allowed=False,
            reason=f"contract-allowlist-entry-stale:{target}:age={age}d",
            severity="block",
        )
    # ... existing checks ...
```

**Backwards compat 注意**：现有 sepolia policy.json 里 `verified_contracts` 是空——会导致所有 contract_allowlist 命中失败。处理方式：

- 只有当 `policy.verified_contracts` **非空** 时才启用此检查
- 空 dict 时跳过（向后兼容路径，等用户自己 `policy verify --save` 才激活）
- mainnet policy 模板 + ROADMAP 指引让操作员 verify 一次再上 mainnet

### 测试

`tests/test_policy_verify.py` 新增：

- mocked Etherscan returning known contract name → render path
- `--save` 写回 `verified_contracts` 后 evaluate 命中（fresh）
- `verified_at` 设 31 天前 → evaluate 报 `contract-allowlist-entry-stale`
- agent caller 拒绝 + `tty_required`
- Etherscan 返回 unverified contract（ContractName 空）→ verify 出口码 1
- 空 `verified_contracts` dict 时 evaluate 不报 stale（向后兼容）

代码：~250 行 src + ~50 行 tests，工作量 **1 天**。

---

## Critical files

| 文件 | 改动 |
|---|---|
| `src/wallet/core/config.py` | `ChainConfig` 加 `broadcast_rpc_url` |
| `src/wallet/core/policy.py` | `Policy` 加 4 个字段；`evaluate` 签名加 `chain`；2 个新决策分支 |
| `src/wallet/core/rpc.py` | 新增 `web3_broadcast()` |
| `src/wallet/cli/_common.py` | `confirm_and_broadcast` 传 `chain` 给 evaluate；用 `web3_broadcast` 广播 |
| `src/wallet/cli/policy.py` | 新增 `verify` 命令；`init --mainnet` 选项 |
| `src/wallet/services/explorer.py` | 新增 `get_contract_source` / `get_contract_creation` |
| `tests/test_policy.py` | MEV-protection branch 测试 |
| `tests/test_policy_verify.py` | 新文件，verify 命令 + stale 检查 |
| `tests/test_rpc.py` | `web3_broadcast` 切换逻辑 |
| `docs/skills/wallet-agent.skill.md` | 提到 agent 永远不要试图绕过这两条 |
| `ROADMAP.md` | 把 `require_private_rpc` + `policy verify` 从 Tier 2 移到 commit history |
| `README.md` | mainnet 配置段加 `broadcast_rpc_url` + verify 流程说明 |

---

## 可复用现有代码

- `services/explorer.py:_call` — Etherscan v2 调用，apikey 已隐藏在 error 中
- `core/policy.py:_resolve_allowlist_targets` — allowlist 地址规范化（小写）
- `core/config.py:atomic_write_text` — policy.json 原子写
- `cli/_caller.py:is_agent` — TTY/agent 分流
- `cli/_output.py:emit / emit_error` — JSON envelope

不需要新写 HTTP 客户端、新写文件锁、新写 caller 分流。

---

## Verification

### 单元测试

```sh
uv run pytest tests/test_policy.py -v
uv run pytest tests/test_policy_verify.py -v
uv run pytest tests/test_rpc.py -v
uv run pytest                              # full suite — 应仍全绿
```

### 集成验证（sepolia，零成本）

1. `wallet policy verify --chain sepolia --save`
   预期：Uniswap V3 router / Aave Pool / faucet 全部 verified；写回 `verified_contracts`
2. 手改 policy.json 把 `verified_at` 改成 2025-01-01：
   `wallet aave supply USDC 1 --broadcast --request-id stale-test`
   预期：`policy_block`，reason 含 `stale`
3. `wallet policy verify --chain sepolia --save` 重新刷新 → 同样命令重试 → 通过
4. policy 加 `require_mev_protected_broadcast: true`，`chains.json` 的 sepolia 不设 `broadcast_rpc_url`，hostname 不在 allowlist：
   `wallet send self 0.0001 --broadcast --request-id mev-test`
   预期：`policy_block`，reason 含 `broadcast-rpc-not-mev-protected`
5. 加 sepolia 的 RPC hostname 到 `mev_protected_rpc_allowlist` → 同命令通过

### 集成验证（mainnet dry-run，零 gas）

1. mainnet `chains.json` 加 `broadcast_rpc_url: https://rpc.flashbots.net`
2. `wallet policy init --mainnet` 生成模板
3. 填入测试 allowlist
4. `wallet policy verify --chain ethereum --save`
   预期：4 个 Etherscan 请求，输出对照表，y/N 写回
5. `wallet swap ETH USDC 0.001 --chain ethereum`（dry-run，无 broadcast）
   预期：preview 显示通过；如把 broadcast_rpc_url 改回公共 RPC 重跑 → `policy_block` 在 dry-run 阶段就显示

---

## Out of scope（明确推后）

- **多 RPC fallback**：ROADMAP Tier 2 单独条目，本 plan 不动 `rpc_urls` 数组重构
- **per-implementation 验证（proxy 跳查 impl）**：本版只看 proxy 名 + impl address 字段；二次跳查 impl 的 deployer 留给后续
- **verify 自动化定时任务**：本版不内置 cron / launchd 集成；操作员自己写 wrapper
- **verified_at 单条 toggle**：当前命令全 y / 全 N；细粒度交互留待 verify 出现实际不通过项时再加
- **Mainnet 端到端 broadcast 验证**：mainnet 真广播测试在 `docs/wallet-mainnet-test-plan.md`，那是单独工作流
- **`broadcast_rpc_url` 多 endpoint 数组**：本版单 URL；fallback 由前述 ROADMAP 多 RPC 条目处理

---

## 实现顺序（建议）

1. **Day 0.5**：Change 1 全套（schema + evaluate + web3_broadcast + tests + sepolia 集成验证）
2. **Day 1.5**：Change 2 全套（Etherscan 客户端扩展 + verify 命令 + stale 检查 + tests + sepolia 集成验证）

每天结束跑 `uv run pytest` 应全绿后再继续。Change 2 依赖 Change 1 已合（evaluate 签名变更先落），不要并行做。
