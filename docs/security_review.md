# Security Review — 2026-05-15

本次审计在 `tier 0 round 3`（commit `5dffc3b`）之后进行，覆盖整个项目代码（非 diff
review），重点放在加密钱包的高价值攻击面：私钥/助记词处理、签名逻辑、外部
quote/RPC 信任边界、policy gate、幂等性。审计方法是 base agent 全量扫描 + 5 路
并行 false-positive 过滤，**只收录置信度 ≥ 8/10 的发现**。

最终通过两条 finding：一条 High（swap 输入 token 误判）+ 一条 Medium（swap 路径
下幂等指纹塌缩）。其它 8 条候选被过滤，列在文末以保留上下文。

---

## 审计方法

- **范围**：`src/wallet/{core,storage,protocols,services,cli}/*.py`，逐文件通读，
  不只 grep。重点文件：`storage/vault.py`、`core/signer.py`、`core/hd.py`、
  `core/rpc.py`、`protocols/aave.py`、`protocols/routes/zerox.py`、
  `protocols/routes/uniswap_v3.py`、`storage/idempotency.py`、`core/policy.py`。
- **攻击者模型**：
  - 信任：CLI flag、环境变量、本地用户身份。
  - 可控：0x API 响应（compromise / cache poisoning）、RPC 节点响应、
    explorer 响应、链上合约返回值、ERC20 metadata（`name`/`symbol`/`decimals`）、
    用户接受的 chain 配置（仅经过现有 CLI 暴露的窄通道）。
  - 不信：FS 任意写入（threat model 排除）、TLS 主动剥离公网 api.0x.org。
- **流程**：base agent 列出 10 条候选 → 5 路并行 verification subagent 重读代码 +
  独立打分 → 只保留 ≥ 8/10。
- **硬排除项**：DoS、磁盘上 secrets at rest（vault 设计本身覆盖）、速率限制、
  依赖 CVE、log spoofing、仅控 path 的 SSRF、文档问题、缺乏 audit log、
  无具体攻击路径的"hardening 不足"。

---

## Findings

### Vuln 1：Swap 路径用 ERC20 `symbol()` 决定 native-vs-token，可被恶意合约欺骗

* **Severity**：High
* **Confidence**：9 / 10
* **Category**：input validation / external call safety
* **受影响文件**：
  - `src/wallet/protocols/swap.py:68`（`is_native_in = token_in.symbol == chain.native_symbol`）
  - `src/wallet/protocols/routes/uniswap_v3.py:102, 155`（同样的字符串判断，`value = amount_in_wei`）
  - `src/wallet/core/tokens.py:96-101`（`fetch_token_info` 直接取链上 `symbol()`）
  - `src/wallet/cli/swap.py:26`（`_resolve_token_or_native` 仅在用户字面输入 `"ETH"` 时走 native 分支）
  - `src/wallet/cli/_common.py:257, 261-265`（preview 不显示输入 token 的合约地址）

**漏洞描述**

`prepare_swap` 通过字符串比较 `token_in.symbol == chain.native_symbol` 判断输入是
否为 native ETH。当用户/agent 以 `0x…` 地址形式传入输入 token 时，
`_resolve_token_or_native` 会落入 `resolve_token` → `fetch_token_info`，后者直接
从链上合约读取 `symbol()`，**完全由攻击者控制**。

一个恶意 ERC20 只需让 `symbol()` 返回 `"ETH"`，就能让 swap 流程：
1. 跳过 `swap.py:69-78` 的 allowance 预检查（认为是 native 输入）；
2. 在 `routes/uniswap_v3.py:155` 把 `value` 设为 `amount_in_wei` 的**真实 native ETH**；
3. 用合法的 `WETH` 地址进入 Uniswap 的 calldata（`uniswap_v3.py:104`）。

rich preview 表里显示 `amount: 1 ETH` 和 `route: ETH > 3000bps > USDC`，**不显示
输入 token 的合约地址**（`_common.py:257`）。一个用户/agent 即使看了 preview，
也无法察觉本来想卖的 `0xBadToken` 实际上变成了卖 native ETH。

**利用场景**

1. 攻击者部署 `0xBadToken`，`symbol()` 返回 `"ETH"`。
2. 通过钓鱼 token 列表、社交工程或污染的 LLM 上下文，让 agent 跑：
   `wallet swap 0xBadToken USDC 1 --via uniswap_v3 --broadcast`
   语义上期望"卖 1 单位的无价值 token 换 USDC"。
3. 钱包构造 tx：`value = 1 ETH`（真实 native），`to = UniswapV3Router`，
   calldata 走 WETH → USDC 的合法 pool。
