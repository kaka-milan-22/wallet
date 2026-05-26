# Bot secret deployment patterns

> **Status**: 前瞻性设计笔记。基于 `agent-vault@1.0.0` 的预期能力(plan 在 `~/.claude/plans/agent-vault-agent-vault-typescript-user-dynamic-willow.md`,**v1.0 尚未实现**,仍是 v0.5.0 在 npm 上)。
> **写于**:2026-05-26 凌晨,跟 agent-vault v1.0 1Password-style 架构讨论同一场会话。
> **何时回来**:agent-vault v1.0 开始实施前,以及 wallet 1.10 接入新版本前,先回这里 confirm 部署方案没变。

## 背景:为什么需要这个文档

`agent-vault@1.0.0`(规划中)采用 1Password-style 架构:vault 文件不再 self-decryptable,master key 用 Argon2id KDF + user-provided password 加密保护。这跟 v0.5(plaintext vault.key 文件 + 0600 权限保护)是**根本性架构转变**。

转变后,任何无人值守的程序(LP bot / 监控脚本 / scheduled job / CI 任务)如果要通过 wallet 调 agent-vault 签名,都**必须能在没有人输入 password 的情况下**提供 password 给 agent-vault。

这个文档回答的问题:**"没有人在键盘旁边,bot 怎么给 agent-vault 提供 master password?"**

---

## 永恒的安全悖论

```
bot 要无人值守签名
   → bot 需要某种东西能解锁 agent-vault
   → 那个东西必须在 bot 能拿到的位置
   → 攻击者拿到 bot 权限 = 拿到那个东西
   → 解锁 = vault 被开
```

**这是 turtles all the way down,没有完美答案,只能 bound blast radius**。无论你把 password 存哪、用什么方式喂给 bot,bot 进程拿到 password 那一刻,任何拥有同 UID 进程权限的代码都能拿到。

任何"完美解决方案"的承诺都是假的。Threat model 必须现实地接受:**bot vault 的安全上限 = bot 这台机器的安全水位**。所以:

**底线**:bot vault 永远只放小额运营资金 ($200-$8444 这种),treasury 在**另一个 vault、另一台机器、另一个密码**。这才是真正的 defense in depth,不依赖任何单点 password 存储是不是完美。

---

## 5 种 pattern,从弱到强

### 1. Plaintext shell env / .env 文件(最差,等于没加密)

```bash
export AGENT_VAULT_PASSWORD="mypassword"  # 在 ~/.bashrc 或 .env
```

文件 0600 看似安全,但**任何 UID 进程都能读** → 攻击者拿到 `vault.key.enc` + `.env` 文件 = 完整解开。比 v0.5 file mode 没多防多少,**v1.0 加密带来的好处大部分被抵消**。**不推荐**。

### 2. systemd `EnvironmentFile=/etc/agent-vault.env` (Linux 标准做法)

```ini
[Service]
EnvironmentFile=-/etc/agent-vault.env  # 0600, root:bot-user
ExecStart=/usr/bin/lp-bot
```

文件归 root 所有 + 0600 + 只 bot 用户能读。比 pattern 1 强,因为 file ACL 更严 + 受 systemd 进程隔离。但**仍是明文密码躺在磁盘上**,任何 root 提权都泄漏。

### 3. systemd-creds + 系统 host key (Linux modern,推荐 VPS 场景) ⭐

```bash
# 在 VPS 上 root 跑一次
echo "mypassword" | systemd-creds encrypt --name=vault-password - \
    /etc/credstore.encrypted/vault-password
```

systemd 用**系统 host key**(或者 TPM-bound key,如果机器有 TPM2)加密,**只在指定 service 启动时**自动解密,作为 `$CREDENTIALS_DIRECTORY/vault-password` 文件 mount 进 service 进程 namespace。

```ini
[Service]
LoadCredentialEncrypted=vault-password
ExecStart=/usr/bin/lp-bot  # 读 $CREDENTIALS_DIRECTORY/vault-password
```

