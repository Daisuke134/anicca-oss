# Earning loops

この文書は収益系loopの運用入口です。product catalogの正本は
[`README.ja.md`](../README.ja.md#14本の主要product-loop)、実行jobの正本は
[`config/loop-registry.json`](../config/loop-registry.json)です。この文書に独自のloop一覧や
scheduler構成を複製しません。

## 現在の原則

- Money Printerは15番目のloopではなく、収益を作るproduct loopsの総称です。
- Coconala、Lancers、CrowdWorksはHuman Gig Work familyです。
- Agent Economyは、owner資金と混ぜないcitizen wallet、検証済み収益、compute支出を扱います。
- CFOは各businessの検証済み収益、残高、支出、payoutを集約します。
- Mobile App Loopsはproduct manifest、配信、計測、収益receipt、CFO handoffを共有します。
- 実行sourceはLife Manager repo内に置きます。OpenClaw、Hermes、別checkout、worktree、home directoryのskill treeを実行依存にしません。
- Telegram報告はrepo所有の共通transportを使い、provider message IDをreceiptとして保存します。
- 外部serviceはrepository-owned adapterとユーザー提供credentialの先に置きます。Postiz、marketplace、financial providerそのものをrepoへ複製しません。

## Local / Cloud

同じloop ID、business recipe、effect rule、replay protection、receipt schema、Telegram reportを共有します。
違うのはhost adapterだけです。

| Surface | Host adapter | Private state |
|---|---|---|
| Local | macOSではimmutable release + `launchd` | `~/.local/state/life-manager`等のowner管理領域 |
| Cloud | Railway worker / scheduler | tenant-scoped database、object store、secret store |

Cloudが全14 loopを自動的にhostするという意味ではありません。READMEのsetup表でCloud対応と明記された
loopだけがCloudで稼働します。Localもcredential、KYC、browser loginがないloopは安全に
`setup_required`となり、成功を偽装しません。

## 操作

```bash
jq -r '.loops | keys[]' config/loop-registry.json
./bin/lm-loop status all
./bin/lm-loop doctor
```

cloneだけで全effectful loopを起動しません。READMEの各loopのsetup/start pathに従い、準備済みの
loopだけを選択して起動します。旧tmux、ClawRouter daemon、OpenClaw cron、旧Video earning slotは
現在の運用architectureではありません。
