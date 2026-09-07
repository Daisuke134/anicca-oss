# 12 Loops — 実測ステータスと TODO（2026-09-03 audit）

実測日: 2026-09-03。全数値は ledger / log / state file から直接読んだもの（推測なし）。
北極星: Life Manager = 財務的に自立した AGI。12 loop 全部が human-loop 最小で金を刷る。
方式: Fable が設計、Sonnet subagent が実装、loop 自身が実行（orchestration 固定）。

## 総収益（検証済みの実金のみ）

| 出所 | 金額 | 最終確認 |
|---|---|---|
| Coconala | **¥129,636 累計**（¥5,460 payout 申請 8/20、6件受注 / 806応募） | revenue-collect.log（8/15以降更新停止） |
| Capafy (mobile) | $19.98 gross / 5注文、$8 payout未着金 | 8/22 で ledger 停止 |
| Lancers | ¥0（応募60件検証済み、受注0、問合せ2件） | contracts.json 9/3 |
| CrowdWorks | ¥0（応募2件、8/11以降ゼロ） | receipts 9/3 |
| Crypto wallet | **-$18.65**（$18.7投入 → $0.05残） | portfolio-realtime 9/3 |
| その他全部（writer/affiliate/stripe/the402/x402） | ¥0 検証済み収益なし | 9/3 |

**直近3週間、新規の検証済み入金ゼロ。「動いている」と「稼いでいる」が乖離している。**

## Loop別ステータス（あるべき姿 → 実測 → 差分）

| # | Loop | 判定 | 実測 | 根本原因 / 差分 |
|---|---|---|---|---|
| 1 | Gig: Coconala | 🔴 出品全休止 | 4 lane稼働中、応募継続（9/2最新）、¥129,636 | revenue collector が 8/15 から停止 → 今の残高が見えない。paid/storefront lane が間欠 fail |
| 2 | Gig: Lancers | 🔴 応募停止中 | 6 job稼働だが application lane が毎tick `planner_contract_invalid`。今日 fresh判断0件 | `application_loop.py` の `_validate()`: observed 33件のうち1件でも budget_min/max_minor 不正だと batch全体を ValueError で捨てる設計。1行の毒で全滅 |
| 3 | Gig: CrowdWorks | 🔴 8/11から死亡 | application exit 1、`account.json` が 8/11 から `input_required` | credential 未投入（旧 hours_limit 説は取り消し） |
| 4 | Writer | 🟡 書けるが測れない | 記事公開は継続（9/1 run あり）。sales-ledger は 8/22 から `ok:false` 連発 | Note/Substack の売上計測が壊れ、収益検証不能。article-daily exit 75 |
| 5 | Affiliate | 🟡 投稿するが¥0 | X投稿は今日も稼働。毎cycle `NO_REVENUE_CREDIT` | Amazon Associates の成果が一度も confirm されず |
| 6 | Investment (Alpaca) | 🔴 blocked | `alpaca_pass_failed` を毎cycle。pm-live-trade は $2.05 で HOLD（最小 $5 未満） | Alpaca 認証/pass 失敗。hackathon 提出は paper のままで可能 |
| 7 | Agent economy / crypto | 🔴 8/28から停止 | franklin1: git checkout 衝突で毎cycle abort。franklin2: proxy 429。daemon SIGTERM 後未復帰 | net -$18.65。「財務自立 AGI」の土台が止まっている |
| 8 | Job hunter | 🟡 Workday only | 今日の run `runner_failed`。Ashby/Greenhouse/Lever は README 自認で未完成 | remote + Tokyo の一般求人に未対応（Dais の要求と乖離） |
| 9 | Fundraiser | 🔴 8/31から死亡 | disk full (Errno 28) 事故で停止、復帰せず。accelerator 応募実績の証跡なし | disk は回復済み（19Gi free）なのに loop が再起動されていない |
| 10 | Connector | 🟢 生存 | 今日も発火、Telegram 配信あり | Luma/Connpass/Peatix のみ実証。唯一「金を直接産まない」loop（設計通り） |
| 11 | LM Cloud (web) | 🟡 課金ゼロ | stripe listener/poller 今日も稼働、**新規 charge 0 件**。selfbuild は動くが `no_verified_award` | 製品は生きているがユーザー/課金がいない。QR onboarding → X で配布が未実施 |
| 12 | Mobile (Capafy) | 🟡 停滞 | $19.98 で 8/12 から売上なし、ledger 8/22 停止 | 新規販売導線なし。postiz/自前 marketing 未接続 |
| - | Ebook | ⚫ 存在しない | plist も pipeline もなし | 作るなら新規（優先度は上記の後） |

## 到達点と順序（2026-09-07 Dais 指示）

目標は 10M MRR。そこへの形は3段で、飛ばせない。

**第1段 — 3プラットフォーム × 4レーンを全部動かす。**
Storefront と Apply は択一ではない。同じプラットフォームの別レーンで、両方が同時に動く。
Negotiate と Paid はその2つが生んだ会話と注文を閉じる。4レーンが揃って1プラットフォームが完成する。

