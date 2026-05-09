# wallet — DeFi CLI wallet (Phase 1)

A Python CLI for Ethereum-compatible chains. Phase 1 covers the **non-DeFi
basics**: accounts, balances, transfers, ERC-20 approvals, transaction history,
address book, and watch-only addresses.

Mnemonics live in `agent-vault`; the wallet process retrieves them through a
**Unix named pipe (FIFO)** — `agent-vault` writes the substituted plaintext
into the pipe inode, this process reads it from the kernel buffer, and the
inode is unlinked. The plaintext **never lands on disk**, only in two process
memories briefly. A legacy 0600 temp-file path (`_reveal_via_tempfile`) is
kept as a fallback for platforms where the FIFO transport fails.

## Install

```sh
uv sync
```

The CLI is exposed as `wallet`:

```sh
uv run wallet --help
```

## One-time setup (Sepolia)

```sh
# 1. RPC endpoint (any of these works)
export WALLET_SEPOLIA_RPC=https://ethereum-sepolia.publicnode.com
# or use Alchemy / Infura with your own key

# 2. Etherscan v2 API key for `wallet history`
export ETHERSCAN_API_KEY=...   # https://etherscan.io/myapikey

# 3. Confirm config
uv run wallet info
```

## Account creation flow

```sh
# 1. Generate a new HD wallet
uv run wallet account create main

# Output shows the mnemonic ONCE. Write it down.
# It also prints the next command:

# 2. (in your terminal — NOT inside an LLM agent)
agent-vault set wallet/main/mnemonic
# paste the mnemonic when prompted

# 3. Verify
uv run wallet account show main
# `signed: yes (vault populated)` confirms vault wiring.
```

To **import** an existing mnemonic instead:

```sh
uv run wallet account import main           # prompts hidden input
agent-vault set wallet/main/mnemonic        # paste the same mnemonic
```

To **derive** more addresses from the same mnemonic:

```sh
uv run wallet account derive main --index 1 --as second
```

Both `main` and `second` share the same vault key. One mnemonic, many addresses.

## Daily commands

```sh
# Balance
uv run wallet balance                       # default account, native
uv run wallet balance --token USDC
uv run wallet balance --all                 # every account + watched address

# Transfer (DRY-RUN by default — pass --broadcast to actually send)
uv run wallet send @alice 0.01
uv run wallet send 0xabc... 50 --token USDC
uv run wallet send @alice 0.01 --broadcast  # asks y/N before signing
uv run wallet send @alice 0.01 --broadcast --yes   # skip confirmation

# ERC-20 approval
uv run wallet approve set USDC <spender> 100              # dry-run
uv run wallet approve set USDC <spender> 100 --broadcast
uv run wallet approve set USDC <spender> --unlimited --broadcast
uv run wallet approve show USDC --spender <spender>
uv run wallet approve revoke USDC <spender> --broadcast

# History
uv run wallet history                       # default account, native + contract calls
uv run wallet history --tokens              # ERC-20 transfers
uv run wallet history --address 0x...       # arbitrary lookup

# Address book / watch-only
uv run wallet book add alice 0xabc...
uv run wallet watch add 0xdef... --label vitalik
uv run wallet token add 0x779877A7B0D9E8603169DdbD7836e478b4624789  # adds LINK
```

## End-to-end test (Sepolia)

Once you have an account funded from a faucet (e.g.
[https://sepoliafaucet.com](https://sepoliafaucet.com)):

```sh
uv run wallet balance                       # see the faucet ETH
uv run wallet account derive main --index 1 --as second
uv run wallet book add me_second $(uv run wallet account show second | grep address | awk '{print $2}')
uv run wallet send @me_second 0.001 --broadcast
uv run wallet history                       # see the tx land
```

## Security checks

Run these before trusting the wallet with anything beyond a faucet:

```sh
# (1) Process args — no secrets visible
ps aux | grep -v grep | grep wallet

# (2) State file — only addresses + vault key references, no plaintext
cat "$(uv run wallet info | awk '/state file/ {print $3,$4,$5}')"

# (3) Independent secret audit
agent-vault scan "$(uv run wallet info | awk '/state file/ {print $3,$4,$5}')"
# Should report 0 secrets in the file.

# (4) Confirm no temp files leak during a real signing (run in another shell)
while true; do ls /tmp/wallet-secret-* 2>/dev/null && echo LEAK; done &
uv run wallet send <to> 0.0001 --broadcast
# Watcher should print no LEAK lines — the FIFO transport keeps plaintext off
# disk. Only if the FIFO path fails will you see /tmp/wallet-secret-* appear
# briefly (the fallback path).
```

## Tests

```sh
uv run pytest
```

Covers BIP-39 derivation against Hardhat's well-known fixed mnemonic, state
file roundtrip + 0600 permissions, EIP-1559 tx field construction, fee floor
behaviour, simulation revert surfacing, amount fixed-point arithmetic, and
the FIFO-based vault.reveal() transport including the tempfile fallback.

## Architecture

```
src/wallet/
  cli/                 typer command tree + presentation (rich)
  core/
    config.py          ChainConfig — chain_id, RPC, explorer, builtin tokens
    hd.py              BIP-39 / BIP-44 (eth-account)
    rpc.py             Web3 factory, format_units / parse_units
    signer.py          mnemonic → private key → sign (in-memory only)
    tokens.py          ERC-20 ABI + helpers (balanceOf, allowance, fetch)
    tx.py              build/simulate/estimate pipeline; EIP-1559 fee policy
  storage/
    state.py           pydantic schema for ~/.wallet/state.json
    vault.py           agent-vault subprocess wrapper (has / reveal)
  services/
    explorer.py        Etherscan v2 client
```

Sending commands all go through `core/tx.py` → preview → confirm →
`core/signer.py`. The signer is the only code path that touches mnemonic
plaintext; it derives, signs, and returns raw bytes.

## Phase 2 (not in this repo yet)

DEX swap (0x / 1inch aggregator), Aave V3 supply / borrow / repay, Lido
staking, and a `protocols/` directory that drops in alongside `core/`. The
existing tx pipeline is the entry point — protocol modules just produce
unsigned txs and hand them to `core/tx.py`.
