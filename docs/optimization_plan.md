# Wallet 项目优化建议（plan）

## Context

`/Users/bbwave03/claude/wallet` 是一个面向 AI agent 的 DeFi CLI wallet（Python 3.13 + web3.py + typer + rich），约 9k 行（含测试）。当前 README 已经定位为 "AI agent-native"，包含 policy gate / idempotency / audit log 等关键安全机制。

本计划基于三路并行审计（架构 / 安全 / UX-性能-依赖）的结果，列出**可落地的优化项**，按影响 × 紧迫度排序。目标不是大改架构，而是消除已知缺陷、补齐漏掉的护栏。

---

## Tier 0 — 立即修复（Critical，肉眼可见的 bug）

### 0.1 `wallet send` 命令运行时 NameError
- 文件：`src/wallet/cli/send.py:34`
- 现状：调用 `make_web3_or_exit(cfg, command="send")`，但第 6 行 import 列表里没有它（同级的 `approve.py:6` 是正确写法）。
- 实测：`app.py:36` 注册了 `app.command("send")(send_cmd)`，没有任何测试覆盖 send 命令（`tests/` 里没有 `test_send.py`，也没有 e2e CLI invoke），所以这个 bug 一直没被发现。
- 修复：在 `from wallet.cli._common import` 行追加 `make_web3_or_exit`；并新增一个 `tests/test_send.py`，至少做 `CliRunner` 级别的 import-and-help 烟雾测试，避免再有 import 漏写。

### 0.2 `DerivedAccount` 默认 repr 泄漏私钥
- 文件：`src/wallet/core/hd.py:24-28`
- 现状：`@dataclass(frozen=True)` 自动生成的 `__repr__` 会把 `private_key: bytes` 全量打印。任何 `print(acct)`、未捕获异常 traceback、`logging.debug(..., acct)`、调试器 watch、`rich.console.Console.log()` 都会把 32 字节私钥写到 stderr / 日志。
- 修复：将字段改成 `private_key: bytes = field(repr=False)`；或者实现自定义 `__repr__` 把私钥脱敏成 `b"<redacted>"`。属于一行修复，受益巨大。

### 0.3 audit.log 文件权限 0o644
- 文件：`src/wallet/storage/audit.py:49`
- 现状：`os.open(..., 0o644)`。注释明说 "owner write, anyone read locally" —— 但威胁模型里 "敌意 LLM caller 在同一台机器" 是核心场景，本机其他进程/用户可以直接 cat 出完整的交易历史（from/to/amount/tx hash）。
- 修复：改成 `0o600`；同时在创建文件后 `os.chmod` 一次以兼容已存在的旧文件（或者写一句迁移代码：启动时如检测到 mode != 0o600 就 chmod）。

---

## Tier 1 — 短期补强（High，agent 视角的真问题）

### 1.1 Nonce 在 prepare 阶段获取，broadcast 时已可能 stale
- 文件：`src/wallet/core/tx.py:60-69`（`_common_fields`）
- 现状：`nonce` 是在构造 PreparedTx 时一次性读取，dry-run → broadcast 之间任何并发 tx 都会导致 nonce 复用或 gap。Agent 工作流中两个 `send` 命令几乎同时执行就会撞车。
- 修复：把 nonce 的获取下沉到签名前（`confirm_and_broadcast` 即将调用 `sign_transaction` 之前），prepare 阶段不写入 `nonce` 字段；这样 dry-run preview 也不需要承诺 nonce。需要同时小改 `_simulate` / `estimate_gas` 路径让 nonce 缺省 → web3 会用 `pending` 自动填，预演不受影响。

### 1.2 `approve --unlimited` 没有显眼警告
- 文件：`src/wallet/cli/approve.py:60-71`
- 现状：`--unlimited` 走 `MAX_UINT256`，但 dry-run / rich 输出里没有红色 banner。Policy 没配置时 agent 可以无摩擦签出无限授权。
- 修复：在 `prepared` 构造之前，无论 dry-run / 是否走 policy，都通过 `stdout_console().print("[bold red]⚠ UNLIMITED APPROVAL ...[/bold red]")` 打出一条警告；JSON 模式下在 envelope 加 `"warnings": ["unlimited_approval"]` 字段，让 agent 也能检测到。

