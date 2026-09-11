# Life Manager execution order

現在のone-repo / two-runtime統合作業と固定TODO順序の正本は
[`docs/superpowers/specs/2026-09-06-life-manager-one-repo-two-runtimes-design.md`](superpowers/specs/2026-09-06-life-manager-one-repo-two-runtimes-design.md)
です。進捗、blocker、次のatomはそのspecだけを更新します。

## 実行原則

1. `main`を唯一のsource of truthとする。
2. 最新`main`由来の専用worktreeで先頭の未完TODOだけを実装する。
3. loop定義、adapter、prompt、schema、testはLife Manager repo内に置く。
4. LocalとCloudでbusiness workflowを複製せず、host adapterだけを切り替える。
5. provider credential、KYC、browser loginが足りなければ`setup_required`として安全に待つ。
6. provider receipt、runtime event、Telegram message IDで結果を確認する。
7. acceptanceを満たした変更だけをreview、PR、main統合し、main由来immutable releaseから反映する。
8. 完了したtask worktreeはowner、clean status、merged ancestry、open PR、open handleを確認して退役する。

## 現在使わないarchitecture

OpenClaw cron、Hermes gateway、ClawRouter daemon、repo外skill tree、別checkoutのsource、旧tmux earning
fleet、Docker Compose local runtimeは現行実行経路ではありません。過去の設計はGit履歴と日付付きspec・
evidenceに残しますが、現行TODOや起動手順として使いません。

## 現在の入口

```bash
# catalog / status
jq -r '.loops | keys[]' config/loop-registry.json
./bin/lm-loop status all
./bin/lm-loop doctor

# daemonを起動しないclean local setup
LIFE_MANAGER_INSTALL_DAEMON=0 ./install.sh
```

14 Product Loopsの意味、必要なユーザー設定、現在の開始入口は
[`README.ja.md`](../README.ja.md#14本の主要product-loop)を参照します。
公開repositoryは <https://github.com/Daisuke134/life-manager> です。