**第2段 — 新プラットフォームを1本、手順書だけで追加できるようにする。**
判断・契約封印・fence・読み戻し・需要スコアリング・カタログ投影は共有部品に入った。
残るのは「出品フォームを観測して adapter を書く」ことだけで、それは判断であってモデルの仕事。
ココナラでカテゴリ3階層を観測して封印・公開したのと同じ構造で、人が selector を書かない。

**第3段 — メタループ。**
毎日マーケットを探し、規約を読んで我々のやり方が許されるか判定し、通れば口座を作り
（KYC が要れば Telegram で1タップ依頼）、フォームを観測して adapter を書き、カタログを投影して
公開し、売上を計測する。読んで記録するだけなら調査であってループではない。**探して、作って、稼ぐ。**

### 12レーンの実測（2026-09-07 15:41）

| | Apply | Storefront | Negotiate | Paid |
|---|---|---|---|---|
| ココナラ | ✅ 09:22 | ✅ 15:38（本日4本を自力公開） | ❌ **8/27 から停止** | ✅ 15:36 |
| ランサーズ | ⚠️ `_validate()` で batch 全滅 | ✅ 15:41（共有カタログ接続済） | ✅ 15:41 | ✅ 15:41 |
| クラウドワークス | ✅ 15:22（受理19件） | — **棚が存在しない**（認証済セッションで確認） | ✅（旧方式） | ✅ |

停止2件: ココナラ Negotiate（`gig-reply-detector` が 8/27 23:20 で沈黙）、ランサーズ Apply。
クラウドワークスの Storefront 欄は欠落ではない。受注者は「探す→提案→契約」で棚を持たないため、
このマスは埋まらないのが正しい。詳細と根拠 → `skills/loop-engineering/references/platform-automation-map.md`。

### 律速（技術ではない）

Storefront 由来の売上は **0円**。30日で閲覧492・購入0。これまでの売上8件はすべて Apply 由来。
需要実測が理由を言っている: `excel_vba` 分野は ¥29,000/レビュー464件が売れ、当方は
¥5,000〜7,000/レビュー0 で売れない。**売れている条件は価格ではなくレビュー数。**
プラットフォームを10に増やしてもレビュー0の棚が10個並ぶだけなので、第2段より先に最初の1件を売る。

## TODO（この順。順序 SSOT — Dais 明示なしに変更禁止）

順序改定 2026-09-04: Dais 明示指示「まず Coconala 出品(storefront)を直す → Lancers 完全プロフィールで応募 → CrowdWorks」。#1〜#3 を gig 3 platform で固定、以降は 9/3 順を維持。
各項目の DONE 条件は「コードが直った」ではなく「**外形的な実測 evidence が出た**」。evidence 無しで次へ進まない。

0. **全 loop の LLM provider を Claude Sonnet に統一** — Dais 指示。棚卸し 2026-09-04 実測:
   - 変更点は 1 箇所: `runtime/agent-runner/config.json` の `task_classes.*.candidates`（gig 4 lane / job-search / capafy / alpaca / marketing が共有）。Writer は `model-runner.sh` で既に claude。franklin×2 は別 repo（対象外）。
   - 現状: ほぼ全 class が `codex@acct2` 第一。claude fallback 無し 14 class（reply-semantic / application-intent-planner / marketing / diagnostic / composition / tool / repeatable / high-value / affiliate×2 / writer-repair 等）は `transient_quota` で死亡中（capafy・alpaca・reply-detector で実測）。
   - 罠1: deployed release `20260904T010225-659deab7` の config は repo と drift（planner の claude-direct fallback が deployed では消失）。修正は release 経路で出荷しないと効かない。
   - 罠2: claude leg は配線だけでは通らない（job-search で claude fallback が `validation_or_task_failure` rc=1、storefront で 160s timeout）。`claude` provider = cli-proxy `:8317` 経由、`claude-direct` = 素の CLI。どちらを既定にするかは storefront 修理の実測結果で決める。
   - `skills/earn/gig/agent-runner/` は未使用の複製（`gig_paths.py:21` は runtime 側を指す）→ 削除候補。
   手順: storefront の claude 経路が実測で通る → その provider 設定を全 class の第一候補に、codex は fallback（9/7 まで死）→ release 出荷 → 各 loop の次 wake attempts で `provider=claude rc=0` を readback。
   DONE: 全 loop の直近 wake の attempts で provider=claude rc=0。codex 依存で落ちる loop が 0。