### 1.3 Token info 没缓存，portfolio 接口 N×3 RPC
- 文件：`src/wallet/core/tokens.py`（`fetch_token_info` / `resolve_token`）
- 现状：每次调用都做 `symbol` / `decimals` / `name` 三个 eth_call。`portfolio` 命令对 watch list 里的每个 token 都重新发起，10 个 token 就是 30 次 RPC。
- 修复：在 `fetch_token_info(w3, address)` 上加 `@functools.lru_cache(maxsize=512)`（按 `(chain_id, address)` 缓存）；symbol/decimals 链上不可变，无失效风险。balance 仍然每次重取。

### 1.4 L2 上的 EIP-1559 费用估算可能错的离谱
- 文件：`src/wallet/core/tx.py:30-46`
- 现状：`max_fee = base * 2 + priority`。Arbitrum / Optimism / Base 的 `baseFeePerGas` 远低于 mainnet，而真正成本主导是 L1 data fee；当前公式既不会算多也不会出错，但用户看到的 `estimated_fee_wei` 可能严重低估（agent 据此判断 "费用可接受" 然后实际付了更多）。
- 修复：在 `_fees()` 里识别 `chain.chain_id ∈ {10, 8453, 42161, 7777777, ...}` 时，额外通过 `eth_estimateGas` + `gasPrice` 走 legacy 路径，或读 `GasPriceOracle` 预编译合约。先做最小版本：识别 L2 时把 `max_fee` 改成 `max(base*2+priority, w3.eth.gas_price)` 兜底。

### 1.5 缺 `WALLET_HOME` / `WALLET_DATA_DIR` 环境变量
- 文件：`src/wallet/core/config.py:63-86`
- 现状：所有 state / audit / policy / idempotency 路径都通过 `platformdirs.user_data_dir(...)` 锁定。容器化、CI、多账户隔离场景没法切目录。
- 修复：在 `state_path()` / `audit_path()` / `chains_config_path()` 前加一行 `if env := os.environ.get("WALLET_HOME"): base = Path(env)`，并在 README 文档里说明。

---

## Tier 2 — 中期重构（Medium，质量与可维护性）

### 2.1 `cli/_common.py` 的 `_category()` 与 `_kind_machine()` 高度重复
- 文件：`src/wallet/cli/_common.py:72-116`
- 修复：合并为 `_classify_kind(prepared, *, as_category=False)`，统一遍历 kind 映射表。

### 2.2 4+ 处 sender / address 解析散落
- 文件：`approve.py:_sender`、`balance.py:_resolve_targets`、`history.py:_resolve_target`、`aave.py:_resolve_account`
- 修复：抽到 `_common.py` 一个 `resolve_sender(state, name | None) -> AccountEntry` + `resolve_targets(state, names) -> list[AccountEntry]`，所有 CLI 共用。

### 2.3 `aave.py` 协议层 + CLI 层共 1470 行，视图逻辑下沉到了协议层
- 文件：`protocols/aave.py`（799 行）+ `cli/aave.py`（671 行）
- 修复：把 reserve 显示 / health factor 文案 / 表格构造从 `protocols/aave.py` 抽回 `cli/aave.py`；协议层只负责返回结构化数据（dataclass / TypedDict）和发起合约调用。

### 2.4 `policy.py` 中 audit log 解析对损坏行静默跳过
- 文件：`src/wallet/core/policy.py:145-172`
- 现状：`json.JSONDecodeError` `continue`。攻击者改写 audit.log 让特定行变成无效 JSON，就能把当日已花额度从滑动窗口里"擦掉"。
- 修复：用 `stderr` warning 提示，并把 audit 行设计成 hash chain（每行包含上一行的 hash），policy 在读取时验证链完整性，破坏即失败 close。

