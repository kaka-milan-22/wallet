# Bot secret deployment patterns

> **Status**: 前瞻性设计笔记。原稿基于 `agent-vault@1.0.0` 规划中的 1Password-style 架构(每次操作要 master password);**已于 wallet 1.10 迁移到 [AnB](https://github.com/kaka-milan-22/AnB)** 后重写。
> **写于**:2026-05-26;**重写**:2026-05-29(迁移到 alice/bob)。
> **何时回来**:LP bot 项目正式启动前、或 AnB 加 TPM/KMS-sealed KEK(unattended restart)前,回这里 confirm 部署方案没变。

## 背景:为什么需要这个文档

迁移到 AnB 后,架构是 client/server:`alice`(客户端)只存密文 + client cert;AES master key(KEK)在独立的 `bob` 守护进程里,Argon2id 包裹 at rest(`envelope.json`),`mlock` 驻留内存,idle TTL 后清零。`bob` 启动时需要 operator 提供一次 master password(`bob serve` 在 TTY 提示,或读 `$ANB_BOB_PASSWORD`),之后**一直保持解锁、对外提供 decrypt oracle**,直到 idle TTL 或进程退出。

这跟 agent-vault 的关键区别:**解锁是一次性的、发生在 `bob serve` 时,不是每次签名**。所以无人值守的难题从"每次调用怎么喂密码"变成"**bob 启动时怎么在没人值守的情况下拿到 master password**"——一个 once-at-boot 问题。

这个文档回答:**"没有人在键盘旁边,`bob serve` 启动时怎么拿到 master password?"** 以及 AnB 特有的一个新维度:**把 bob 和它的解锁密钥放到跟 bot 不同的机器上**。

---

## AnB 带来的新维度:bob 可以不在 bot 这台机器上

agent-vault 时代,密钥和消费者必然同机。AnB 把信任拆开:

```
bot 机器(跑 LP bot + alice)          硬化主机 / 内网(跑 bob)
  • alice client.key (0600)   ── mTLS ──►  • master KEK (envelope.json)
  • 只有密文,没有 KEK                       • master password 只在这里解锁
  • 攻破它 ⇒ 能在 bob 在线时请它解密          • 攻破 bot 机器拿不到 KEK / password
                                            • idle TTL 一到,窗口关闭
```

- 攻破 **bot 机器**:拿到 `client.key`,在 bob 仍 unlocked 时能冒充 alice 请求解密(受 `authz.json` 前缀授权限制爆炸半径)——但拿不到 master password,也拿不到 envelope,bob 一旦 lock/down 就彻底没戏。
- 攻破 **bob 主机**:那才是真正的"撬保险柜",所以 bob 主机按高价值 SPOF 加固。

**结论**:无人值守 bot 场景,优先把 bob 放到一台更硬化的主机(或内网 / WireGuard 后面),bot 机器只留 alice。下面"怎么喂 master password"的讨论,作用对象是**跑 bob 的那台机器**。

---

## 永恒的安全悖论(仍然成立)

```
bob 要无人值守启动
   → bob serve 需要 master password
   → password 必须在 bob 主机能拿到的位置
   → 攻击者拿到 bob 主机权限 = 拿到那个东西
   → 解锁 = KEK 被开
```

**turtles all the way down,没有完美答案,只能 bound blast radius**。无论 password 存哪、怎么喂给 `bob serve`,bob 进程拿到它那一刻,同 UID 进程权限的代码原则上都能从内存里挖(AnB 用 `mlock` + `PR_SET_DUMPABLE=0` 把这一步抬高,但不是 SE 级隔离)。

Threat model 现实地接受:**bob 的安全上限 = bob 主机的安全水位**。所以:

**底线**:bot 用的 hot 账户永远只放小额运营资金 ($200-$8444 这种),treasury 在**冷储 / 硬件钱包 / 另一套 bob + 另一个 password**。这才是真正的 defense in depth,不依赖单点 password 存储完美。

---

## 5 种 pattern,从弱到强(喂给 `bob serve`)

### 1. Plaintext shell env / .env 文件(最差,等于没加密)

```bash
export ANB_BOB_PASSWORD="mypassword"   # 在 ~/.bashrc 或 .env
bob serve --addr :8443
```

文件 0600 看似安全,但**任何 UID 进程都能读** → 攻击者拿到 `envelope.json` + `.env` = 完整解开。把 Argon2id 包裹的好处大部分抵消。**不推荐**。

### 2. systemd `EnvironmentFile=/etc/anb-bob.env` (Linux 标准做法)

```ini
[Service]
EnvironmentFile=-/etc/anb-bob.env   # 0600, root:bob-user, 含 ANB_BOB_PASSWORD=...
ExecStart=/usr/local/bin/bob serve --addr :8443
```

文件归 root + 0600 + 只 bob 用户能读。比 pattern 1 强(file ACL + systemd 进程隔离),但**仍是明文密码躺在磁盘上**,root 提权即泄漏。

### 3. systemd-creds + 系统 host key (Linux modern,推荐 VPS 场景) ⭐

```bash
# 在跑 bob 的主机上 root 跑一次
echo "mypassword" | systemd-creds encrypt --name=bob-password - \
    /etc/credstore.encrypted/bob-password
```

systemd 用**系统 host key**(有 TPM2 时用 TPM-bound key)加密,**只在指定 service 启动时**解密,mount 进 service namespace 的 `$CREDENTIALS_DIRECTORY/bob-password`。

```ini
[Service]
LoadCredentialEncrypted=bob-password
ExecStart=/usr/bin/bash -c '\
    ANB_BOB_PASSWORD="$(cat $CREDENTIALS_DIRECTORY/bob-password)" \
    /usr/local/bin/bob serve --addr :8443 --ttl 900'
```

- ✓ 磁盘上没有明文(密文 with host-key 加密)
- ✓ root 也读不到(除非启动该 service)
- ✓ 有 TPM 的机器:hardware-bound,克隆磁盘到另一台解不开
- ✓ 进程 namespace 隔离 + 加密 at rest

**这是 Linux VPS 上跑 bob 的最好方案**。配合短 `--ttl`,无人值守窗口进一步收紧。

### 4. Cloud KMS fetch at boot (云原生,中等复杂度)

```bash
ANB_BOB_PASSWORD="$(aws secretsmanager get-secret-value \
    --secret-id anb-bob-password --query SecretString --output text)" \
    bob serve --addr :8443 --ttl 900
```

- ✓ 密码不在 bob 主机磁盘上
- ✓ 每次访问 AWS audit log 一行;IAM role 控制访问(instance profile,无 long-lived key)
- ✗ 引入 AWS / GCP / Vault 依赖;同 region KMS 挂了 bob 起不来

**适合**:你本来就在云上、有 IAM 体系。**单独为 bob 上 cloud KMS 是 over-engineer**。

### 5. TPM-sealed password (硬件绑定,最强)

```bash
tpm2_seal -P passphrase mypassword /etc/anb/sealed-bob-password.tpm
# Boot: tpm2_unseal → ANB_BOB_PASSWORD → bob serve
```

- ✓ 密码被 TPM 加密,**绑死这台物理机器**;克隆磁盘解不开;偷整机才能解
- ✗ Linux 服务器 TPM 不普及;云 VPS 通常没暴露 TPM;配置复杂

**systemd-creds (pattern 3) 在有 TPM 的机器上自动用 TPM**,所以拿到 80% 的好处不用直接玩 `tpm2-tools`。

> **关于 `bob serve -D`**:daemonize 模式在 TTY 上读一次密码、校验后 re-exec 一个 detached child,把密码**经 pipe** 交给子进程——密码不进 env、不落盘。无人值守要的是"启动时无人输入",所以仍需 pattern 2-5 之一把密码喂到 `$ANB_BOB_PASSWORD`;`-D` 解决的是"启动后脱离 TTY 后台常驻",两者正交。

---

## 跨平台对照表

| 部署 | 推荐 pattern | 安全档次 |
|---|---|---|
| Mac mini 自己用,bob 本地前台 | `bob serve` TTY 输密码 | 最高(人值守 + idle TTL) |
| Mac mini 跑 bot 无人值守 | `launchctl setenv ANB_BOB_PASSWORD` + 0600 plist,或存进 login Keychain 的 generic-password item 启动时取 | 中等(Mac 没 systemd-creds 等价物;Keychain item 需 login keychain unlocked) |
| Linux VPS 跑 bob | **systemd-creds + LoadCredentialEncrypted** + 短 `--ttl` | 中等-高(TPM 时高) |
| 云上已有 AWS/GCP 体系 | Secrets Manager fetch → `ANB_BOB_PASSWORD` | 中等(取决于 IAM) |
| Bare-metal Linux 有 TPM | systemd-creds w/ TPM 自动用上 | 高 |
| bot 与 bob 分机 | bot 机器只放 alice client.key;bob 在内网/WireGuard 后,按上面任一 pattern 解锁 | **最推荐**:两台机器都被攻破才丢 KEK |

---

## 关键洞察 —— AnB 的安全收益不依赖密码存哪

把密码存哪是部署细节。**AnB 真正的结构性升级是"客户端不再 self-decryptable + 密钥可与消费者异地"**:

| | agent-vault v0.5 现状 | AnB(worst-case:password 在 env var) |
|---|---|---|
| 客户端文件被偷(没攻入机器) | ✗ AES key 在同机另一文件,一起偷就解开 | ✓ **没有 bob + master password 解不开**(client 只有密文) |
| 机器被攻破 = vault 全光 | ✓ vault.key 秒拿 | ⚠ 攻破 bot 机器只拿到 client cert;还要 bob 在线 + 偷到 bob 主机的密码源 |
| 攻击者要付出的成本 | 1 步 (`cat` 文件) | 2-3 步(拿 client.key + bob 在线窗口 + (异地时)再攻破 bob 主机) |

**多步攻击的核心价值**:每多一步,攻击复杂度成倍涨。专门盯一个 $8444 LP bot 的攻击者愿意走多步,但供应链 / drive-by / 普通 malware **绝大部分不会**,因为它们打规模化通用脚本,不会专门写"偷 client.key 再趁 bob 在线请求解密"的逻辑。

---

## 具体场景推荐

### 场景 A:Mac mini + 你自己 / bot 都跑

```
bob:    本地前台 `bob serve`,treasury 操作时你在键盘旁输 master password
bot:    单独的小额 hot 账户;bob 无人值守时段用
        launchctl setenv ANB_BOB_PASSWORD(0600 plist)起 bob
        → 设短 --ttl;authz.json 把 bot identity 限到自己的 key 前缀
```

### 场景 B:Linux VPS,bob 与 bot 分机(推荐)

```bash
# 在 bob 主机(内网 / WireGuard 后)一次性 setup (root)
echo "$BOB_PASSWORD" | systemd-creds encrypt --name=bob-pw - \
    > /etc/credstore.encrypted/anb-bob/bob-pw

# bob 主机 systemd unit
[Service]
LoadCredentialEncrypted=bob-pw
ExecStart=/usr/bin/bash -c '\
    ANB_BOB_PASSWORD="$(cat $CREDENTIALS_DIRECTORY/bob-pw)" \
    /usr/local/bin/bob serve --addr :8443 --ttl 900'

# bot 机器:只放 alice(enroll 到 bob),client.key 0600,无 master password
# bob 重启 / 系统重启 → systemd 自动解密 → 无人值守
```

密码在 bob 主机磁盘上是 host-key(or TPM)加密的密文,只有 bob service 启动时能解出。bot 机器即便整台沦陷,也拿不到 master password 或 envelope;只能在 bob 在线、且 `authz.json` 允许的前缀内请求解密——把损失锁死在那个小额 hot 账户。

---

## 一句话总结

- **写死在代码 / commit 进 git** → 别(repo 泄漏 = password 泄漏)
- **env var / .env** → 凑合,明文落盘一个档次
- **systemd-creds (Linux) / launchctl + Keychain (Mac)** → 真实可行,blast radius 受控
- **Cloud KMS / TPM** → 上限更高,正常场景 over-engineer
- **bob 与 bot 分机 + 短 idle TTL + authz 前缀授权** → AnB 特有的最强组合,优先选

**底线**(再强调一次):bot 的 hot 账户永远只放小额运营资金,treasury 在另一套 bob、另一台机器、另一个密码、或直接硬件钱包。**这才是真正的 defense in depth**。

---

## 跟项目其它文档的关系

- `docs/why_hard_wallet.md` —— hardware wallet 的理由,跟本文**互补**:本文解决"软件 vault 怎么部署到 bot 场景";why_hard_wallet 解决"为什么超过 $1.5k 要硬件钱包"。结论一致 —— **任何软件方案都有上限,大额资金最终归宿是硬件钱包**
- `docs/ARCHITECTURE.md` —— wallet 整体架构。本文是 wallet → alice → bob → 部署环境这条链路的下游
- `~/claude/wallet/base/architecture.html` —— LP bot 项目的部署架构。本文为该项目的 secret 管理子问题提供选项
- [AnB README](https://github.com/kaka-milan-22/AnB) —— bob 的 `serve` / `--ttl` / `ANB_BOB_PASSWORD` / `authz.json` / 远程部署一节是本文的权威依据;接口变了本文要 sync

## 何时 revisit 本文档

1. AnB 加 TPM / cloud-KMS sealed KEK(unattended restart,免 operator 输密码)→ 重读,这会让 pattern 3-5 中的一大半变成 AnB 原生能力
2. AnB 加 Secure-Enclave client key → 重读,bot 机器的 `client.key` 暴露面会被 SE 收掉
3. LP bot 项目正式启动时 → 重读,挑场景 A 或 B,然后写具体 ansible / launchd plist / WireGuard 拓扑
4. 发现新 pattern(1Password Connect / HashiCorp Vault agent / cloud KMS 新模式)→ 加到 5 种 pattern 后面