1. **Coconala storefront 復活 → 出品カタログ化（最重要・最も汎用）**
   Dais 方針: 出品は 4 lane で最も汎用。サービス自体にレビューが蓄積し recurring になる。上限 20 本を「上手くいっている競合の出品を見て写す」。特化はシステム開発（0→1 開発、Web/アプリ、修正）。出品 asset は platform 非依存で共有し、skill で定義して Lancers/CrowdWorks へもそのまま流す。
   進捗 2026-09-04:
   - a. **DONE** — 14 件 `受付休止中` → `公開中`（commit `665bd1acd`、effects.jsonl reopen 14 行、readback 受付休止 0）。根本原因: 一覧 scraper が `受付休止中` を読めず `state:None`、contract 検証が 14 件を毎 wake 捨てていた。
   - a'. **DONE** — `listing_contract_family_missing:4371816`。repo 外 state `~/gig/private/storefront-bundle/families.json` の family 欠落を復元。
   - b. **原因を訂正（2026-09-06 実測）** — `storefront_create_proposal_failed`（#0 の provider 問題）ではない。18:21〜20:13 の full wake 12連続が effect 0 で `failed`、同時間帯に `gpt-5.6-terra` へ 21 回到達している。落ちていたのは guard 拒否: `storefront_copy_names_prohibited_tool:スプレッドシート` と `storefront_create_title_stem_not_continuative`。
     根本原因は guard ではなく拒否の扱い。IMPROVE 経路は `_seal_generated_proposal` の例外を catch して no-op に縮退していたが、CREATE 経路は `_seal_create_contract` を素で呼び wake ごと死んでいた。さらに拒否理由はどちらも捨てられ、次 wake が同じ context から同じ違反を再生成していた。
     **DONE（PR #4222、main `3244ca535`）** — 拒否を `proposal-rejections.jsonl` に gap 単位で永続化 / CREATE 経路を catch して no-op receipt に縮退 / 両プロンプトへ直近拒否を差し戻し / 禁止語を `PROHIBITED_COPY_TERMS` から、連用形規則を新設 `TITLE_STEM_CONTINUATIVE_ENDINGS` からプロンプトへ注入（guard と定数を共有し drift 不可）/ 同一 guard 3連続で当該 gap を打ち切り。新規テスト 13 本 PASS、`test_storefront_direct.py` の既存 2 失敗は clean main と同一。
     release `363b78ce` を storefront label のみに apply 済み（2026-09-06 21:21、他レーン plist は不変）。**ただしこの修正はまだ一度も発火していない** — 到達前に別の理由で落ちるため、効くかどうかは未証明。
   - b'. **二階層カテゴリを raise せず報告する** — 21:31:51 の wake が `storefront_category_type_absent:813` で失敗。deploy 前の 20:10:03 にも同一理由で落ちており既存欠陥。
     実測: sub `813` で4回・sub `361` で2回発火。category-child エージェント結果12件（sub 813/237/231）で**一度も本物の type 値が取れておらず**、返り値は `0`×7・`686`×2・`000000`・`00000000`・`231`。ある rationale は「公式カテゴリタイプの候補は提供されていないため」と明記。観測は常に `master_category_type_id:1D`（値なしプレースホルダ1個・disabled）で、同フォームの `fix_limit:13D` `proposal_limit:13D` も disabled。
     決め手: 書き込み側は既に二階層を正式サポート（`storefront_draft.py:143` / `:336-341` の `# Coconala offers only two levels in some categories.` / `:476`）。reader だけが観測した形を報告せず raise していた。
     過去の地雷: 「空リスト = 二階層」と推論して early return した修正が、公開を毎回 `カテゴリタイプを正しく選択してください` で弾かせた（`3f5b4848e` が raise に戻した）。よって12秒待ちは不変、**enabled かつ空は従来通り raise**、**disabled かつ空のときだけ**二階層と報告する。select 不在も従来通り raise。
     **DONE（PR #4237、main `ef82677d7`）** — reader が `master_category_type_absent` を返す / 両呼び出し側が type エージェント呼び出しを飛ばし `category["type"]=None` にして分岐を記録 / schema が `type_value: null` を許可 / 推論が外れた場合に備え公開拒否を専用の `storefront_publish_category_type_rejected` として立てた。新規テスト 7 本 PASS。release `20260906T215357-ef82677d` を storefront label のみに apply（21:54、他レーン plist 不変）。
   - b''. **競合ページの空読みで wake を殺さない** — 21:40:05 の wake が `competitor_source_empty` で失敗。実測: 21:35:59 の wake は 14 件中 **9 件**しか evidence を書けずに死亡、前後の wake（21:22 / 21:09 / 20:54 / 21:44 / 21:55）は全て 14 件読了 → 一過性の空読み。
     既存慣行に合わせた: `_read_official_catalog` は同じ理由で dashboard を5回 retry し「failing the whole wake on it costs a decision cycle for nothing」と書いてある。
     **DONE（PR #4239、main `3d737e7e0`）** — 空 body だけ5回 retry（`attempt<4` の3秒 sleep も既存と同一）/ `competitor_source_is_own_service`・`competitor_source_not_official`・`competitor_service_redirected` は初回で raise のまま（ページ自体の正しさの話で環境の話ではない）/ retry 後も空なら manifest の新 `unread` に記録して skip、`sources` に入れないので evidence count は正直なまま / **閾値は発明せず、読めた source が 0 のときだけ致命**。新規テスト 7 本 PASS。
     **DONE（2026-09-07 08:25）** — 一晩で15件出荷。うち7件は**同じクラス**だった: 回復可能な1件の失敗が wake 全体を殺す。1件ずつ直して13段かかったのは私の誤りで、3段目でクラスを名指して全箇所を一度に潰すべきだった。#4374 でまとめて閉じた。
     出荷分: #4222 guard拒否をwakeの死因にしない / #4237 二階層カテゴリ / #4239 競合空読み / #4245 タブopen timeout / #4250 schema の `oneOf`（自分の回帰） / #4269 3ストライクが実物の下書きを捨てる（自分の回帰） / #4272 seller form の retry が最弱 / #4278 プロンプトが助詞の規則を教えていない / #4280 公開ページ読み戻しの retry / #4287 prepare_draft の retry 不整合 / #4294 **下書き作成が wake の effect 予算を食って公開を止めていた** / #4307 RETIRE をモデル判定へ / #4308 共有カタログ / #4366 ログイン画面を「空の棚」と誤命名 / #4374 per-item 失敗クラスを一括 / #4376 封印済み契約を捨てて毎回作り直していた件
     失敗の分類規則は `skills/loop-engineering/references/transient-vs-fatal.md` に切り出した。新しい raise を足す前に必ず読む。
     **7時間の停止**: 00:51〜08:09 の 241 wake が completed 0。原因はセッション切れをループが `official_inventory_empty_or_invalid`（在庫が空）と誤って名指したこと。Apply lane は同じ失効を正しく名指したので次 wake で自力復帰した。**差はコードではなく名前だけだった**（#4366）。
     **残り（1b の完了条件）** — full wake が `effect 1 / readback 1` で公開到達するのを本番実測する。2026-09-07 08:48 時点で未達。下書き2件（`4387924` LINE予約・定型応答 ¥100,000 / SEO記事 ¥3,000）が中身入りで公開待ち。直近 wake は `no_executable_unfenced_mutation_contract` — 落ちてはいないが下書きを公開候補として拾えていない。
   - c. **競合調査 → 出品カタログ 20 本** — Coconala「システム開発・制作」「Web/業務システム」「AI」上位出品（売上件数・星5）を lane 既存の competitor 観測（`competitor-*.json`）で収集し、title/価格帯/構成/FAQ の共通パターンを抽出。雛形は `~/gig/applied.jsonl` 高単価案件（¥300,000/¥250,000/¥180,000）。asset は `skills/gig-work/profile/listings/*.json`（platform 非依存: title/body/価格tier/納期/FAQ/画像）に置き、skill で「出品 asset の作り方・流し方」を定義。
   - c'. ~~**成功出品データは既に収集済み。IMPROVE に渡っていないだけ**~~ **DONE（#4406）** — `family_market()` が需要台帳から公開事実だけを両判断プロンプトへ渡す（中央値 / 販売実績数 / レビュー済み数 / 検索結果数 / evidence path、各比較対象は価格・評価・レビュー数のみ）。クエリ文言も理由文も他者の言い回しも渡さない。指示を「知識の禁止」から「表現の禁止」へ変更: 他者の文言・画像・レビュー本文・主張・実績の複製は禁止、観測された分布は公開事実として使用必須、価格提案は現在価格ではなく分布に対して根拠を示す。コードは価格を計算しない。実測 `29000`/`464` がプロンプトに入ることを変異テストで固定。
     以下は当初の記録:
   - c'-orig. **成功出品データは既に収集済み。IMPROVE に渡っていないだけ（2026-09-06 実測）** — `_extract_search_demand`(`storefront_direct.py:1370`) が公式検索から `comparables`（`display_price_jpy`/`rating`/`review_count`）を作り、`_demand_score`(1430) が `median_price_jpy`/`sold_comparables` を出し `demand-evidence.jsonl` に残している。実測: `excel_vba_gas_automation` = ¥29,000/レビュー464件・¥3,000/428件（当方の Excel 3 件は ¥7,000/¥6,000/¥5,000 で販売 0）、`line_bot_dev` = median ¥35,000・12 件全て星5・検索結果 1,657 件（当方に該当出品なし）。
     この構造化データは CREATE 経路（`_create_proposal_prompt` 3974）にしか渡らない。既存 15 件を支配する IMPROVE 経路 `_proposal_prompt`(3798) は競合ページ本文を 8,000 字に切って渡すだけで、プロンプトが「never copy their wording, images, reviews, sales, speed, guarantees or results」と使用を禁じている。
     直す場所: `_proposal_prompt`(3798) と `_judgement_prompt` の CONTEXT_JSON に当該 family の `median_price_jpy`/`comparables`（価格・評価・レビュー数のみ）/`sold_comparables`/`visible_result_count` を追加し、禁止文を「表現・画像・実績の複製は禁止。観測された価格・評価・レビュー数の分布は公開事実として使用してよい」へ変更する。
     DONE: IMPROVE 提案の evidence に demand-evidence の path が入り、価格提案が family median を根拠に説明され、公式読み戻しで価格が確認できる。
   - d. **公開** — カタログから順に公開（上限 20）、各 wake で公開状態と購入数を readback。
   - e. ~~**重複出品を畳めるようにする（RETIRE が構造上発火不能）**~~ **DONE（2026-09-07 09:01 本番実測）** — モデル判定へ置き換え（#4307）。`duplicate-listings.jsonl` に 8/18 以来はじめて新規行 `['4313386','4357844']`（理由: 両方とも Excel/VBA の定型転記自動化）。`4357844` が公開棚から消え 15 → 14 件。difflib の 0.9 では実測 0.533 で永久に発火しなかったペア。残りは SNS 系 5 件と Excel 残り 2 件で、1 wake 1 効果なので順に落ちる。復元の実測は未。
     以下は当初の記録:
   - e-orig. **重複出品を畳めるようにする（RETIRE が構造上発火不能）** — 2026-09-06 実測: 公開 15 件は全て `sales_count 0`、30 日 views 441。うち 8 枠が 2 アイデアの反復（SNS 系 5 件 `4244556/4244912/4302213/4330105/4330753`、Excel 系 3 件 `4244910/4313386/4357844`）。
     `_near_duplicate_listings`(`storefront_direct.py:2137`) は 2154 行の `ratio >= 0.9` でしか重複を認めないが実測最大ペアは **0.857**。もう一方の経路も 958 行 `capacity_pressure` が `15 >= 20` = false。結果 985 行 `retire_ready` が全 15 件で false → 6344-6399 の実行器が本番で死んでいる。
     直し方: 閾値を上げるのではなく計器を替える。difflib を捨て `storefront-proposal-agent` に生カタログを渡して「買い手が代替品として比較する組」を判定させ strict schema で封印する。決定論コードはアーカイブ操作・読み戻し・復元・単一 effect fence を握り続ける。0.85 へ下げる案は棄却（正当に別物の出品を畳み始める）。
     DONE: 重複組が `duplicate-listings.jsonl` に新規追記され、1 件が非公開へ落ちて公式読み戻しで確認でき、次 wake で重複 effect 0、復元も実測できる。
   DONE: 公開 ≥ 15 件、うちシステム開発系 ≥ 5 件が公開 URL で readback、wake exit 0、replay effect 0。
   実測の棚と根拠 → spec `docs/superpowers/specs/2026-08-16-storefront-loop-ssot.md` 4A 節。
   - f. **共有カーネル抽出（項目13の前提）DONE（#4394）** — `skills/_shared/marketplace-core/scripts/storefront_kernel.py` に判断の中核を移した: 選定(KEEP/IMPROVE/RETIRE/REPLACE)・契約検証と封印・需要抽出とスコアリング・REPLACE 計画・進行中の下書き・契約回収・拒否台帳とガード同一性。platform も禁止語リストも引数で、カーネルに `coconala` の文字列は無く `skills/earn/gig` を import しないことをテストで固定。Apply と Paid が既に持つ構造に Storefront が並んだ。ランサーズ/クラウドワークスの接続は項目13。
   - g. **1b が止まっていた本当の理由（2026-09-07 実測）** — CREATE ゲート5条件はすべて開いていた（`blocked_by: []`）。止めていたのは私が #4222 で入れた3ストライク規則で、`create:line_bot_dev` の3件はいずれもプロンプト修正(#4278/#4376)より前の古い証拠だった。打ち切りに解除が無く、下書き `4387924` は永久に埋まらない状態だった。#4409 で wake が5条件を自己申告するようにし、本 PR でストライクを「稼働 release より新しい拒否だけ」で数えるようにした（修正の出荷が解除になる自己修復規則）。

   - h. **セッション切れの自己復帰（2026-09-07）** — 検知は #4366 で入ったが、そこで止まっていた。`session_vault.py:586` の `relogin_coconala()` は**既に完成していた**（ログイン実行・vault への保存・cooldown 付き）のに、storefront からの呼び出しが **0箇所**。だから 00:51〜08:09 の 241 wake がセッション切れを報告しながら人手を待った。検知した wake が1回だけ呼び、返答をそのまま記録するよう接続した（成功 / cooldown / 失敗＋理由 / 利用不可 を区別、例外は wake に伝播させない、復帰しても失効の事実は隠さない）。
     **教訓**: 復帰機能は既にあったのに、私は「作る」と提案した。Dais の指摘の通り、**確認せずに提案し TODO を更新していなかった**のが原因。実装済みのものを探してから提案すること。