### 2.5 Idempotency / policy 文件写入未 fsync
- 文件：`src/wallet/storage/idempotency.py:59-61`、`src/wallet/core/policy.py:77-83`
- 修复：在 `tmp.replace(p)` 之前 `os.fsync(fd)`，并对 parent dir 也 fsync（ext4 / APFS 需要）。

### 2.6 vault fallback 路径把私钥写到 tmp 文件
- 文件：`src/wallet/storage/vault.py:167-198`（`_reveal_via_tempfile`）
- 修复：Linux 上优先 `memfd_create`；fallback 写盘前用 `os.O_TMPFILE` + 立即 unlink（保持 fd 可读）；写盘后用随机字节覆盖再 unlink，避免崩溃残留。

### 2.7 RPC 没有重试 / 退避
- 文件：`src/wallet/core/rpc.py`
- 修复：包一层简单的 `for attempt in range(3): try: return ... except (HTTPError, ReadTimeout, JSONRPCError): backoff`，只对幂等读操作（`eth_call`、`get_block`、`max_priority_fee`、`get_transaction_count`）启用。

---

## Tier 3 — 加分项（Low / nice-to-have）

- `pyproject.toml` 依赖加上 upper bound：`web3>=7.0,<8`、`httpx>=0.27,<1` 等，防止 supply-chain 自动跳大版本。
- 加 `--debug` / `-v` 全局 flag，把每个 RPC 请求/响应输出到 stderr（json-safe），方便 agent debug。
- 加 `__all__` 到每个 module，明确公共 API；同时打开 `mypy --strict` 跑一遍补 type hints（`confirm_and_broadcast` 的 `w3` / `sender_account` 当前未标注）。
- `is_valid_mnemonic` 的 `except Exception`（hd.py:53）可收窄成 `eth_account` 抛出的具体异常。

---

## 关键文件清单（按修复优先级）

| 优先级 | 文件 | 主要动作 |
| ---- | ---- | ---- |
| Tier 0 | `src/wallet/cli/send.py` | 补 import + 加测试 |
| Tier 0 | `src/wallet/core/hd.py` | `repr=False` 私钥字段 |
| Tier 0 | `src/wallet/storage/audit.py` | 0o644 → 0o600 + 迁移 |
| Tier 1 | `src/wallet/core/tx.py` | nonce 下沉、L2 费用兜底 |
| Tier 1 | `src/wallet/cli/approve.py` | unlimited 警告 |
| Tier 1 | `src/wallet/core/tokens.py` | lru_cache token info |
| Tier 1 | `src/wallet/core/config.py` | `WALLET_HOME` env var |
| Tier 2 | `src/wallet/cli/_common.py` | 合并 `_category` / `_kind_machine`、抽 sender 解析 |
| Tier 2 | `src/wallet/protocols/aave.py` + `cli/aave.py` | 视图层抽回 cli |
| Tier 2 | `src/wallet/core/policy.py` | audit hash chain |
| Tier 2 | `src/wallet/storage/{idempotency,vault}.py` | fsync / memfd |
| Tier 2 | `src/wallet/core/rpc.py` | retry/backoff |

---

## 验证方式（实施后）

每个 Tier 单独验证、不要混并：

- **Tier 0 验证**
  - `uv run wallet send 0xdead 0.0 --dry-run` 不再 NameError
  - `python -c "from wallet.core.hd import derive; print(repr(derive('test test test test test test test test test test test junk')))"` 看不到原始私钥
  - `stat -f %A ~/Library/Application\ Support/wallet/audit.log` 返回 600
  - `uv run pytest tests/test_send.py` 通过
- **Tier 1 验证**
  - 在 Sepolia 上跑两个并发 `wallet send`，验证 nonce 不冲突
  - `wallet approve set USDC 0xspender --unlimited --dry-run` 看到红色警告（rich）/ `warnings` 字段（json）
  - 给同一个 token 连续跑 5 次 `wallet portfolio`，用 `--debug` 观察 RPC 调用数据下降
  - `WALLET_HOME=/tmp/wallet-test uv run wallet account list` 路径切换生效
- **Tier 2 / 3 验证**：`uv run pytest` 全绿；对重构动到的模块单独补单测。
