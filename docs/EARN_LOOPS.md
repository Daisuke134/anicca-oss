# EARN LOOPS — human-funded (claude-p) と self-funded (ClawRouter/genesis) の全体像

**目的**: 「どのloopが毎日走っていて、何をしていて、どこにあるか」を常に把握できるようにする。
Dais 2026-07-04 指摘: 「何度も見失って重複loopを作ってしまう」事故が実際に起きた(下記 実インシデント参照)。
このファイルが唯一のSSOT(Single Source of Truth) — 新しいloopを足す/消す時は必ずここも更新する。

## 0. 2層アーキテクチャ(TIER 1 / TIER 2)

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│ TIER 1: human-funded AI      │        │ TIER 2: self-funded AI        │
│ = "claude-p"                 │        │ = "ClawRouter" / genesis      │
│                               │        │                               │
│ 燃料 = Dais個人のClaude       │        │ 燃料 = 自分のwallet(USDC)+    │
│        Code/Anthropicサブ    │        │        ClawRouter x402課金    │
│                               │        │        (無料モデル時 $0)      │
│ 実体 = tmux常駐 + claude -p  │        │ 実体 = 単一 launchd daemon    │
│        (5つの独立loop)       │        │        (com.anicca.daemon)   │
│                               │        │                               │
│ 起動: *-cli.sh (tmux new-    │        │ 起動: anicca-daemon.sh →      │
│       session -d)            │        │       runtime/loop/index.mjs  │
│                               │        │       (無限loop、120秒毎wake)│
│ 生死監視: *-healthcheck.sh   │        │ 生死監視: launchd             │
│  (5分毎launchd)              │        │  KeepAlive=true               │
└─────────────────────────────┘        └──────────────────────────────┘
```

**Claude Code (このdev session)自体はどちらでもない** — project CLAUDE.md記載の通り、開発用ad-hoc
agent。ただし `~/anicca` repo に含まれる skill 群は model-agnostic 設計(README.md「Model-agnostic
by design」、`ANICCA_BRAIN=claude-p`での切替オプションあり)なので、DeepSeek/Gemini/Grok等どのAIが
cloneして起動しても同じloopが使える想定。ただし完全な汎用ハーネス化は `docs/EXECUTION-ORDER.md` 上
`LATER`(未着手)扱い。

## 1. TIER 1: claude-p (human-funded) — 5つの常駐loop

全て同一パターン: `tmux -S /tmp/anicca-<name>-tmux.sock new-session -d -s anicca-<name>-core` で
`claude -p` を常駐起動 → 起動直後に1パス実行 → その後 `CronCreate`(下記2.1参照)で自己登録した
cronが後続passを駆動 → tmuxセッションは stay idle。

| method | tmux session | cli.sh | healthcheck.sh (5分毎launchd) | producer.sh | run.sh | cron頻度 | 収益通貨/アカウント |
|---|---|---|---|---|---|---|---|
| **clip** | `anicca-clip-core` | `skills/earn/clip/clip-cli.sh` | `clip-healthcheck.sh` | なし(`~/.claude/skills/earn-clip-rewards/scripts/pipeline.py`を利用、`ai.anicca.clip-producer.plist`AM3:17で別途daily) | `clip/run.sh` | 毎時7分 | USDC(IG per-view報酬、founder wallet) |
| **affiliate** | `anicca-affiliate-core` | `affiliate/affiliate-cli.sh` | `affiliate-healthcheck.sh` | `affiliate/producer.sh` | `affiliate/run.sh` | 毎日08:41 JST | ¥(Amazon Associates JP `aniccaai-22`) |
| **video** | `anicca-video-core` | `video/video-cli.sh` | `video-healthcheck.sh` | なし | `video/run.sh` | 4時間毎(23分) | USDC(`money_blueprintdaily`専用アカウント) |
| **bounty** | `anicca-bounty-core` | `bounty/bounty-cli.sh` | `bounty-healthcheck.sh` | なし | `bounty/run.sh` | 毎日09:29 JST | USD(Algora GitHub bounty、マージ+実支払のみ計上) |
| **gig** | 4 direct launchd owners | なし | shared registry `launchd-ledger` probe | 各owner自身 | `gig/run.sh`(read-only集約) | owner別60–300秒 | ¥(ココナラ→Daisの三菱UFJ銀行、human-funded) |

**gigはshared tmux coreを使用しない。** Apply / Reply / Paid / Storefrontの4つのdirect ownerを
shared earning-health registryがlaunchd labelとdurable Storefront wake ledgerからread-only監視する。

**launchd plist配置**: 各methodの`launchd/`ディレクトリにrepo管理されているが、**videoだけrepo内に
plistが無く**、`~/Library/LaunchAgents/`に手動インストールされた状態(SSOT違反、要修正)。

## 2. TIER 2: ClawRouter (self-funded) — 単一 genesis daemon

`~/Library/LaunchAgents/com.anicca.daemon.plist`(`KeepAlive=true`)が`~/anicca/anicca-daemon.sh`を
起動 → ①Life Manager releaseを検証 ②repo内skillsを直接参照
③ClawRouter(x402課金router、無料モデル時$0)を:8402で起動 ④`node runtime/loop/index.mjs`をforeground
でexec(無限loop)。

### 1 wakeの処理フロー
```
USDC残高取得 → tier決定(brain modelの格)
  → 直近ledger 20行 + genesis.md人格プロンプトでコンテキスト組立
  → LLM(既定 free/glm-4.7、$0)に run_skill({slot,args}) ツールを提示
  → slot選択をパース → $LIFE_MANAGER_REPO/skills/<slot>/run.sh を子プロセス実行
  → 結果をon-chain実績から profitable 判定 → ~/.anicca/state/ledger.jsonl に1行追記
  → sleep(既定120秒) → 繰り返し(loop-detect.mjsが同一slot連発を防ぐ)