1'. **Coconala paid lane: 全 client に返信・提出** — 実測未（Dais 報告: 一部 client に返信/提出していない、取りこぼしあり）。paid lane の state で「未返信 client 数」「未提出 見積り数」を実測し、取りこぼし 0 にする。
   DONE: paid lane の wake summary に unanswered_clients=0、未提出 0、かつ実際の返信 receipt ≥1。
2. **Lancers 応募復旧 + 完全プロフィール応募** — `application_loop.py:320-350` `_validate()` が 1 行不正で batch 全滅（`planner_contract_invalid`、9/4 も継続、今日 fresh 判断 0）。不正 row は skip、健全 row だけで判断へ。profile は 9/4 に avatar 登録で 90%（残り電話認証のみ、blocker にしない）。
   DONE: 次 wake で `error` 消滅・`eligible_count > 0`、`application_verified` 60 → 61 以上。
3. **CrowdWorks 復旧** — **実態を訂正（2026-09-07 実測）**: 認証は通っている（`status` が `authenticated`/`role: employee` を返す）。8/11 からの `input_required` は state ファイルが古いだけだった。停止の真因は5段で、いずれも今日剥がした: ①`security find-generic-password -w` が GUI 解錠待ちで永久ハング（手元で2分ハングを再現）。launchd では誰も応答できない ②vault モジュールのパスが repo 外前提で `~/.local/_shared/...` を指す（実際は `skills/browser/scripts/`）③`spec_from_file_location` で同階層が sys.path に入らず `target_ownership` の import 失敗 ④クッキー120個中16個が旧 Chrome 形式の文字列 `partitionKey`。`setCookies` は全か無かなので全体が復元不能（**#4318、repo・全レーン共通**）⑤`session_vault` が実行中イベントループ内で `asyncio.run()`（**#4359、repo・全レーン共通**）。
   **アーキテクチャが二重（未解決）**: `ai.anicca.crowdworks.{acquisition,negotiation,fulfillment,storefront}` は repo 外 `~/.local/share/anicca/crowdworks-revenue-skill/lane_runtime.py` の旧方式で、storefront は 8/15 凍結・`fixed_service_listing_not_supported`（プロフィール確認のみで出品しない）。`ai.anicca.crowdworks-revenue-{application,paid,report}` は repo 内の新方式だが **storefront が存在しない**。つまり現行アーキテクチャにクラウドワークスの出品機能は無い。CrowdWorks 側にはパッケージ（固定価格出品）があるのでプラットフォームの制約ではない。
   repo の `account.py` は既に SSOT を読む新版で、動いている repo 外の版が古い。**repo 外の版を repo へコピーしてはいけない**（退行する）。正しい順序は repo 版へ label を差し替え、`lane_runtime.py` の storefront 相当を新方式で作ること。
   **出品できない理由の切り分け（2026-09-07、自分の公開プロフィールを一次情報として観測）**:
   `https://crowdworks.jp/public/employees/7145638` の実測 — `Kaito｜AI自動化`（ココナラの `Kosuke` とは別ペルソナ）、**本人確認 未提出**、NDA未締結、インボイス発行事業者未確認、完了数 0 / 契約数 0、時間単価 3,000〜5,000円、登録 2026-08-11。公開プロフィールのタブは サマリー / 評価実績 / 職種・スキル / ポートフォリオ・経歴 / 回答・相談履歴 / ランキング のみで、**出品（パッケージ）タブが無い**。
   旧レーンの `fixed_service_listing_not_supported` は `https://crowdworks.jp/user_skills`（**スキルタグのページ**）に価格入力欄が無いことを根拠にしている（`provider_sources.py:9` の `_ROUTES["storefront"]`）。スキルページに価格欄が無いのは当然で、**この判定は見ているページが違う**。ココナラで「ログイン画面を空の棚」と誤命名したのと同じクラス。
   **確定（2026-09-07、認証済みセッションで観測）: CrowdWorks に受注者向けの固定価格出品は存在しない。**
   認証済みの `https://crowdworks.jp/profile?role=employee` にある導線は スキル登録・スキル検定（`/user_skills`）／支援サービス／AIクラウドワークス新規登録 のみ。サイトの受注者向けリンクは `/e/proposals`（提案）と `/e/contracts`（契約）だけ。`/package_offers` と help centre の該当ページは 404。
   受注者モデルは「仕事を探す → 提案する → 契約する」であり、ココナラのように棚へ並べる出品が無い。**旧レーンの `fixed_service_listing_not_supported` という結論は正しかった**（根拠にしていたページが誤っていただけ）。
   **したがって CrowdWorks に storefront は作らない。** 収益経路は Apply で、それは既に動いている: `application-receipts.jsonl` が 19 行、直近2件とも `status: verified`、最終 2026-09-07 14:12。本人確認も 2026-09-07 に承認済みになった。
   **残る作業は Apply 側**: 最新の `application-owner.json` が `submission_uncertain`（`project_id: 13423472`）で1件不確定。応募したか否かを確定できないまま終わる経路を閉じること。
   **Dais 側の項目**: 本人確認が未提出。KYC は Dais が行う唯一の人手工程として明示されている。CrowdWorks の出品が本人確認に紐づく場合、ここが開くまでこのレーンは収益化できない。

   旧記載: — 実測: `account.json` が 8/11 から `status: input_required`（credential 待ち）で application lane exit 1。9/3 に書いた `hours_limit` 文字列説は repo/state に該当ファイル無し（**誤りとして取り消し**）。credentials.json SSOT から再ログイン → 4 lane を launchd に bootstrap。
   DONE: `application-receipts.jsonl` に 8/11 以降初の receipt 1 件。