4. 链上结算：用户掏 1 ETH ≈ $2-3k，换回少量 USDC。
   `0xBadToken` 一根毛都没动。

**影响**

每笔最高一次性损失 = `amount_in_wei` 单位的真实 ETH。在 mainnet 默认 policy
`max_per_tx` 为 0.5 ETH 时，单次封顶约 $1-1.5k（按 $2-3k/ETH）；日封顶按
`max_per_day` 累乘。`--via 0x` 路径因为 0x 服务端自己根据 sentinel 决定 `value`
而被部分护住；`--via uniswap_v3` 和 `--via auto`（fallback 到 uniswap_v3）**完全
可利用**。

**修复建议**

不要用 `symbol()` 做路由决策。两个等效的结构性方案：

- 在 `TokenInfo` 上加显式字段 `is_native: bool`，只有 `_resolve_token_or_native`
  在用户字面输入 native 符号时才置 True；`fetch_token_info` 永远写 False。
- 或：判断 `token_in.address.lower() == chain.builtin_tokens["WETH"].lower()` 且
  来源是 chain 内置 token 表（不是链上读取）。

附带的纵深防御：在 `_common.py` swap preview 表里加一行
`token in (addr): 0x…`，让人/agent 在签名前能 diff 输入地址。

---

### Vuln 2：Swap 路径下 idempotency 指纹塌缩 → 静默回放错误 tx_hash

* **Severity**：Medium
* **Confidence**：9 / 10
* **Category**：replay / idempotency
* **受影响文件**：
  - `src/wallet/storage/idempotency.py:69-89`（`_fingerprint` 实现）
  - `src/wallet/protocols/swap.py:95-111`（`prepare_swap` 写入 description 的字段名）
  - `src/wallet/cli/_common.py:442-473`（replay 命中时的 envelope 形状）
  - `src/wallet/storage/idempotency.py:7-9`（docstring 承诺 Stripe-style 语义）

**漏洞描述**

`_fingerprint` 读取的字段是 `desc.get("token_address")`，但 `prepare_swap` 把
swap 的输出 token 写到 `swap_token_out_address`，把 min-out 写到
`swap_amount_out_min_wei` —— **从来没有写过 `token_address` 这个 key**。指纹同时
缺失 `chain_id`（只用 `chain.name`），地址也没小写化。

结果是：两笔 swap 只要满足
- 同 `chain.name`
- 同 `from`
- 同 `to`（= 同一 router 地址）
- 同 `kind = "swap"`
- 同 `amount_wei` + 同 `unit`（= 输入 token 符号）

就会得到**完全相同的 fingerprint**，哪怕输出 token 不同、min-out 不同、链 ID 不同。

`lookup()`（`idempotency.py:114-131`）发现指纹匹配后，按设计返回
`CachedResult`，`_common.py:452-473` 据此生成的 JSON envelope 是：

```json
{ "ok": true, "tx_hash": "<第一笔的 hash>", "data": { "phase": "idempotent_replay", "outcome": "replayed_idempotent", ... } }
```

顶层没有 `replayed: true` 字段，`ok` 和 `tx_hash` 形状和正常成功广播完全一致。
agent 拿到这个 envelope，只检查 `ok` / `tx_hash` 就会认为第二笔已经成功。

**利用场景**

1. agent 调用：
   `wallet swap USDC WETH 100 --request-id R1 --broadcast`
   成功，缓存。
2. agent 因任意原因（重试逻辑 bug、planner 复用 ID、prompt 缓存）再调：
   `wallet swap USDC DAI 100 --request-id R1 --broadcast`
3. 指纹相同，钱包返回第一笔 WETH 交易的 hash，标记 `ok: true`。
   **没有任何链上广播发生。**
4. agent 进入下一步："现在用刚换到的 DAI 去做 X"。
   实际余额里只有 WETH，DAI 为零。下游链条上的任何动作（再 swap、还款、给地址
   打 DAI）都会失败或行为异常 —— 在极端情况下，agent 误判后续操作的输入状态，
   可能导致更大的资金损失（例如在 Aave borrow 路径下错误估计抵押）。

**影响**

- 直接：违反 `idempotency.py:7-9` 文档承诺的 Stripe-style 语义（同 request_id
  → 同结果 / 否则 `IdempotencyMismatch`）。这里既没等价回放，也没抛 mismatch。
- 间接：agent 在错误的状态假设下继续操作，是真实资金风险。

**修复建议**

- 把 `chain.chain_id`（不只 `chain.name`）、`desc.get("swap_token_out_address")`、
  `desc.get("swap_amount_out_min_wei")` 加入指纹输入。
