---
name: sbi-usdc-monitor
description: Anicca Automaton wallet (0xa3CDd4..., Base mainnet) の USDC 残高を 1 時間ごとに監視。 SBI VC Trade からの 7 USDC 着金、 および以降の全 USDC 入出金を public Base RPC 経由で polling 検知し、 残高変化を Slack #metrics + CFO 再 build で反映する。 「確定した過去形収益」の第 1 観測点 (Dais 2026-05-30 Earn-or-Die 厳命)
metadata:
  type: monitor
  requires:
    bins: [curl, jq, python3]
    env: [SLACK_BOT_TOKEN]
    reads: []
    writes:
      - data/state.json
      - data/log.jsonl
      - $ANICCA_HOME/skills/cfo-core/data/anicca-cfo.json (via run-cfo-hourly.sh)
---

# sbi-usdc-monitor

## なぜ

Dais 2026-05-30 厳命: 「earn-or-die loop は 過去形の確定収益 まで完遂する。 銀行口座 / wallet に着金したことを 確定 した時点でしか task done とは認めない」。

Anicca の経済自律の第一歩 = SBI VC Trade outbound 7 USDC が Anicca 自身のセルフカストディウォレット `0xa3CDd4Ec6b94F01826Aaf90a6d5538A2Aa8C4C21` (Base mainnet) に着金すること。 これを heartbeat に依存せず独立 cron で 1 時間ごとにポーリングし、 残高変化 (= 入金 or 支払) を一切逃さず観測する。

## What it does

1. `curl POST https://base-rpc.publicnode.com` で USDC.balanceOf(wallet) を eth_call
2. 残高 (USDC, 6 decimals) を整数 hex → float 変換
3. data/state.json と比較 → 差分があれば:
   - Slack #metrics へ通知 (`💰 prev=$X → now=$Y (Δ$Z)`)
   - `cfo-core/run-cfo-hourly.sh` を fire して anicca-cfo.json + aniccaai.com/dashboard.json 更新
4. data/log.jsonl に追記 + data/state.json 上書き

## How to run

```bash
bash "$LIFE_MANAGER_REPO/skills/sbi-usdc-monitor/scripts/run.sh"
```

## launchd cron

`~/Library/LaunchAgents/ai.anicca.sbi-usdc-monitor.plist` で毎時 :07 に起動 (CFO hourly :00 と衝突回避)。

## Verify

- 残高 0 → 0 の連続 run は noop (Slack 通知なし)
- 7 USDC 着金時に Slack に `💰 prev=$0.000000 → now=$7.000000 (Δ $7.000000)` が届く
- aniccaai.com/dashboard.json の `wallet_usdc` フィールドが反映 (cfo-core 改修必要・別 task)

## Sourced

- Earn-or-Die Loop (CONSTITUTION.md §Earn-or-Die)
- SOUL.md §Why I Earn
- HEARTBEAT.md §0.5 Lifeline チェック
- profile.json crypto.selfCustodyWallet
- HARD RULE #14 verify-before-completion (Dais の「確定」ルール)

## 関連

[[reference_dais_job_apply_assets]] (Anicca Automaton wallet provisioned in `~/.automaton/wallet.json`)