4. **profile readback を loop 化** — 3 platform の公開プロフィールを定期 readback し完成度を state に記録（Lancers storefront lane は 9/4 から `profile_completion_percent` を返す。Coconala/CrowdWorks は未）。
   DONE: `~/.local/state/anicca/*/profile-readback.json` が 2 回目 wake でも更新。
5. **Coconala revenue collector 復旧** — `~/gig/earnings.jsonl` 最終行 8/12。専用 plist 無し。
   DONE: 本日日付の行が入る。
6. **Fundraiser 再起動** — 8/31 disk 事故停止、未復帰。 DONE: accelerator 応募 1 件の受領証跡。
7. **Agent economy 復旧** — franklin1 git 衝突 / franklin2 proxy 429 / daemon 未復帰。 DONE: 両 franklin が wake 完走、ledger に本日行。
8. **Writer 売上計測復旧** — DONE: `sales-ledger.jsonl` に `ok:true` 1 行。
9. **Job hunter 拡張** — `runner_failed` 修正 → remote + Tokyo 一般求人。 DONE: 応募 1 件の受領証跡。
10. **LM Cloud 出荷** — QR onboarding → X 配布 → Stripe 初 charge。 DONE: `new charges: 1`。
11. **Alpaca 修復 + hackathon 提出** — DONE: 提出受領。
12. **Capafy 販売再開** — postiz self-host 含む marketing 接続。 DONE: 新規注文 1 件。
13'. **ランサーズにカタログを売らせる残作業（2026-09-07 実測）** — 配線は #4466 で完了、納期の写像は #4500 で完了（18日→21日、25日→30日。カタログ本体は無変更なのでココナラ側は不変）。
    **残る本当の障害はティア数**: カタログ20 family のうち **18 family がティア2つ**で、ランサーズの product validator は**ちょうど3プラン**を要求する。3ティアを持つのは `mvp_web_app_build` と `ai_agent_integration` の2つだけ。
    つまりランサーズで売れるのは現状この2 family だけで、残り18を売るにはカタログに3つ目のティアを足す必要がある。これは価格設計の判断であってコードの問題ではない。
    ついでの記録: 私はこの修正で `test_catalog_projection_that_fails_product_validation_names_the_catalog` を壊し、**赤いまま出荷した**。18日納期を実例に使っていたテストが、18日が通るようになって前提を失った。実例を2ティア family へ差し替えて復旧（#4500 の後続）。テストが赤いまま merge しないこと。
    ティア数の障害は #4518 で解消（18 family にプレミアムを追加、20/20 が3プランで通る）。

    **その先で実物を測って見つかった3つの障害（2026-09-07、PR #4537）**:
    1. **カテゴリ名が20件すべて実在しなかった** — 全 family が `platform_overrides.lancers.category = 'システム開発'` を持っていたが、`___main_category_id` の9択にその文字列は無い。接続できていても20件すべて作成に失敗していた。実在ラベルへ写像した。
    2. **サブカテゴリは依存 select** — 大カテゴリを選ぶまで populate されない。語彙は公開の `/menu/search` パッケージ分類から取得（8親グループが9択のうち8つと1対1）。裏取り: 現在稼働中パッケージの `subcategory`（`SNSマーケティング・運用代行`）が対応する親にそのまま存在する。実機で1回選択が通り確定。`データ分析・作業自動化` の8択にブラウザRPAとSlack/Zoom連携を正直に置ける項目が無いため2 family を移した。
    3. **作成画面は6ステップのウィザード**（基本情報 → 料金表 → 業務内容 → 確認事項 → 画像ほか → 公開）。全ステップがDOMに存在し、現在以外は `_hidden_` ラッパー内。1画面だと仮定した平坦な入力は `ProjectPlanMenuForm[0].description` で `Locator.fill: Timeout 30000ms exceeded` になる（要素は解決するが箱がゼロサイズ）。`_fill_create_form` がステップを歩き、各「次へ」の後に到達を検証し、進めなければ画面上の検証文言を添えて `create_step_stalled: <step>` を投げる。CSSモジュールのハッシュは焼き込まない（可視性で判断する）。

    レーンは wake ごとに1 family を作成する（他に効果が無かった wake のみ、`account_lock` は入れ子にせず二度目を取得する — `fcntl.flock` は open file description を掴むので入れ子は自分で自分を待つ）。作成済みは永続化して二度作らない。overlay 不足の family は既定値で埋めず名指しでスキップ。
    **未証明**: カタログ由来の出品はランサーズにまだ1件も公開されていない。ウィザードの歩き方はDOM読みから書いたもので、公開ステップの送信ボタンのラベルは未観測（実行時探索に委ねている）。**受け入れ試験はループ自身の次の wake**。