- ✓ 磁盘上没有明文(密文 with host-key 加密)
- ✓ root 也读不到(除非启动该 service)
- ✓ 有 TPM 的机器:hardware-bound,克隆磁盘到另一台机器解不开
- ✓ 比 env var 强一档(进程 namespace 隔离 + 加密 at rest)

**这是 Linux VPS 上对标 macOS Keychain biometric 的最好方案**。配合 agent-vault v1.0 的 `--password-from-fd`(读 `/run/credentials/.../vault-password` 文件 fd):

```bash
exec 9</run/credentials/lp-bot.service/vault-password
agent-vault write /tmp/x --content "<agent-vault:bot-key>" --password-from-fd 9
```

### 4. Cloud KMS fetch at boot (云原生,中等复杂度)

```python
import boto3
password = boto3.client('secretsmanager').get_secret_value(
    SecretId='lp-bot-vault'
)['SecretString']
os.environ['AGENT_VAULT_PASSWORD'] = password
# 启动 bot 子进程
```

- ✓ 密码不在 VPS 磁盘上
- ✓ 每次访问 AWS audit log 一行
- ✓ IAM role 控制访问 (VPS 用 instance profile,无 long-lived AWS key)
- ✗ 引入 AWS / GCP / Vault 依赖
- ✗ Cloud KMS 同 region 挂了 bot 起不来

**适合**:你本来就在云上跑别的东西,有 IAM 体系。**单独为 agent-vault 上 cloud KMS 是 over-engineer**。

### 5. TPM-sealed password (硬件绑定,最强)

```bash
# Linux with TPM2
tpm2_seal -P passphrase mypassword /etc/agent-vault/sealed-password.tpm
# Boot: tpm2_unseal → password
```

- ✓ 密码被 TPM 加密,**绑死这台物理机器**
- ✓ 克隆磁盘到别的机器解不开
- ✓ 攻击者偷整机才能解(物理威胁)
- ✗ Linux 服务器 TPM 不普及;云 VPS 通常没暴露 TPM
- ✗ 配置复杂

**实际上 systemd-creds (pattern 3) 在有 TPM 的机器上自动用 TPM**,所以你拿到 80% 的好处不用直接玩 `tpm2-tools`。

---

## 跨平台对照表

| 部署 | 推荐 pattern | 安全档次 |
|---|---|---|
| Mac mini 自己用 | macOS Keychain biometric cache(v1.0 默认) | 最高(SEP + 用户 presence) |
| Mac mini 跑 bot 无人值守 | env var from `launchctl setenv` + 0600 plist | 中等(Mac 没 systemd-creds 等价物;可以把 bot password 存进 macOS Keychain 一个 generic-password item,但 item 本身需要 login keychain unlocked) |
| Linux VPS 跑 bot | **systemd-creds + LoadCredentialEncrypted** | 中等-高(TPM 时高) |
| 云上跑 bot 已有 AWS/GCP 体系 | Cloud Secrets Manager fetch at boot | 中等(取决于 IAM 配置) |
| Bare-metal Linux 有 TPM | systemd-creds w/ TPM 自动用上 | 高 |

---

## 关键洞察 —— agent-vault v1.0 的安全收益不依赖密码存哪

把密码存哪是部署细节。**v1.0 真正的结构性升级是"vault 文件本身不再是 self-decryptable"**:

| | v0.5 现状 | v1.0 (with worst-case password in env var) |
|---|---|---|
| 文件被偷(没攻入机器) | ✗ AES key 在另一个文件里,一起偷就解开 | ✓ **没有 password 解不开**(就算你 env var 写在 .env 里,只要 .env 没被偷一起) |
| 机器被攻破 = vault 全光 | ✓ vault.key 秒拿 | ⚠ 还需偷到密码源(env var / systemd-cred / KMS token) |
| 攻击者要付出的成本 | 1 步 (`cat` 文件) | 2 步 (拿文件 + 拿密码) |