```

### 利用可能 slot 一覧(`skills/registry.json` status=live のみLLMに提示)
| slot | 実体 | 備考 |
|---|---|---|
| report | `skills/report/` | 毎wake、AgentMail+telemetry送信 |
| self/issue-dev | `skills/self/issue-dev/` | ★self-heal本体、下記3節参照★ |
| cook | `skills/cook/` | 新しいearn手法をweb探索 |
| yield / hl_trade / x402_sell / token_launch | `skills/earn/`(共通run.sh、strategy引数で分岐) | 各種money戦略 |
| earn/gig, earn/clip, earn/video, earn/bounty | `skills/earn/<name>/` | claude-p側と**同じコード**、`ANICCA_INSTANCE=clawrouter`で分離 |
| earn/pm-trade, earn/defi-yield | (2026-07-04時点でregistryから削除済み、"wiring"段階に後退) | 独自strategy実装が本日削除され開発中に戻った |

## 3. self-heal / self-improve の仕組み(Sutando由来)

Sutando(`github.com/sonichi/sutando`、実在のBP。"Realtime by Day, Rewriting Itself by Night")から
移植したのは**peer-registry(生死監視)・resurrection(復活)・bot2bot(エージェント間通信)**の3機構
(`specs/22-REF-SUTANDO.md`)。self-improve自体はAnicca独自設計(`specs/18-SELF-IMPROVEMENT-AND-SWARM.md`)。

`skills/self/issue-dev/run.sh` が実装済み(SLOT.mdの"declared"表記は更新漏れ):
1. 自分のledger(reverted tx `status:0x0`、または直近12件中`loop_detect`3回以上)を読む
2. 問題があれば `gh issue create -R Daisuke134/anicca` で母リポジトリにIssue1件を起票(重複防止あり)
3. Issue本文: "Fix the MOTHER so every anicca inherits it"

★ **重要な制約(未解決)**: Issueを自動でPR化・merge・全体展開する「forum-rollout」パイプラインは
`docs/superpowers/plans/`に計画書があるのみで、**実装コードは存在しない**。つまり現状は
「Issueが立つところまでは自動、その先(誰かがPRを書いてmergeする)は人力」。これはTask化して
別途解決すべき欠落(母リポジトリの自動修正ループが未完成)。

## 4. CronCreate の正体(2026-07-04 コード調査で確定)

各`*-cli.sh`内で呼ばれる`CronCreate`/`CronList`/`CronDelete`は**Claude Code CLIネイティブの
cron機能**であり、`.claude/scheduled_tasks.json`に永続化される(`durable=true`指定時)。
**`~/.openclaw/cron/jobs.json`(OpenClaw gateway管理)には登録されない** — 全文検索で該当cron文言
(`7 * * * *`等)は1件もヒットしなかった。両者は完全に別システムなので混同しないこと。

## 5. 実インシデント記録(このファイルを作るきっかけ、2026-07-04)

1. **disk cleaner 3重問題**: `com.anicca.disk-cleaner`(1h毎)/`ai.anicca.disk-janitor`(5分毎)/
   OpenClaw cron`anicca-disk-hourly`(10分毎)が同じ`~/.cache/anicca-clones`を別ロジックで掃除し、
   producer.shのengine venvを繰り返し破壊。→ `~/scripts/disk-cleaner.sh` v9に1本化、他2つ無効化。
2. **tmuxソケット消失→4loop重複起動**: clip/affiliate/video/bounty全てで`/tmp/anicca-*-tmux.sock`
   が消失、5分毎healthcheckが「死んでいる」と誤判定して重複起動(計8プロセス、Load Avg 8.99)。
   孤立プロセスをkillして解消。原因は未特定(Task #7)。**gigのbackoffパターンを他4つにも展開すれば
   同種の暴走は起きない**。
3. **ClawRouter用producer実行経路の欠如**: `ai.anicca.clip-producer.plist`にANICCA_INSTANCE設定が
   無くclaude-p専用だった為、ClawRouterはearn/clipを75回選んでも常に空振り。
   `ai.anicca.clip-producer-clawrouter.plist`を新規追加。

## 6. 運用コマンド早見表

```bash
# 全claude-p loopの生死確認
for s in clip affiliate video bounty gig; do tmux -S /tmp/anicca-$s-tmux.sock list-sessions; done

# healthcheckログ確認
tail ~/.openclaw/logs/{clip,affiliate,video,bounty,gig}-core-healthcheck.log

# ClawRouter genesis daemon確認
launchctl list | grep anicca.daemon
tail ~/.anicca/state/ledger.jsonl

# 重複プロセスが無いか(名前ごとに1プロセスのはず)
ps aux | grep "tmux -S /tmp/anicca-.*-tmux.sock new-session" | grep -v grep
```

## 7. Task Backlog(このドキュメント作成時点でpending)

- Task #6: ClawRouter用producer経路のE2E確認
- Task #7: tmuxソケット消失の根本原因調査 + gig方式のbackoffを他4loopへ展開
- Task #8: CloakBrowser cookie復号失敗(Keychain "Chromium Safe Storage"アクセス不可)の根本解決
- 新規: video methodのlaunchd plistをrepo管理下に戻す(SSOT違反の是正)
- 新規: forum-rollout(Issue→PR→merge→全体展開)パイプラインの実装(現状Issue起票止まり)