13. **共有 component / 「金を刷る loop を作る skill」化** — 実測: 共有 profile を読むのは Lancers のみ（`storefront_offer.py:20`）。Coconala/CrowdWorks は未接続。
    3 platform の実測（2026-09-06）:

    | | ココナラ | ランサーズ | クラウドワークス |
    |---|---|---|---|
    | 実装 | `skills/earn/gig` 282 ファイル / 114,057 行 | `skills/earn/lancers` 4,072 行 | repo 外 `~/.local/share/anicca/crowdworks-revenue-skill/` 3 ファイル・未 versioned |
    | label / 間隔 | `hf-gig-storefront-direct` / 60s | `lancers-revenue-storefront` / 1800s | `crowdworks.storefront` / 300s |
    | 生存 | 稼働中（1b 修正前は全 wake 失敗） | lane 専用ログが 9/1 23:03 で停止 | lane state が 8/15 `observed_status:failed` で凍結、plist 指定ログが未生成 |
    | 出品 | 15 件・全て販売 0 | 1 件 `1338228` published | 0 件 |
    | 需要実測 | 30 日 views 441 / 購入 0 | 検索表示 20・閲覧 0・相談 0・注文 0 | — |
    | `_shared/marketplace-core` | 未使用 | 利用（唯一） | 未使用 |
    | SKILL.md | なし | あり | なし |

    共有層 `skills/_shared/marketplace-core` は 2,251 行 4 本（`ledger.py` 913 / `contracts.py` 549 / `application_transaction.py` 477 / `telegram_outbox.py` 312）= 帳簿と通知のみ。ココナラ側の資産（契約封印・公式読み戻し・重複 fence・capability family・KPI 帰属・コピー guard）は 1 行も共有されていない。
    ランサーズの出品内容自体はココナラより良い（`B2B企業のSNS更新を止めず見込み客に伝わる投稿を毎月制作し` / ¥29,800・¥198,000・¥398,000 の月次 3 プラン / やらないことの明示あり）。欠けているのは露出と、出品を増やす能力 — `storefront_offer.py` は "Inspect or align one canonical Lancers storefront offer" で 1 件を整合させる以上のことをしない。

    **あるべき形（2026-09-07 時点の差分）** — SKILL.md が既に宣言している依存方向は
    `loop config → recipe → shared runtime → provider adapter → official provider`。
    Apply と Paid はこの形になっている（`references/marketplace-apply-lane.md` / `marketplace-paid-lane.md` と `paid_kernel.py`）。
    **Storefront だけが recipe も kernel も無く、全部 `earn/gig/scripts/storefront_direct.py` の中にある。**

    ```
    skills/loop-engineering/references/marketplace-storefront-lane.md   ★新規
    skills/_shared/marketplace-core/scripts/
      storefront_kernel.py       ★ 選定(KEEP/IMPROVE/RETIRE/REPLACE/CREATE)・契約封印と検証・
                                    単一effect fence・重複fence・公式読み戻し照合・ロールバック・KPI帰属
      listing_projection.py      ★ catalog → 各platformの出品形（project_lancers は listing_catalog.py に実装済）
      transient.py               ★ 5回×3秒を1箇所に（今は5種類バラバラで、今夜落ちたのは全部弱い方）
      listing_catalog.py            既存（#4308）。ココナラのみ接続
    skills/_shared/marketplace-core/schemas/
      listing_contract.schema.json      ★ platform非依存
      duplicate_judgement.schema.json   ★ 今 earn/gig にある
    skills/earn/gig/adapters/coconala_storefront.py       ★ DOM操作だけ
    skills/earn/lancers/adapters/lancers_storefront.py    ★ DOM操作だけ
    skills/earn/crowdworks/                                ★ repo外(~/.local/share/anicca/)から repo内へ
    ```

    **抽出条件は既に満たしている。** SKILL.md の「A new abstraction is prohibited for one speculative
    consumer — the second real consumer is the extraction trigger」に対し、ランサーズという2つ目の
    実消費者が存在する。`storefront_kernel.py` が無い限り、ランサーズとクラウドワークスは公式読み戻しも
    重複fenceも単一effect fenceも自前で作り直す。今夜15回踏んだ穴を2platformがもう一度掘ることになる。
    DONE: skill で新 loop 1 本、既存資産再利用を実証。カタログ1箇所の変更が3platformの出品に反映されることを実測。