- 所有 address 在 hash 前 `.lower()`，避免 checksum 大小写造成假阴。
- 更彻底的方案：直接对**规范化的 signed-tx 字段（去掉 nonce）** 求 hash —— 这
  天然覆盖一切影响链上行为的字段。
- 在 replay envelope 顶层加 `replayed: true` boolean，agent 不用挖到 `data.phase`
  才能区分缓存命中 vs 新广播。

---

## 已过滤的候选（透明记录）

下列 8 条在 base agent 阶段被列为候选，但在 verification 阶段未通过 ≥ 8/10
置信度阈值，已被过滤。保留在这里是为了：(a) 让后续 review 不重复劳动，
(b) 标记纵深防御方向（即使不构成 finding，也值得未来 hardening 时参考）。

| # | 候选 | 文件 | 最终置信度 | 过滤理由 |
|---|---|---|---:|---|
| F1 | 0x calldata 未与显示 minOut 做断言 | `protocols/routes/zerox.py:99-110, 126-141` | 6/10 | 攻击需 0x 自身或 CDN 被攻破；损失上限为 sandwich 损失非全额清零。值得做 calldata decode 校验作为 hardening，但当下攻击路径不够具体。 |
| F2 | `_reveal_via_tempfile` 竞态 | `storage/vault.py:111-122, 167-198` | 2/10 | `mkstemp` 创建 0o600 inode，跨 UID inotify 看到 CREATE 事件但 `open()` 被 DAC 拒绝；同 UID 攻击者已经赢了（ptrace 等）。理论竞态，无具体跨权限攻击路径。 |
| F3 | 签名异常通过 envelope 泄漏 | `cli/_common.py:520-530` | < 7 | 现状 `eth_account` 和 `hd.derive` 不在异常里带助记词；要求未来贡献者写出泄漏代码才成立 —— 推测性。`except Exception` 收窄是合理 hardening，但不是当前漏洞。 |
| F4 | Aave HF 估算信任 oracle / LT | `protocols/aave.py:402-453` | < 7 | mainnet 用 Chainlink-backed 真实 oracle，超出 threat model。testnet mock oracle 可被换值但是预期行为。 |
| F5 | idempotency 文件解析失败静默丢条目 | `storage/idempotency.py:96-111` | < 7 | 运维健壮性问题，需要外部触发（concurrent crash / 磁盘问题）才放大成 replay；不是被攻击者直接利用的漏洞。 |
| F6 | policy `unknown` 类别 default-allow | `core/policy.py:128-146` | < 7 | 当前所有 `prepare_*` 都明确分类，需要未来贡献者新增 `prepare_*` 时忘记更新分类表才成立 —— 推测性。值得改为 fail-closed。 |
| F7 | 0x `quote.spender` 不固定到已知 AllowanceHolder | `protocols/routes/zerox.py:106-109` | 7/10 | 攻击需 0x API 被攻破 + 用户预先在 `contract_allowlist` 里加了 UniswapV3Router + 有 stale 大额 approval。前置条件多。修复（pin 到 chain-known allowance holder）成本低，建议做为后续 hardening。 |
| F8 | Aave `getUserAccountData` HF "no debt" 哨兵 brittle | `protocols/aave.py:294-306` | < 7 | 与 F4 同样依赖恶意 Aave 部署，mainnet 无效。 |

---

## 后续建议

- **Vuln 1（High）应当作为下一轮 tier 0 round 4 的首要项**。修复成本低（一行字段
  + 几行调用点），影响面大，且与 Vuln 2 不冲突，建议合并到同一个 PR：
  `prepare_swap` 这一层一起改 `is_native` 判断 + idempotency 指纹字段集。
- **Vuln 2（Medium）独立可发**，纯改 `idempotency.py` 的指纹组成 + envelope 顶层
  加一个 boolean，0 影响外部行为。
- **F7（0x spender pinning）**虽然没通过门槛，但修复成本极低（每链一个常量
  地址 + 一行断言），建议顺手做掉。
- 复盘一下：本次审计 5 路 verification 中 3 路打 < 8，说明 base agent 阶段的
  推测性候选偏多。后续如果再做这种全量扫描，可以在 base 阶段就要求每条候选必
  须给出"具体的输入控制路径"而不是"如果未来代码这么改就会有问题"。

**硬件钱包仍然是 mainnet > $1500 资金的硬性前置**，见
[why_hard_wallet.md](why_hard_wallet.md)。本审计的所有 finding 都是软件层；
私钥一旦泄漏，所有 policy / idempotency / audit 防线一并失效。