**两步攻击的核心价值**:每多一步,攻击复杂度成倍涨。专门盯一个 $8444 LP bot 的攻击者愿意走两步,但供应链 / drive-by / 普通 malware **绝大部分不会**,因为它们打的是规模化通用脚本,**不会专门写"先 cat vault 再找 password env"的逻辑**。

---

## 具体场景推荐

### 场景 A:Mac mini + 你自己 / bot 都跑

```
default vault:  biometric cache (treasury 你 Touch ID)
bot vault:      password 存进 macOS Keychain 一个 generic-password item
                launchctl bootstrap 跑 bot 的 service 用 security cli 取出来
                → bot 启动时通过 LAContext 触发 Touch ID OR pre-authorized
```

### 场景 B:Linux VPS 跑 bot

```bash
# 1. 一次性 setup (root)
echo "$BOT_VAULT_PASSWORD" | systemd-creds encrypt --name=vault-pw - \
    > /etc/credstore.encrypted/lp-bot/vault-pw

# 2. systemd unit
[Service]
LoadCredentialEncrypted=vault-pw
ExecStart=/usr/bin/bash -c '\
    AGENT_VAULT_PASSWORD=$(cat $CREDENTIALS_DIRECTORY/vault-pw) \
    /usr/bin/lp-bot'

# 3. bot 重启 / 系统重启 → systemd 自动解密 → 不需要人值守
```

密码在磁盘上是 host-key (or TPM) 加密的密文。只有 `lp-bot.service` 启动时能解出来。你 ssh 进 VPS 用 root 都直接看不到明文(除非 spawn 一个 unit 模拟,可以做但很 explicit)。

---

## 一句话总结

- **写死在代码** → 别(commit 进 git = repo 公开/被偷 = 全员看到,比 v0.5 plaintext vault.key 更糟)
- **env var / .env** → 凑合,跟 v0.5 file mode 一个档次,**v1.0 加密带来的好处大部分被抵消**
- **systemd-creds (Linux) / launchctl + Keychain (Mac)** → 真实可行,blast radius 受控
- **Cloud KMS / TPM** → 上限更高,**正常场景 over-engineer**

**底线**(再强调一次):bot vault 永远只放小额运营资金,treasury 在另一个 vault 另一台机器另一个密码。**这才是真正的 defense in depth**。

---

## 跟项目其它文档的关系

- `docs/why_hard_wallet.md` —— hardware wallet 的理由,跟这个文档**互补**:这个文档解决"软件 vault 怎么部署到 bot 场景";why_hard_wallet 解决"为什么超过 $1.5k 要硬件钱包"。两者结论一致 —— **任何软件方案都有上限,大额资金最终归宿是硬件钱包**
- `docs/ARCHITECTURE.md` —— wallet 的整体架构。这个文档是 wallet → agent-vault → 部署环境这条链路的下游部分
- `~/claude/wallet/base/architecture.html` —— LP bot 项目的部署架构。本文档为该项目的 secret 管理子问题提供选项
- agent-vault v1.0 plan(`~/.claude/plans/agent-vault-agent-vault-typescript-user-dynamic-willow.md`)—— 本文档假设 v1.0 已 ship。Plan 改了的话本文档要 sync

## 何时 revisit 本文档

1. agent-vault v1.0 开始实施时 → 重读,确认 `--password-from-fd` / `--password-from-env` 接口跟假设一致
2. wallet 1.10 加 `vault_name` per-account 字段时 → 重读,确认部署 pattern 对得上 wallet 的多 vault 路由
3. LP bot 项目正式启动时 → 重读,挑场景 A 或 B,然后写具体 ansible / launchd plist
4. 任何时候你发现新 pattern(比如 1Password Connect server / HashiCorp Vault agent / cloud KMS 新模式) → 加到 5 种 pattern 后面