14. **README を real-time status に** — DONE: loop が書き換えた README diff が commit される。

## 補足事実

- Lancers profile: 公式完成度 **90%**（本人確認・NDA・avatar・portfolio 済）。残り10%は電話認証のみで、収益ブロッカーではない。

### 履歴書/職務経歴書（実サイト一次情報で再検証。当初の「欄は存在しない」は誤りだったので訂正）

| サイト | 職務経歴書 upload 欄 | 一次ソース |
|---|---|---|
| **Lancers** | **新規登録フローにのみ存在**（任意、「スキップする」で飛ばせる）。通常の「プロフィール編集」画面には無く、経歴・資格はテキスト入力のみ | lancers.jp/consultation/detail/7604（回答「職務経歴書の同様の内容をプロフィールに記載することができます」）、lancers.jp/faq/A1028/615 |
| **Coconala** | **無し**。職歴・学歴・資格は全てテキスト欄。画像upload は portfolio と本人確認書類のみ | help.coconala.com/hc/ja/articles/360011290814 |

Coconala は該当なしで確定。**Lancers は登録時に skip された可能性があり、当該アカウントで実際に upload 済みかは未確認**。
ただし公式の案内どおりテキストの経歴欄で代替可能なので、収益ブロッカーとは断定できない。

### ★ 本当の穴: profile 完成度が誰にも監視されていない

`PROFILE-ASSETS.md` 手順8 は「public URL からプロフィールを読み返して完成度を記録する」と定めているが、
**`~/.local/state/anicca/lancers/` に completeness / profile readback の記録は 1 件も無い**（grep 実測 0件）。
つまり「完成度90%」は `PROFILE-ASSETS.md` に手書きされた 8-31 時点の一回限りの値で、loop は profile を継続監視していない。
**profile が劣化・reset されても誰も気付かない構造。** Lancers 公式は「完成度が高いと受注率が14倍」と明示しており、
監視不在は最上位 leverage の放置。

Lancers 公式の「完成度100%の内訳」は 3 出所（help.lancers.jp / lancers.jp/faq / info.lancers.jp）を当たったが非公開。
公式が受注率向上要素として挙げるのは: 顔写真 / 自己紹介文 / 4つの認証 / ポートフォリオ / パッケージ出品
（lancers.jp/help/beginner/lancer/profile）。

- Lancers 応募60件の内訳: open 21 / selecting 14 / canceled 11 / ended 10 / unknown 4。**明示的 rejection は記録なし、受注も0** — 落選というより案件側の流札が主。
- Coconala outcome 549件: we_won 6 / someone_contracted 128 / closed_unfilled 394。勝率 ~1.1%（応募母数比）。
- gig 3 platform は component 重複ではなく共有 profile + platform別 adapter の構成（適切な分業、再発明なし）。
