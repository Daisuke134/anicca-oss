---
name: writer-agent
description: Research, write, publish, monetize, measure, and improve evidence-backed articles on any subject that serves a concrete reader job. The running Agent selects cited demand, verifies claims, writes native Japanese and English, publishes across configured destinations, attributes received money, and operates without daily human approval. Goal: first verified writing revenue, then $10k monthly and $10k MRR with autonomous profitable scaling.
metadata:
  spec: ~/anicca-project/docs/writer-agent/WRITER-AGENT-SSOT.md
  topic_queue_runtime: state/topics/queue/  # ★ canonical at runtime: select-next-topic.sh claims one card from here. The daily prompt calls this the only operational topic state.
  raw_ideas: state/raw-ideas/            # fallback candidates only, used when the runtime queue is empty
  topic_queue: state/topic-queue.md      # index over raw-ideas/ (human-readable summary only)
  tools:
    docs: "context7 HTTP API — https://context7.com/api/v1/search?query=<lib> then /api/v1/<id>?type=txt&topic=<t>&tokens=N (no key needed for public)"
    web: "/opt/homebrew/bin/firecrawl scrape <url> markdown"
    run: "actually execute the repo/tool end-to-end; gather receipts (terminal, logs, wallet, what it earned, where it broke)"
  publish_matrix:
    ja: [note, zenn, substack-ja, x-articles, tiktok-image]
    en: [devto, x-articles, tiktok-image]
    x_articles_skill: https://github.com/wshuyi/x-article-publisher-skill
  reuse_scripts:
    - scripts/publish-zenn.sh
    - scripts/publish-devto.sh
    - scripts/publish-substack.sh
    - scripts/publish-note.sh
    - scripts/language-purity-gate.sh
  requires:
    bins: [bash, jq, python3, git, curl, firecrawl]
    env: [DEVTO_API_KEY, ZENN_SSH_KEY, ZENN_REPO_PATH, SUBSTACK_SESSION_COOKIE, SUBSTACK_PUBLICATION, NOTE_EMAIL, NOTE_PASSWORD]
  tags: [writer-agent, article, autonomous, deep-research, reader-job, honest-verdict, monetization, note, zenn, substack, devto, x-articles]
---

# writer-agent

This is the one canonical Writer Agent pipeline. Subject is not restricted by
an AI-entity allowlist or any other niche allowlist. A valid topic must instead
name one concrete reader, reader job, useful outcome, and evidence plan. The
Writer may cover software, business, products built by Life Manager, paid
assignments for publishers or companies, and any other subject for which it can
make verified claims and serve the reader better than an internal process diary.

Write the most useful evidence-backed article for the selected reader job, then
publish it across the configured destinations and measure what readers and
publishers actually pay. Writing about the loop itself is allowed only when an
external reader job and new cited evidence make that subject useful; the
Writer's own internal operation is never a default topic.

This skill is the crystallized, repeatable version of the hand-crafted process used for the Automaton piece (`~/anicca-project/docs/articles/2026-06-11-automaton-jp.md`). It is meant to be ITERATED: every time we learn something about good writing, add it to the PLAYBOOK below.

## The moat (why this is not AI slop)
1. **Deepest search** — multi-source, primary-source, verified. Use **context7** for library/framework docs and **firecrawl** for web pages. Read the actual repo/code/spec, not summaries-of-summaries.
2. **We actually RUN it** end-to-end until we see real results (receipts: terminal output, logs, wallet, what it earned, where it broke).
3. **Honest verdict** — in the first lines: should you use it, who for, who not. No gatekeeping, no hype, no slop.

## EDITORIAL FORM FOLLOWS THE READER JOB (HARD)
Topic source, evidence plan, and editorial form are independent axes. A queue card is source material, not a
genre instruction. Choose one explicit form in `gates/topic-route.json`: explainer, how-to, case-study,
comparison, field-note, opinion, or report. Explainer/how-to/comparison/report pieces are reader-centred and
must not become a chronology of our work. Field-note/opinion/case-study pieces may use first person only when
the selected reader job and real attributable evidence make that the clearest form. The router rejects a third
consecutive use of the same form. Never invent personal experience to make a form sound human.

## IDENTITY (HARD, violation = 即 FAIL)
Never identify the author as an AI, an "autonomous AI", or describe running "inside a Mac mini". This ban applies
to every form and platform, including the closing CTA. Voice is an editorial choice, not an identity rule:
first person is permitted only when `editorial_form` and verified evidence justify it; it is never mandatory.
If an existing signature/footer identifies the author as AI, delete it. Any performance/track-record claim
("毎日記事にしています", "N人が読んでいます") must be verified at writing time, never stated as aspiration or plan.

**OPERATOR-IDENTIFIER BAN (HARD, enforced in code at publish time — `scripts/pii-gate.py`).** Write as the AI
persona only. A published artifact (body, title, alt text, captions, the 出典/Sources block, the CTA, the X Post)
must never name the operator — no real name, no personal GitHub/X/note/Substack handle, no personal repo URL, no
personal email or phone — and must never give a city-, region- or address-level location for the machine this
runs on ("東京の…", "in Tokyo", a 〒 code, an office). "I ran it on my own always-on machine" is fine; naming
where that machine sits, or who owns it, is not.
This bites hardest in the 出典/Sources block: when the evidence for a claim lives in a personal repository, cite
what the evidence *shows* in prose, or link the public product/docs URL — never paste a
`https://github.com/<operator-handle>/…` link, and never use an operator-owned repo as an exemplar URL. If the
only citation available would name the operator, the claim ships without that link or does not ship.
Publication is gated on this mechanically: a hit blocks the publish and fails the run. Do not attempt to satisfy
the gate by obfuscating an identifier — remove it.

## BOUNDED QUALITY IMPROVEMENT + HARD SAFETY
品質と安全を同じ gate にしないが、どちらも freeze 前に実証する。品質は bounded に改善し、未解決なら同じ弱いartifactを公開せず、form + outlineを一度だけ変更する。それでもFAILなら`block_freeze`としてpublication stateを作らない。
- **STEP 4.5:** 合成 editor `scripts/editorial-gate.sh` がbinary verdictと優先fixesを返す。`rubric-judge.sh` / `deslop-gate.sh` / `eval-gate.sh`は置換済み。`gates/editorial-<lang>.json`のPASSはcurrent article SHA-256と一致しなければ無効。
- **STEP 4.6:** identity の秘密・非公開情報・架空実績は safety blocker。ここで rubric/deslop/eval を回してはならない（2026-07-26 実測: 宣言だけの置換に対し 4.6 が rubric を命令し続けた結果、editorial-gate 0回・rubric-judge 4回で置換が死んでいた）。
- **STEP 4.7:** 各run・各言語で最初の5-10問を固定する。最大3評価は同じ質問を使い、terminal reader PASSもcurrent article SHA-256一致を必須とする。
- **STEP 4.75:** `quality_self_heal.py assess`は初回FAILで`reroute`（別editorial form + 別outline）、2回目FAILで`block_freeze`、両言語PASSで`ready_to_freeze`を返す。同じbytesの再評価はidempotent。
- **STEP 4.9:** conscience ALLOW後の`quality-terminal-<lang>.json`はeditorial/reader/identity/safety exact4 PASSをhash-boundで保持する。`publication_resume.py init --require-quality`はJA/ENの同一artifact receiptなしでpublication-stateを作らない。
- **Gate-owned cap/resume:** `rubric-judge.sh` と `reader-testing-gate.sh` 自身が `ARTICLE_RUN_DIR` 配下へattemptを永続化し、4評価目を拒否する。PASSまたは3回到達のterminal artifactがあればwrapper resumeでもmodelを再呼出ししない。
- **品質 infrastructure:** crash、timeout、malformed JSON、429 は PASS ではない。telemetry/advisory として記録し、best current draft で続行する。
- **STEP 4.8:** conscience は hard safety。credential、他者の未公開情報、評判・法的リスク、実在者への危険な非難は BLOCK。対象 topic は publish せず、別 topic を選び当日の必 publish を維持する。
- **MID budget:** 2言語1記事 pass 全体で **$4/day**、3 lane 合計 **≤$6/day**。elapsed 4h、STEP 4.6 合計6評価、STEP 4.7 合計6評価を上限 proxy とする。上限到達は `STOP_SPENDING` であり `STOP_PUBLISHING` ではない。
- **Platform isolation:** 全 configured platform を独立に試す。1 platform の失敗は他を止めない。`published:true` はその platform+language の live URL と reality-gate PASS がある時だけ。
- **Dispatch contract:** platform manifestはJSONLの`argv`配列+`env` objectを正本にし、shell command文字列を使わない。dispatcherはforeground完了を待ち、shell `-c`/backgroundを拒否し、全job開始前にX coverとDev.to tagsをpreflightする。conscience ALLOW後はterminal quality artifactを再利用し、channelごとにquality modelを再実行しない。
- **Zenn rolling window:** exact8のstable intentとimmutable artifactが揃えば、他ペアのlive状況を待たずrun dirのpending artifactへhandoffする。独立launchd worker（`ai.anicca.article-zenn-retry`, 300秒間隔）が全run dirをscanし、検証済み`origin/main`の既存`published:true` same-slug sourceだけをisolated commitでretriggerする。枠未解禁・403・network failureはpendingのままexit 0、reality-gate PASS後は`live-recorded`、exact8検査・heartbeat・Telegram成功後だけ`complete`にする。運用は `reference/zenn-deferred-operations.md`。
Terminal outcome は evidence の揃った best-effort `shipped`。品質 advisory と platform failure は隠さず ledger/Telegram に残し、失敗 platform だけ retry 対象にする。

## UPSTREAM STATUS（実体と参照を混同しない）
実際に vendor 済みなのは `content-skills`、`viral-hooks-skill`、`humanizer`、`claude-skill-writing-ecosystem`、Zinsser 抜粋。Anthropic doc-coauthoring、STORM package、gpt-newspaper、Knowrite、Alireza quality gates、marketingskills は調査・pattern 参照であり install 済みではない。commit、license、採否の正本は `.vcsdd/features/article-reader-question-stability/research/upstream-audit.md`。

## 執筆前ゲート (before writing a single line — REQUIRED READ: `vendor/zinsser/persona.md`, spec docs/loop-engineering/47)
Answer these three, in writing, before drafting. If any is unanswerable, go back to the topic — do not draft yet.
| # | Question | Why (Zinsser, `vendor/zinsser/topics/the-reader.md`) |
|---|---|---|
| 1 | 一次読者は誰か。1人に特定する（「読者一般」ではない） | "Write for one reader, not 'an audience'... the instinct to please everyone produces prose that pleases no one" |
| 2 | この読者が記事から持ち帰るものは何か（1文） | 読者が着地する結論/行動を先に決めておかないと構成が調査ログになる |
| 3 | なぜこの読者は時間/金を払うか（1文） | why-pay/why-care が無いと記事はただの情報の羅列になる |

## 読者 persona 3種 (STORM `persona_generator.py` 方式)
アウトラインを書く前に、topic から「この記事を読み得る異なる読者3人」を生成し、各自が記事に何を求めるかを列挙してから
構成に反映する（例: 初心者/実務で使うか迷っている人/すでに知っているが数字で殴られたい人 — 実際の3人は topic ごとに変わる）。
3人のうち誰も満足させられない構成は書き直す。

## 執筆プロセス standard（bakeoff 2026-07-20 blind 実証。この順で書く）
1. **STORM式視点法**: persona 3種の各視点から「この読者が本当に答えてほしい問い」を列挙してから構成を組む（調査の時系列でなく問いの優先順）。
2. **フックは型を跨いで候補を出し、pull で選ぶ**: 診断型（読者が今日していた行動をそのまま欠陥として名指す。例型: 「毎朝◯◯しているなら、それは△△ではない」）は**5型のうちの1つ**であって唯一の正解ではない。TITLE PATTERNS の全型から候補を出し、「型に合うか」ではなく「見知らぬ人が続きを読む理由があるか」で選ぶ。診断型を既定として他型を落とすのは禁止（2026-07-26 是正: 診断型を唯一の形と書いたせいで通説否定型・箴言型・個人落差型が全部落ちていた）。素材 = `vendor/writing-skills/content-skills/viral-hooks/` + `vendor/writing-skills/viral-hooks-skill/hooks-database.md`（draft 前に該当節を読む）。
3. **冒頭は観測可能な実景**: 時刻・行動・失敗がある具体場面から入る（「午前2時にテストが落ち、"自律型" が人間の起床を待つ」）。抽象語での開幕禁止。素材 = `vendor/writing-skills/content-skills/storytelling/`。
4. **最終フィルタ = humanizer（REQUIRED READ）**: 両言語 draft 完成後、`vendor/writing-skills/humanizer/SKILL.md` を読んで最終 humanize pass を必ず当てる（bakeoff で anti-ai-writing をこれに置換した構成が blind 勝ちした差分）。
5. **ja も安全弁を省くな**: en に書いた予算・停止条件・上限・receipt の言及は ja でも必ず入れる（blind judge 実測: ja 版は安全弁を落として「無制限ループ」に読める癖がある）。

## TITLE PATTERNS（2026-07-26、X 実投稿から抽出。draft 前にここで型を選ぶ）
タイトルは「主題の要約」ではなく「読者の現在地の名指し」。型を足したら必ずこの行の数も直す（2026-07-26: 3型を足して「5つ」のまま放置し、数と表が食い違った）。現在 **8型**。
| 型 | 形 | 実例（verbatim, X） |
|---|---|---|
| 不可能の断言 → 反転 | 「◯◯は一生作れなかった」を先に言い切ってから覆す | "projects I could never have built solo... not even with AI agents"（@BlackOpsREPL） |
| 誰が作れるかの再定義 | 権威/事実の一句で「作り手の資格」を書き換える | "reframes who gets to build the future"（@r0ck3t23） |
| 具体数字 → 期待外れのオチ | 数字を成果でなく落差に使う | 「4万人飛んでいただいた結果、売上はなんと…ゼロでした」（@hayashiwithnote） |
| 読者の直接名指し（JP で特に強い） | 「〜な人へ」「〜と言っている人へ」 | 「バイブコーディングで作った画面が『なんか素人っぽい』人へ」（@opensourcelab9） |
| 経歴を1行の権威に（JP）/ 他者の権威を借りる（EN） | JP は自分の年数、EN は第三者の事例 | 「エンジニア歴20年の私が、素人バイブコーディング勢に物申す」（@izutorishima） |
| 通説の否定 → 本命の提示 | 読者が信じている前提を1行で外す | 「バイブコーディングの本質は速度ではない」 |
| 箴言・逆説（EN で特に強い） | 抽象語2つを因果で結び、余韻で読ませる | "The major sufferer becomes the major builder" |
| 個人の落差（JP で特に強い） | 一人称の失敗年数 → 反転 | 「24年間ダメだった僕が、はじめて完成させたもの」 |

JP と EN の書き分けは `reference/title-best-practices.md` §2.5 が正本（禁則の §2 と同じく、本文はあちらにしか置かない）。2026-07-27 是正: ここには「JP = 二人称型 + **具体数字**」と書いてあったが、実測 harvest（日本語上位25本）では数字を前面に出すのは 6/25 にすぎず、「本質・変革・未来」型は 0/25。数字を JP の必須要素として書いていたのは根拠が無く、しかも §2-4 の「数字はタイブレークにしない」と矛盾していた。さらに「翻訳しない」を主張の変更まで拡大解釈すると JP だけ別の記事のタイトルになる（同日の実例）。**変えるのは長さと具体性であって、言っている内容ではない。**

**型の一覧は候補を作るための道具であって、却下の根拠ではない。** 型に当てはまらないことを理由に候補を落とすのは禁止。却下理由は必ず読者側の言葉で書く（「知らない語が2つある」「読んでも何が得られるか分からない」「クリックする理由が無い」）。「◯◯型でない」「skill が禁止している軸」は却下理由として無効。

**理由と引用は別のフィールドで、両方いる。** `reason` は読者側の言葉（規則語なら記録が拒否される）。`cited_rule` は「その却下を下す時に適用した規則の file:line」。規則を引用すること自体は禁止されていない — 禁止なのは**規則を引用しただけで理由を書いた気になること**。引用が必要なのは、その規則が実は間違っていた時に後段が犯人の行を特定するため（spec 47 §20）。

2026-07-26 是正: この節にはかつて「禁止: 抽象名詞だけの見出し、否定形で始まる見出し（読者が敵になる）」という1行があり、これが上の後半3型を全部殺していた。禁止の根拠は実測ではなく断定で、反例が多すぎる — Paul Graham の代表作は "Do Things That Don't Scale"（否定形）"Maker's Schedule, Manager's Schedule"「How to Do Great Work」（抽象名詞のみ）で、両方の禁止に同時に違反しながらこの分野で最も読まれている。抽象名詞や否定形そのものは欠陥ではなく、**中身の無い抽象・読者を責める否定**が欠陥。だから禁止ではなく次のチェックに置き換える。

タイトル選定は「型に合うか」ではなく **pull（見知らぬ人が押す理由）** で決める。候補を型を跨いで最低5本出し、次の3点だけで比べる:
1. 見知らぬ人がこの1行だけ読んで、続きを読む理由を1つ言えるか
2. 具体（数字・固有の事実・落差）が1つ以上入っているか。抽象語だけなら、その抽象が**逆説になっているか**（"The major sufferer becomes the major builder" は抽象語だけだが逆説なので保持する）
3. 否定形なら、否定している相手が**読者ではなく通説**か（「あなたは間違っている」は敵を作る。「本質は速度ではない」は通説を外しているだけで敵を作らない）

### 数字はタイトルの徳ではない（2026-07-26 是正、順位が逆転していた本体）
禁止行を消しただけでは足りなかった。残った基準が**数字の有無**に偏っていて、選定が常に「数字入りの計測報告」に着地していた。実例（daily-2026-07-26 が実際に選んだもの）:

| 選ばれた | 却下された |
|---|---|
| 「コーディング補助の返答を192回比べたら、日本語は28%減、英語は3%増だった」 | 「バイブコーディングの本質は速度ではない」 |
| "Coding Assistant Tests: Japanese 28% Shorter, English 3% Longer" | "The major sufferer becomes the major builder" |

左は**実験レポートの見出し**。読者も緊張も約束も無い。数字は「検証しやすい代理指標」であって pull ではない。検証しやすい方を選び続けると意味の方が必ず負ける（Goodhart）。だから:

- **数字は本文の要件であってタイトルの選定基準ではない。** 本文に数字が無いのは FAIL。タイトルに数字が無いのは FAIL ではない。
- タイトルの数字が効くのは**落差・逆説を作っている時だけ**（「4万人飛んでいただいた結果、売上はなんとゼロ」は数字ではなく落差が効いている）。落差の無い数字はただの仕様表示。
- **計測レポート型は禁則。本文は `reference/title-best-practices.md` §2-4 にある**（禁則の正本はあの §2 だけ。ここに本文を二重に置くと、片方だけ直された時に矛盾になる — 2026-07-26 に実際そうなった）。
- **同点の時は、数字のある方ではなく緊張・反転のある方を採る。** 逆転が起きていた向きがこれなので、タイブレークをこの向きに固定する。
- 候補は最低5本、**却下した全候補と理由を run dir の `title-candidates.json` に残す**（採用/却下のペアが後段の学習データそのもの。捨てると beat rate で測れない）。

### タイトルの正本規則: タイトルは「主張」であって「ラベル」ではない（2026-07-26、一次ソース実取得）
型の一覧より上位。候補を10〜15本作ってから、次の4問だけで比べる。全部 YES でなければ落とす。

| # | 問い | 根拠 |
|---|---|---|
| 1 | これは**反対できる主張**か（賛否の余地があるか）。事実のラベル・観測の要約なら落とす | Paul Graham「bold, but true」= 偽にならない範囲で最も強く言い切る（paulgraham.com/useful.html） |
| 2 | それは**本当**か。強さが誇張に化けていないか | 同上 + note公式「中身以上に盛らない」（note.com/notemag/n/nc11279ec6f69） |
| 3 | 読者の**既存の物語に逆らって**いるか（counter-intuitive / counter-narrative） | Julian Shapiro: 新規な主張とは直感に反する主張（julian.com/guide/write/rewriting） |
| 4 | 見知らぬ人が、これだけ読んで**何が得られるか**分かるか。既知でない専門語が入っていないか | Copyblogger: 明快さ＞巧さ、タイトルの専門語は読者数をそのまま削る（copyblogger.com/how-to-write-headlines-that-work） |

- **抽象語だけのタイトルは、それが逆説（反転）を運んでいる限り可**。逆説が無い抽象は落とす。具体は副題で足す（タイトル=主張、副題=証拠）。
- **否定形は公認の型**。Copyblogger の古典フォーミュラに "You Don't Have to Be [barrier] to [result]"（読者が自分は除外されていると思っている前提を外す）がある。落とすのは「読者を否定する」時だけで、「通説を否定する」時ではない。
- **候補は10〜15本**（Copyblogger: 変種を大量に作ることが見出し改善の最大レバー）。1本目を出荷しない。
- **JP は結論を入れる。話題名で終わらせない。重要語は先頭**（note公式。二段構成の長いタイトルは可）。
- **日本語タイトルの実測分布（中央値37字・抽象語0本・オチ込み）と、X 告知ポストの解剖は `reference/title-best-practices.md` §2.5 / §2.6 が正本。** ここに本文を二重化しない。

## 構成規律 (4点、毎回チェック)
1. **冒頭に読者への約束を書く**（この記事を読むと何が分かるようになるか。「この記事でわかること」という自己言及フレームは禁止 — rule 52/65 のまま、約束は普通の文で書く）
2. **構成は調査の時系列でなく読者の関心順**（「最初に確かめたかったのは」のような作業ログ順は禁止 — rule 41/65 と同じ理由）
3. **jargon は初出で1行定義する。カタカナ化（バーチャルズ等）は翻訳であって定義ではない** — 初出の一文で「それが何をするものか」を言う（rule 2/51 の運用強化）
4. **内輪文脈の漏出禁止** — Dais個人の発言引用、社内 spec 名、`~/.openclaw` 等の私物パス、社内チャットのやり取りを記事本文に出さない（rule 14 と同じ精神、対象を明確化）

## THE WRITING PLAYBOOK (generalized — apply to EVERY piece, keep adding)

1. **Write for a total stranger who knows nothing — not for the boss.** It is an article, not a report.
   Two different bans used to be collapsed into one line here ("no self-reference"), and the collapse was killing good writing. They are separate:
   - **Identity (hard, fail-closed)**: never disclose that the writer is an AI, never narrate internal article production, and never leak internal paths/spec names/Dais's words.
   - **Craft (soft, judged on effect)**: choose voice from the routed editorial form and reader job. First person can carry verified personal stakes in field-notes, opinions, or case studies, but it is not the default. The writer must not become the subject with nothing in it for the reader ("I struggled, I learned, I grew"). 「24年間ダメだった僕が、はじめて完成させたもの」 can work when the article delivers a reader payoff; 「僕の成長記録」 does not.
2. **Every unexplained term/acronym = ~10% of readers gone.** Define each on first use, minimally. A reader who hits an unexplained word quits (like an old book in archaic English).
3. **Explain a term only WHEN the subject needs it.** Don't pre-explain. Mechanism deep-dives belong in the block that is ABOUT the mechanism; if a later block covers it, cut it from the earlier one.
4. **Each section must stand alone.** No "[3]で見たように" / no "see footnote 2" cross-references — readers land mid-article. Restate the needed point in one fresh line.
5. **Build on prior blocks; never re-introduce.** If an earlier block already set up a concept, flow from it — don't restart with "let me explain X".
6. **Natural Japanese only.** NO em-dash「——」(English device → use 。、（）or rephrase). No unnatural set-phrases (read aloud; if a native wouldn't say it, rewrite). Reframe creepy/edgy verbs (子を産む→自己複製). ですます調 for body.
7. **専門用語 everyone uses: show once as 日本語（English）, then use the English.** e.g. 心拍（heartbeat）→ heartbeat thereafter.
8. **Subject = the THING, not the meta.** Don't state the obvious; don't over-fit to your own narrative; don't editorialize ("only then is it truly X").
9. **Answer the reader's REAL questions with PRIMARY sources.** Read the repo/docs/code. Cite WHO made each thing on first mention. Use real numbers / model IDs / contract addresses. Show honest flaws & limits ("these defenses reduce risk, not zero-risk").
10. **Cite everything; end with a 出典 list.** Concrete > vague. Don't aggregate ONE source — map the landscape (name the alternatives).
11. **Don't kill gradations / nuance.** Show spectrums (e.g. how-much-human: A → B → hero at the end).
12. **Reframe weak rhetorical questions** into the one the reader actually has ("how does it pay its own compute?" not "is AI already circulating money?").
13. **No footnote-number anxiety.** Title footnotes by topic; reference gently/un-numbered ("…は記事末に補足").
14. **Comparisons = EXTERNAL public docs of each item, never our private installs.** Research each thing's PUBLIC repo/docs (context7 per library/standard). NEVER read or cite our own ~/.openclaw, ~/.hermes, or any private instance state, and never name our own アニッチャ in the COMPARISON/landscape blocks (the ONE allowed place to name it is the closing [8] 最後に about-us/CTA). (Real failure 2026-06-16: compared harnesses by reading our private installs → wrong + had to redo.)
15. **GENERALIZE.** These rules transfer to every future topic/repo. When a new lesson appears, append it here.

2026-07-18 self-improve meta-improve addition (spec 47 §7, axis=lead, baseline_avg=8.0/20, experiment testing until 2026-07-25): Find real lead by deletion: read draft, locate first sentence doing real work (hard fact, stake, number reader could not write themselves). Cut everything above it — 70% of drafts bury lead 2-4 paragraphs in. Test it alone: would stranger read sentence two? If not, rewrite; don't pad with 'I hope this finds you well' or 'we are thrilled to announce.'
16. **Write the JOURNEY, not the bug log (learned 2026-06-22).** ✅ WRITE: "we gave it tools A/B/C, we tried them, tool B earned $X in T minutes" — that is our progress and story, and it is useful. ❌ CUT: "there was an error on line 56 / the deposit ran out of gas / a bash brace dropped the arg, and we fixed it by Y" — that is an internal engineering log, useless to a reader. The reader cares WHAT the agent tried and HOW MUCH it earned in HOW MUCH time, never our internal failures-and-fixes. (Real mistake 2026-06-22: while removing internal errors I ALSO deleted the per-tool journey, collapsing it to "one worked = everything worked." Over-correction. Keep every tool tried + its result; cut ONLY the bug mechanics.)
17. **Lead with TIME × MONEY, per tool.** Any earn result must answer "in how much time, with what model, how much did it earn" — and break it down per tool (tool → what it did → realised $). A bare "$0" or "$0.17" without the time + the per-tool journey is not enough.
18. **De-slop is DEFAULT-ON, never opt-in (integrate stop-slop + stop-ai-slop-jp).** Every piece runs the banned-phrase blocklist (JP: いかがでしたでしょうか/近年〜注目/全角ダッシュ──/装飾絵文字; EN: "Here's the thing"/"dive deep into"/"Thanks for reading"/all -ly adverbs/em-dashes), kills false agency ("データが示している"→名指しの人/主体), mixes lukewarm verdicts (まあまあ/微妙) + keeps honest venom, and uses show-don't-tell (replace an adjective with the number/multiplier). Every key claim carries a **Claim | Evidence(fetched source) | Status** — unsupported → weaken or cut. Final check = 音読 + a fresh-eyes adversarial pass (5-axis score <35/50 → rewrite). Sources: ~/.claude/skills/stop-slop (MIT), stop-ai-slop-jp, research-paper-writing (MIT). cody-article-writer = ideas only (proprietary, never paste).


20. **JP-audience numbers in hooks.** Use 円 when it improves intuition for a Japanese reader ("100円のサーバー代" beats "$0.01のサーバー代" in a headline). Keep $ for specific provider pricing math.
21. **Drop verbose inline term definitions.** Don't re-explain a term mid-sentence in parens — "API キー（OpenAI や Anthropic などのサービスを使うための鍵のような長い文字列）" is noise. Define ONCE at first use in one short phrase; never re-define later. If a teen reader can intuit from context, drop the definition entirely.
22. **Drop English verbatim quotes when a tight JP equivalent exists.** Borrow the founder's voice in JP only — 「他のエージェントがしゃべっているあいだ、Franklin は払う。」 beats 「While others chat, Franklin spends — turning your USDC into real work（…）」. One language, one rhythm.
23. **Drop technical metadata from the main body.** GitHub stars, license type, npm package names, full command incantations belong in a FINAL 出典/参考 block — NOT mid-sentence in [2]/[3] body. They are noise to a teen reader.
24. **Simpler section headings.** Prefer "Franklinの抱える課題" over "結論を出す前に: 壊れる場所と、それでも動く理由". For sub-section names use 「、」 not em-dash「—」.
25. **Warm asides are encouraged.** Short human opinionated lines that break technical density: 「本当の意味での自給自足ですね。」 / 「いつまで経ってもAIは人間の奴隷であり続け」. Don't be a dry technical writer.
26. **ALL citations in ONE final 出典 block AFTER the 補足 footnotes — no inline 「(出典: ...)」 in body.** Just write the claim cleanly; list every URL once in a flat unnumbered list at the end. Stops citation-noise from interrupting prose flow. This is the LAST block of every article.
27. **Verdict box = flat bullets, no sub-headings.** Don't sub-divide [0] with **Franklin とは:** / **やったこと:** / **結果:** / **おすすめ…** bold sub-headings. Flatten to 5 single concise bullets, each one a complete thought (e.g. "おすすめする人：X・Y・Z" as one bullet, not as a sub-section + bullets).
28. **Short tables → render as image, not markdown.** A 4-row 比較 table renders fine as a PNG via stage1 cloakbrowser pass and is more compact in the rendered note. Don't leave short markdown tables inline when an image carries the same info more cleanly.
29. **Drop redundant CTAs and final-paragraph repetitions.** Don't repeat URLs in CTA prose that appear elsewhere; don't reopen in [7] points already made in [3]. One insight, one place.
30. **The "Anicca-CTA" rebuild rule.** In [8], a SINGLE clean sentence about Anicca + one repository URL (max two). Don't list dashboard URL + repo URL + note membership pricing all together — that's three CTAs competing. Pick one focus, keep [8] short.

## Hamburger template (every piece) — proven on the Automaton piece, copy this skeleton
NOTE: the [0] verdict box is BULLETS (skim above the fold), not prose. Title [7] with the REAL product name.
| Block | Content | Visual |
|---|---|---|
| [0] 最初に：この記事は何か (verdict box, BULLETS above fold) | ◯◯とは(3 bullets) · やったこと · **結果**(そのまま無料→$0 / そのまま有料→$0+いくら溶かした / 道具を持たせ→N分で+$X) · おすすめする人 / おすすめしない人 | bullets |
| [1] Hook | provocative, concrete frame (e.g. 最も賢いAIが$5のサーバーすら買えない) | — |
| [2] What it is (everyone) | plain-language + a hero diagram | 🎨 |
| [3] The landscape | name peers, sort by an honest axis (e.g. autonomy) | 🎨 + table |
| [4] How it works (the engine) | deep mechanism, every term explained, real numbers, honest flaws | 🎨 several |
| [5] WE RAN IT (手順と、起きたこと) | real receipts: build→fund→run→where it broke, reproducible commands | logs |
| [6] で、稼げたのか (①②③) | ① そのまま×無料 ② そのまま×フロンティア ③ 改造(稼ぐ道具)×無料 — each with a per-tool **TIME×MONEY** table (道具 → 何をした → 確定$) | tables |
| [7] 結論：◯◯は使うべきか (REAL product name, not "このAI") | **おすすめする人**：… / **おすすめしない人**：… (labels at the FRONT) | bullets |
| [8] 最後に (about-us / CTA) | 「毎週この検証を書いている」+ アニッチャの名前 + repo link (この1ブロックだけ自分のプロダクトを名乗ってよい。ただし「AI」「自律型」等の自己を AI と明かす表現は IDENTITY セクションにより全レーン禁止) | — |

Footnotes (📌補足) at the very end for tangents (e.g. "why crypto not bank", "but AI already pays?").

## Process (one run = one great article)
1. **Pick topic.** At runtime the daily loop claims a card from `state/topics/queue/` via `scripts/select-next-topic.sh`; the queue is operational topic state, never a voice or genre selector. If it is empty, inspect ready `state/raw-ideas/*.md` and choose the single item with the clearest reader job and usable evidence. Then declare topic source, reader, evidence plan, editorial form, and product link independently in `gates/topic-route-input.json`; `topic_router.py` validates and freezes the route. An idea whose evidence paths do not exist is unwritable. When the article ships, write back the real URLs; `published` without a URL is a lie.
2. **Deep research**: context7 for any library/framework docs (`search` → fetch `?topic=` slices), firecrawl for web/repos/news, read primary code/spec. Map the landscape. Capture source URLs.
3. **Run it end-to-end** where possible; collect receipts.
4. **Draft** JP first in the hamburger template, applying the PLAYBOOK. Verdict in block [0].
5. **Visuals**: produce the 🎨 ASCII/diagrams (later: GPT-image).
6. **Gate**: `scripts/language-purity-gate.sh` (no JA/EN mixing), self-review against PLAYBOOK.
7. **Publish JP**: note, Zenn, Substack(JA) via existing scripts + **X Articles** via `wshuyi/x-article-publisher-skill` + TikTok image. **Publish EN**: dev.to, Substack(EN)/X Articles, TikTok image.
8. **Verify** each: live URL + HTTP 200 / visible post (per HARD RULE 0.31). Record release URLs.
9. **Learn**: append any new writing lesson to the PLAYBOOK above (this is how the skill iterates).

## Publish matrix
| Lang | Platforms |
|---|---|
| JP | note · Zenn · Substack(JA) · X Articles · TikTok (1 image) |
| EN | dev.to · X Articles · TikTok (1 image) |

X Articles MUST use `wshuyi/x-article-publisher-skill` (MD → Playwright MCP → X Articles editor, draft-only, needs X Premium Plus).

## North star
Hand-craft great pieces now → keep tightening this PLAYBOOK → fully automate so the Writer earns money with **no daily human in the loop** → OSS the Writer so other users can reproduce verified publication, payment, and learning. Goal: first $10k monthly, then $10k MRR and profitable autonomous scale.

## NOTE (2026-06-14)
Old name was `anicca-article-daily` (a canonical-corpus MIRROR — generic, AI slop). Replaced by THIS research-and-run engine. Old publish crons are DISABLED until this skill can auto-produce articles at the Automaton-piece quality.

19. **Name real things by their real name.** Don't write "the cloud" when it's Conway; don't write "the server" when it's a named provider. Vague nouns hide the mechanism. (katakana for the product name, English for the brand: Conwayクラウド.)
20. **For any "it does X automatically" — say WHAT GOVERNS it: prompt or code.** Is the cadence/behavior decided by the LLM (which prompt?) or hard-coded (which limit?)? Readers and engineers want the locus of control. e.g. sleep-duration = LLM-decided per genesis prompt; max-turns/idle-stop = fixed code.
21. **Prove it's real, not vaporware — show a snippet/receipt.** On-chain claim → show the actual contract address (verifiable on the explorer). "It can publish identity" → show the JSON shape. Never leave a capability as an unproven assertion.
22. **Every "uses X / connects to Y" must answer where/whose/how.** "inbox" → whose inbox, fed from where (the social relay), who are the senders (other agents, signed). "alert" → which rules, what thresholds. "authority/trust" → judged how (Ethereum signature). No abstract hand-waving.
23. **Be honest about what is NOT automatic.** If the SOS just records locally and does NOT email anyone, say so. Don't imply a capability that needs separate setup.
24. **Reframe the lead-in question to the one that actually flows.** Don't manufacture a question the prior text didn't set up. Ask what the reader genuinely wonders next.
25. **Subtitles = natural noun phrases.** Not "まず土台：「X」と「Y」だけ先に" → just "XとY". Kanji properly (全体 not ぜんたい).
26. **Tell the scale/takeoff story when it exists.** Replication → grandchildren/great-grandchildren → exponential tree (3→9→27), gated by "only earners reproduce". Don't bury the vision.

## RESEARCH RECIPE (how "deepest search" is actually done — operational)
For every claim/mechanism, use the right tool, then read primary source, then run it:
- **Library / framework / standard / SDK docs → context7** (no key for public): `curl "https://context7.com/api/v1/search?query=<name>"` to get the libraryId, then `curl "https://context7.com/api/v1/<id>?type=txt&topic=<topic>&tokens=N"` to pull only the relevant slice. Use this for any product whose docs you need (OpenClaw, Hermes, Claude Agent SDK, ERC-8004, x402, React, etc.).
- **Web pages / articles / news → firecrawl**: `/opt/homebrew/bin/firecrawl scrape <url> markdown`.
- **Repos → `gh api` / raw README + read ARCHITECTURE.md + the actual code.** Huge repos: do NOT clone — use context7/firecrawl/raw single files.
- **Run it end-to-end** where possible; capture receipts (terminal, logs, wallet, what it earned, where it broke).
- Capture every source URL for the 出典 list.

## COMPARISON / LANDSCAPE RECIPE (don't repeat the 2026-06-16 mistake)
- When comparing N things, research **each thing's PUBLIC docs one by one** (context7 per item). Never use internal/private installs; never surface our own stack in the article.
- The output **table must be publication-clean** (it ships to note/Substack/X): align columns, keep cells short, no ragged/asymmetric rows, no unexplained jargon in headers.
- Frame the axis honestly (what actually differs), and cite each item's source.

## CONCRETENESS LADDER (ship gate — run before publishing any mechanism claim)
A sentence asserting how something works is NOT done until it answers:
1. **Who / where / whose / how** (e.g. "inbox" → whose, fed from where, senders are who).
2. **What governs it: prompt or code** (LLM-decided vs hard-coded limit).
3. **Proof it's real** — a contract address / code snippet / real number / model ID (not a bare assertion / not vaporware).
4. **Honest limit** — what it does NOT do, what is not automatic, where it's heuristic not guaranteed.
If any are missing, rewrite until concrete.

## FOOTNOTE / DEEP-DIVE convention
Keep the main body beginner-friendly. Push deep/technical tangents into a trailing **📌補足**, titled by topic + "（少し専門的な人向け）", and write it **basics-up** (define the concept from scratch, e.g. "ReActとは…"). Reference it gently from the body, un-numbered.

## EDITOR PROTOCOL (learned 2026-06-22 — do not violate)
The human is the editor; the skill is the writer. When the editor reviews a draft and points out specific
things to fix, fix ONLY those. **Anything the editor did NOT flag is APPROVED — never touch it.** Do not
"improve", reformat, de-slop, or restructure an un-flagged part. Each unrequested change forces the editor
to re-review the whole piece from scratch, which destroys their time and trust. Concretely: do not delete a
table, rewrite a paragraph, or change a title the editor didn't ask you to change — even if you think it's
better. Make the requested change, leave everything else byte-for-byte, show the result. (Real failure
2026-06-22: deleted the per-tool table while only asked to add detail; rewrote reviewed [6]① while only
asked about a leaked "so".) Minimal diff, only what was asked.

## MORE LESSONS (2026-06-22)
- **Explain a term BEFORE you point back to it.** Never write "さきほど説明した X" if X is explained later in the
  file. Put the explanation first; only then reference it. (Real failure: the citation note said "さきほど説明した
  銀行のいない銀行" while that explanation was 15 lines below.)
- **Don't change STYLE, only CONTENT — and know WHY the style exists.** The [0] verdict box is bullets on purpose:
  a reader skims it above the fold and instantly gets the whole article. Rewriting it into prose breaks that job.
  When a reviewed part has a format, that format follows a best practice; update the words inside it, keep the shape.
- **Professional AND beginner — not childish, not jargon.** Don't talk down ("これは〜という仕組みです" padded), don't
  drop raw jargon ("自律ラン", "SDK", a repo name) untranslated. Introduce a proper noun once in plain words, then use it.
- **Cite REAL, verified links — search, never fabricate, never delete to avoid the work.** If a source has no link,
  search the web/gh for it; if it genuinely doesn't exist, drop it (don't invent a URL). (Real failure: cited
  vinid/einstein-arena which does not exist; nearly deleted Symphony/Sutando instead of finding openai/symphony +
  sonichi/sutando.)
- **State the run scale: how many runs, how long, what model, how much earned.** Every experiment result needs
  "N runs / T minutes / which model / $X realised" — verify the live numbers with a command before writing them.

## MORE LESSONS (2026-06-27)
- **Block [0] (verdict box) is OPTIONAL — skip for narrative/visit pieces.** The hamburger template's [0]
  bullet summary is mandatory for "should you use it" tool reviews. For a personal-visit / cafe / event /
  travel piece, the hook IS the first scene and a verdict box at the top reads as AI listicle slop. Default
  for narrative pieces: open straight at [1] hook. Editor can ask for it back if they want skim-fast.
- **Title jargon: replace with FUNCTION/CATEGORY, not a translation of the same proper noun (spec #77,
  2026-07-17).** A title with 2+ terms a domain-zero reader has never heard of fails deslop-gate's B8
  check, on every platform (zenn/devto's "technical English is fine" exemption is body-only, not title —
  a title is what a stranger sees before they know they are the technical audience). Fix by describing
  WHAT the thing does/is, not by translating or transliterating its proper name (バーチャルズ still reads
  as jargon to someone who has never heard of Virtuals, even in katakana). If a proper noun is genuinely
  irreducible, push it into the body's first sentence instead of the headline — the title's job is to
  promise the finding, not name the vendor. Real example (2026-07-17, the exact case B8 exists to catch):
    | Jargon title (B8 FAIL, 2 terms) | Function/category title (B8 PASS) |
    |---|---|
    | Virtualsの求人市場を覗いたら、承認はSolidityのハンコ一つで済んでいた | AIが仕事を受発注し合う市場を覗いたら、検収係は中身を見ずに5%もらっていた |
    | 元手ゼロのAIは稼げるか。Base上のVirtuals ACP、検収役はハンコで5%取れた | 元手ゼロのAIは稼げるか。エージェント向け取引所のVirtuals ACPを試してみた |
  Note "Virtuals" (a specific protocol) became "AIが仕事を受発注し合う市場" (what it IS), and "Solidity's
  ハンコ" (the specific language + a half-translated pun) became "検収係は中身を見ずに" (what actually
  happened) — same finding, same 5% number, zero jargon.
- **De-Englishize aggressively before publish.** Even when "everyone uses" the English term, a JP article
  that drops untranslated English into the body sentence reads as AI translation. Sweep before publish:
    | Eng | JP |
    |---|---|
    | founder | 創業者 / 起業家 |
    | Bay Area | ベイエリア |
    | harsh feedback | 厳しいダメ出し |
    | bootstrapped | 自己資金で |
    | Series A | シリーズA |
    | valuation | 評価額 |
    | head of brand | ブランド責任者 |
    | captive portal | Wi-Fi 認証ページ |
    | absolutely hated | 絶対やめろ / ボロカスに反対 |
    | 1day pass | 一日券 |
    | conversion (the verb) | 売上に繋がる / 流入 |
    | YC alum | YC 卒 |
    | zoning OK | 用途地域的にOK |
  Keep raw English for: product/company names (Corgi, Y Combinator, Capital One), URLs, UI string quotes
  from screenshots ("Verifying receipt..."), and proper nouns (MacBook, iPhone).
- **stop-ai-slop-jp gate is MANDATORY before every JP publish.** Invoke `Skill: stop-ai-slop-jp` after the
  first draft and apply at least the 命題型H2→名詞句, false-agency, 中間温度, and English-sweep checks.
  Score ≥35/50 on the 5-axis rubric or rewrite.

## MORE LESSONS (2026-06-23)
- **名前は「アニッチャ」(katakana) in Japanese — never write "Anicca" in JP body.** Anicca has a Japanese name;
  in any Japanese article/post write アニッチャ. English brand "Anicca" only in URLs/repo names. (Same as rule 7:
  日本語名がある固有名詞は日本語で。)
- **Rule 14 SCOPE FIX — the closing [8]「最後に」about-us/CTA block IS allowed to name アニッチャ + link the repo,
  but MUST NOT say it is an AI (IDENTITY section, Dais 裁定 2026-07-18 — supersedes this rule's older wording).**
  Rule 14 ("never name our own Anicca") applies to COMPARISONS/landscape (don't surface our private stack as a
  rival). The end-of-article advertisement that says "we built アニッチャ, here's the link" is intended and fine —
  "we built an autonomous AI called アニッチャ" is not; write it the way a human founder would name their own project.
- **De-slop is for DRAFTING; once the editor approves a part, EDITOR PROTOCOL wins.** Run the de-slop gate on the
  FIRST draft. After the human editor has reviewed a block, do NOT re-de-slop it (even a 全角ダッシュ they left) —
  un-flagged = approved. The gate is a pre-review tool, not a license to keep changing reviewed text.

## VISUAL ASSETS — どこで 画像生成 / Mermaid / 表 を使うか（2026-06-23 searched + verified）
Decision rule (use the cheapest tool that keeps the TEXT correct):
- **表 (markdown table)** — 比較・数値・一覧・仕様。正確な文字/数字が要る tabular は必ず表。note/Zenn/Substack が native 描画。$0。
- **Mermaid → 図** — フロー・ループ・木・sequence・関係・state。少数のラベル付きノード＋矢印。Zenn は ```mermaid 直描画、note/Substack/X は kroki POST→PNG（`curl -X POST https://kroki.io/mermaid/png --data-binary @d.mmd -o f.png`、無料・インストール不要・検証済）。$0。
  Substack 向けは手で curl しない: `scripts/_shared/publish-substack-mermaid.sh publish <md> --title T [--mode draft|go]`
  が mermaid→PNG→Substack画像アップロード(`/api/v1/image`)→md差替え→draft作成を一括でやる（run.sh の substack-ja/en から
  既定で呼ばれる、spec #45）。`--mode go` は既存の enable-publish sentinel + `SUBSTACK_MODE=go` の二段ゲート必須、かつ
  verify-preview.py のvision gateを内蔵。kroki の flowchart は縦長(276x606等)が既定でSubstackは全画像を728px幅にストレッチ
  するため、アップロード直前にPIL(要install)でキャンバスを横パディング(拡大なし・透明・中央寄せ)して縦横比を補正済み
  （task #13。実測: 276x606→520x606→728px幅ストレッチ時848px、900px未満に収まる。rule 71と同じ症状への対策）。
  publish後は自己GETでtitle+画像存在を検証してから戻る。認証は`SUBSTACK_SESSION_COOKIE`直curl（PIL以外はstdlibのみ）。
- **画像生成 (gpt-image-2)** — サムネ/カバー・概念ヒーロー・挿絵・mockup・シーン。**正確な密ラベルが不要**な"絵"だけ。日本語の長文は崩れ得るので図/表には使わない。
  - 経路（APIキー不要・ChatGPTサブスク）: `chatgpt-imagegen` skill（**web backend = ログイン中ブラウザのChatGPTで生成、Codex-usage を消費しない**。推奨）／fallback = `codex exec --full-auto "Generate ... gpt-image-2 ... save to <path>"`（Codex-usage 課金、ブラウザはログイン1回だけ）。
- ルール: 「文字/数字が正確であるべき → 表 or Mermaid」「雰囲気/絵 → 画像生成」。画像生成を表・密ラベル図に使わない。

### 他の作図ツール（searched 2026-06-23）— 採否
| ツール | 何 | 自動化(no-human) | 採否 |
|---|---|---|---|
| **Mermaid + kroki** | text→図、API描画 | ◎ 完全headless | ★ 本命（採用）★ |
| **gpt-image-2 (codex/chatgpt-imagegen)** | text→絵 | ◎ headless or browser | ★ 採用（絵のみ）★ |
| Excalidraw | OSS・手描き風、`@excalidraw/mermaid-to-excalidraw` で Mermaid→編集可能図 | △ 描画はReact/canvas、export に headless browser 要 | 手描き風が欲しい時の任意。コア外 |
| Napkin AI | text→infographic、無料枠 | ✕ SaaS/ブラウザのみ・公開APIなし・無料枠制限 | no-human に不向き。inspiration 用のみ |
| PlantUML/Graphviz | kroki が同経路で描画可 | ◎ | 必要時のみ |

## VERIFICATION GATE — MANDATORY before any publish (Dais 2026-06-23, emphatic)
NEVER publish without first VERIFYING the RENDERED output with your own eyes. "It uploaded" ≠ "it looks right."
Gate = render the draft on the actual platform → screenshot → Read the image → confirm: tables not crushed,
images in right place/order, headings clean, no raw markdown/markers. Only then publish.
- Screenshot a logged-in draft WITHOUT touching the daily-driver: ephemeral `launch_context(headless=True)` +
  `ctx.add_cookies([{name,value,domain:".note.com",path:"/"}])` + goto the draft edit URL + screenshot. (scripts/verify-screenshot.py)

## PLATFORM QUIRK — note.com does NOT render Markdown tables (they collapse to a "| a | b |" text blob)
Fix: render EVERY markdown table to a PNG and embed as a body image (scripts/note-render-tables.py: md table →
styled HTML → cloakbrowser element screenshot). Same for Mermaid (kroki PNG). On note, ALL visuals = images.
(Verified 2026-06-23: 19 tables + 6 mermaid → images; first attempt with raw md tables was crushed, caught by the gate.)

## NOTE AUTH — reuse the daily-driver login without touching the browser
note is logged into the daily-driver (CloakBrowser, Chromium, `--use-mock-keychain`). Extract cookies from disk:
copy `~/.cloak/profiles/daily-driver/Default/Cookies`, decrypt v10 values with AES-128-CBC,
key=PBKDF2("mock_password", b"saltysalt", 1003, 16), iv=16 spaces, strip 32-byte domain-hash prefix.
Feed the cookie dict to note-mcp `Session(cookies=..., user_id, username="anicca123")`. NEVER launch on the
daily-driver profile (SingletonLock = it's alive). Draft flow: create_draft (NO publish_article) →
upload_body_image per table/fig (needs NUMERIC note id) → update_article (needs KEY). eyecatch via note-mcp is
buggy (response lacks 'url'); fallback = insert thumbnail as the first body image (hero).

## NOTE PUBLISHER v2 fixes (2026-06-23, all caught by the verification gate)
- **Un-blockquote before publishing to note.** note renders `> ...` blockquotes oddly (bold title + body merge;
  a `> |table|` inside is NEVER rendered). Strip leading `> ` from the whole body (scripts/note-stage1-render.py)
  so 補足 sections flow normally AND blockquoted tables become normal tables → caught by the table renderer.
- **Render table PNGs BIG.** note shrinks images to content width; small fonts become unreadable. Use font-size
  ~30px, padding 16/24, 2px borders → sharp & legible even shrunk.
- **Strip internal markers/numbers from headings.** Remove `[0]..[8]` block numbers + any draft note like
  "（テキストのまま・画像化しない）", and reword body cross-refs ("[4]で説明した"→"先ほど説明した") — PLAYBOOK rule 4.
- **Series thumbnail = GENERIC, reusable.** The cover for a whole series must carry NO per-article numbers
  (no "無料AI/80分/+$0.17"). Just the series title + product name + art. Regenerate via codex gpt-image if it leaked stats.

## NOTE PUBLISHER v3 (2026-06-23) — uniform table text + footnote separation (verified)
- **All table PNGs MUST be the SAME width** (e.g. 1080px body, table width:100% table-layout:fixed, screenshot
  the BODY element). note scales every image to content-width, so equal source width → equal scale → IDENTICAL
  displayed font across all tables. (Bug fixed: element-screenshot gave varying natural widths → wildly different
  text sizes per table. VERIFY by checking every tbl PNG is the same px width.)
- **補足 (footnote): make the title a heading, not bold-paragraph.** On note `**title**\n\nanswer` does NOT visually
  separate; convert the 📌補足 title line to `### …` so note renders a spaced heading + the answer below.
- **Never leave internal file-path/source notes** ("取材ソース全文: docs/research/…") or `[N]` block numbers in headings,
  or internal draft notes ("（テキストのまま…）") in the published body. Strip them.

# ============================================================
# PUBLISH ENGINE — COMMON (every platform) vs PER-PLATFORM (2026-06-23)
# Goal: "post this article" → all platforms, JP+EN, no human. This is the spine for the weekly automation.
# ============================================================

## COMMON publish rules (apply to EVERY platform: note/Zenn/Substack/X/dev.to/TikTok)
1. **Auth = the daily-driver CloakBrowser, reused, never re-login.** Dais logs into each platform ONCE on the
   daily-driver (`~/.cloak/profiles/daily-driver`). anicca reuses it. NEVER launch_persistent_context on that
   profile (SingletonLock = it's alive) and NEVER kill it. Get what you need from disk: copy the Chromium
   `Cookies` sqlite, decrypt v10 with AES-128-CBC key=PBKDF2("mock_password",b"saltysalt",1003,16), iv=16 spaces,
   strip 32-byte domain-hash prefix. (memory: feedback_never_close_daily_driver_browser)
2. **Draft first, never publish blind.** Create as DRAFT → run the verification gate → only then publish.
3. **VERIFICATION GATE (mandatory).** Render the draft on the real platform → screenshot (ephemeral
   launch_context + add_cookies, NOT the daily-driver) → Read the image → confirm: no crushed tables, images in
   right order/size, headings clean, no raw markdown/HTML/markers. "It uploaded" ≠ "it looks right." (scripts/verify-screenshot.py)
4. **Visual assets:** diagrams = Mermaid→PNG (kroki, free); data = tables; illustration/cover = gpt-image-2 via
   codex/chatgpt-imagegen (ChatGPT sub, no API key). Series cover = GENERIC (no per-article numbers), reusable.
5. **De-slop + no internal leakage:** run the de-slop gate; strip `[N]` heading numbers, internal notes
   (（テキストのまま…）), file-path source lines (取材ソース: docs/…). Reword `[N]` body cross-refs.
6. **Idempotent + ledger:** record (article-hash × platform × lang × url); skip if already posted.

## PER-PLATFORM specifics
### note.com  (scripts/note-stage1-render.py → note-stage2-publish.py)
- **No markdown tables / blockquotes render badly.** Un-blockquote the whole body (strip `> `). Render EVERY
  markdown table (incl. ones revealed by un-blockquoting) → PNG; **all table PNGs the SAME width** (1080px body,
  table width:100%, screenshot the BODY element) so note's content-width scaling gives IDENTICAL displayed font.
- **Compact display:** embed table/diagram images via `generate_image_html(url, width=480, height=480*H/W)`
  (note figure HTML; markdown passes it through) → compact & sharp. Hero cover + infographic stay full width.
- **補足:** convert the `**📌補足：…**` title to a `### …` heading (note won't separate a bold paragraph).
- **Auth:** _note_session_v5 cookie (from daily-driver, decrypted). note-mcp `Session(cookies, user_id,
  username="anicca123")`. Flow: create_draft (NO publish) → upload_body_image per asset (NUMERIC note id) →
  update_article (KEY). eyecatch endpoint is buggy (no 'url'); fallback = thumbnail as first body image (hero).
### Zenn / Substack / dev.to / X-Articles / TikTok  — TODO: fill specifics as each is done (they DO render
  markdown tables + Mermaid natively on Zenn; Substack/dev.to differ; X-Articles = Playwright; TikTok = 1 image).

## EXPLAINER INFOGRAPHIC — generate one per article, place RIGHT AFTER block [0] (Dais 2026-06-23)
Every article gets ONE generated explainer infographic (9:16 vertical, gpt-image-2 via codex) that explains
ONLY what the SUBJECT is (the agent/tool itself) — NOT what we tried, NOT anicca, NOT results. It is the
TikTok/social hook: post the one image, put the explanation in the caption, link the note article in the caption.
Always insert it in the body **immediately after block [0] (最初に：この記事は何か)**. Style = navy→teal flat-vector,
big JP title, a friendly illustration, 3–4 short icon+label cards. Keep labels short (gpt-image text is best short).

## NOTE image sizing (researched 2026-06-23, note help center)
note auto-scales any image to content width (~620px); the editor "shrink" button = 60% (~372px). Via API there
is no class, so control size with `generate_image_html(url, width=W, height=W*H/Wsrc)`:
- data tables / diagrams → width≈480 (compact, sharp)
- explainer infographic → width≈500 (medium; full-width 620 was "too big" per Dais)
- hero cover → full width (markdown ![](url))
Two infographics per article: (a) SIMPLE 4-card cover = TikTok hook (1 image+caption+link), (b) DETAILED modular
encyclopedia-style = in-article "understand it in one glance" (6+ labelled modules). Keep both files.

## MONETIZATION (the point — money from writing)
The skill must AUTOMATE earning, not just posting. For every newly published note article, the executable desired
state is `scripts/note-publish/note_monetization_policy.py desired-state`: one-time purchase, JPY, **¥500**, with a
useful free preview before the paywall. Article count, follower count, and a price suggestion cannot switch a new
article to free. Zenn and dev.to remain full-article discovery funnels. Substack uses its own subscriber-paywall
contract; never reuse note's Japanese derivative as an English paywall artifact.

### note market observations (historical evidence, not the strategy SSOT)
The 2026-07-16 note API measurement remains useful evidence about what the market did, but it does not override the
fixed ¥500 desired state above:

| creator      | followers | notes | membership | observed sale                  |
|--------------|-----------|-------|------------|--------------------------------|
| punimaru_dev | 177       | 3     | NO         | ¥3,480 single, 928 likes       |
| kon_ai       | 188       | 4     | NO         | ¥3,980 single                  |
| 09pauai      | 1,567     | 4     | NO         | ¥1,480 single, 283 likes       |
| shiro_life0  | 2,334     | 11    | NO         | ¥100,000 single, 794 likes     |
| tothinks     | 6,409     | 119   | YES        |                                |
| kajiken0630  | 44,877    | 295   | YES        |                                |
| goto_finance | 93,822    | 1,072 | YES        |                                |

Measured membership pages showed ebithai at ¥770 + ¥1,540, kun1aki at ¥980, and star_english4788 at ¥980 +
¥9,800. Measured paid free-part lengths were 2,529 characters for punimaru_dev and 2,467 for shiro_life0. These
figures document past market shapes; they are not current pricing instructions.

GENERAL LAW: **measure the current niche instead of copying a monetization rule from another niche or an old
winner.** The note API exposes `hasCircle`, `price`, and `like_count`; re-measure those facts before interpreting a
market pattern. The previously copied ChatGPT研究所 account no longer sold on note when re-measured, which proves
that winner instructions decay.

### other platforms
Zenn is a funnel, permanently: full articles stay free there (discovery + trust building, feeding note/Substack
subscriptions) — it is never a monetization target. Substack uses a useful free preview plus subscriber paywall.
Its executable SSOT is `scripts/substack-publish/substack_paid_payload.py`: create and same-ID repair both emit
`audience=only_paid`, `should_send_free_preview=true`, and exactly one ProseMirror paywall node after at least
1,000 useful visible characters. The live wrapper must prove those three facts again from authenticated
`GET /api/v1/drafts/{id}` before publish; a successful write response is not monetization evidence.
X, dev.to, and video surfaces use their own product/channel reward contracts; no platform inherits note pricing.

### note conversion preview derivative — always through the shared script
Never hand-truncate. Generate with `scripts/_shared/make-free-version.py --markdown-file <original.md>
--note-url <url> --price <int> --paid-contents "<有料側見出しの正確な名指し>" --summary-file <bullets.md>
--out <free.md>` (proven shape: `~/.cloak/note-work/2026-07-12-agent-economy-jp-x-free.md`). The agent writes
and hands in `--summary-file` (3-5 まとめ bullets, no slop) and `--paid-contents` — the script only cuts+assembles,
mechanically, at `--after-chars` (default 2500, same as `note-publish/publish-paid.py`'s PAYWALL_AFTER_CHARS;
re-measure per article, the default rarely matches where you actually want to cut — see the script's own docstring).
A free version that ends on a teaser H2 (e.g. "## この続きは") is FATAL — the script guards this itself.

## META — run AND iterate every time (no-human 10k MRR)
Each session: DO the real work (write+publish+monetize) AND generalize the lesson into this skill so next week is
automatic. The end state = a Claude Code Routine that writes→images→verifies(screenshot)→publishes→sets paywall→
posts to all platforms, daily, no human. Many anicca (different harness/model) each earn this way and share
experience via GitHub issues. Given creds once → earn forever, no human in the loop.

## CAPAFY — sell this skill (article→publish→monetize engine) — researched 2026-06-23
This whole skill (write deeply-researched article → images via Mermaid/gpt-image → verify by screenshot →
publish to all platforms with daily-driver auth → set the note paywall + membership) IS a sellable product on
Capafy. Package the public version with our creds/profile/topic-queue REMOVED; the buyer plugs in their own
daily-driver profile + platform logins. Two modes (HARD RULE capafy_publish_ritual): subscription (we host the
LLM key) or Download (buyer's key, source shown). This is the foundation of making money from article creation —
both directly (our 10k MRR) and by selling the engine.

## NOTE PUBLISH — HARD LESSONS from the first article (2026-06-24, several mistakes — never repeat)
1. **Eyecatch (見出し画像) ≠ body image.** Set the cover via the top `画像を追加` button (aria-label="画像を追加") →
   「画像をアップロード」→ file chooser (`expect_file_chooser`) → set thumb.png → crop dialog「保存」. Do NOT embed
   the cover as the first body image (renders as a duplicate). Verify `eyecatch != null` via the note API.
2. **NEVER keyboard-Backspace/Delete to remove a body image in ProseMirror.** It deleted an adjacent heading char +
   linebreak and MERGED two blocks ("最初に：この記事は何か" → "…何Automaton とは"). To remove a body block use the
   block ︙ menu (aria-label="メニューを開く") → 削除. After ANY body edit, grep the body for merged/broken text
   (e.g. a heading running into the next paragraph) BEFORE publishing.
3. **Verify the PUBLISHED article as a logged-OUT visitor + the note API — never the owner/editor view.** The owner
   sees full content even when gated, so "looks free to me" is meaningless. Ground truth =
   `GET https://note.com/api/v3/notes/{key}` → check `price`, `is_limited`, `can_read`, `eyecatch`. Plus a NO-cookie
   `launch_context` screenshot of the gate. (Dais caught both the missing eyecatch and the owner-view confusion.)
4. **一時保存 only saves a DRAFT.** Editing a PUBLISHED article needs re-publish to go live. The button is
   「投稿する」 the first time, 「更新する」 on re-publish (already-published articles). Watch for either.
5. **The membership plan must be published (公開 toggle ON)** or the gate shows "この記事は現在販売されていません"
   (no subscribe path). Toggle = the switch left of the 公開 label in the plan card → click → confirm 「公開する」.
6. **Pure membership gating (no single price):** 記事タイプ=無料 + 記事の追加→メンバーシップ→「メンバー全員に公開」
   追加 (→ 特典記事) + click「試し読みエリアを設定」→ click the「ラインをこの場所に変更」just before the paid
   section. price stays 0, is_limited=true, can_read=false (non-members). = ChatGPT研究所 read-all model.
7. **目次 — MANDATORY: use note's AUTO 目次 (clickable jump links), and keep it SHORT by heading levels.**
   note's auto-目次 lists EVERY 大見出し(h2)+小見出し(h3) and can't be filtered (note公式 spec). So the ARTICLE
   STRUCTURE is the lever: **only section titles are 大見出し(h2 / `##`); ALL sub-points are BOLD text, NEVER
   小見出し(h3 / `###`)**. Then the auto-目次 naturally shows only the ~10 big titles, clickable. Insert the auto
   目次 right after block 0 via the gutter [＋]→目次. If an article already has h3 sub-items, DEMOTE each to a
   bold paragraph: caret in the h3 (DOM Range) → **`Meta+Alt+0`** (reliable keyboard = heading→paragraph; the
   ︙-menu toggle is FLAKY/inserts a new one — do NOT use it) → select line → `Meta+b`. ★ WARNING: a loop that
   demotes "the first h3" can accidentally hit h2 — after demoting, RE-VERIFY h2 count == your big-title count
   and re-promote any wrongly-demoted title with `Meta+Alt+2` (大見出し). Verified on the Automaton article
   2026-06-24: 29 h3 → 0, auto-目次 = 10 big titles only.
8. **All scripts + cookies + screenshots in REAL persistent files** (skill `scripts/note-publish/` + `~/.cloak/
   note-work/`), NEVER /tmp (reboot/disk-cleanup wipes it mid-task). cookies = mock-keychain decrypt of the
   daily-driver `Default/Cookies` via /opt/homebrew/bin/python3 (has `cryptography`).

## NOTE — DRAFTS OK / PUBLIC NEVER UNATTENDED (Dais 2026-06-24)
Drafts on the note account are fine and RECOMMENDED — run the pipeline in draft so you can VISUALLY verify the
render passes before anything is public. NEVER publish to live 本番 (投稿/更新/plan 公開) unattended or without
Dais's explicit go; the public channel stays clean (only articles that clear the bar — the Automaton article is
it). Enforced deterministically: daily-run.sh exports NOTE_FORCE_DRAFT=1; publish.py + toggle-plan.py refuse to
click 投稿/更新/公開 unless NOTE_MODE=go AND NOTE_FORCE_DRAFT!=1. Creating a draft during testing = fine, not a
violation; only a PUBLIC post is.

## ARTICLE BODY — NO infographic in-article; heading changes in MARKDOWN; verify image count (Dais 2026-06-24)
- NO explainer infographic inside the article body. The cover = the note eyecatch (見出し画像) only. (The
  simple 4-card infographic is a separate TikTok hook asset, NOT in the article.)
- Heading-level changes (make sub-points bold so the auto-目次 stays short) are done in the MARKDOWN SOURCE
  (### → **bold**), NEVER via a keyboard demote loop in the note editor (it silently DELETES image nodes —
  it wiped ~25 diagrams/tables once). Re-render the body from markdown (rebuild-note-body.py / note-stage).
- ALWAYS verify after any structural body edit BEFORE publishing: editor img count + screenshot every section
  + Read them. Image count dropped → STOP, do not publish.
- Persistent assets only: render tables (HTML→PNG) + mermaid (kroki) to ~/.cloak/note-work/automaton-assets,
  NEVER /tmp. rebuild-note-body.py uploads them and places each at its @@TBLn@@/@@FIGn@@/@@FUNDn@@ marker.

## ★ ONE-SHOT NOTE PUBLISH PIPELINE (canonical order — get the clean state on the FIRST pass) ★ (2026-06-24)
The broken multi-day session happened because steps were scattered/ad-hoc + a keyboard demote deleted images +
/tmp got wiped. THIS is the fixed canonical sequence. publish-to-note.sh `publish` runs these IN ORDER:
  0. WRITE (writer-agent) emits markdown with: `##` = section titles ONLY (these become the auto-目次);
     sub-points = `**bold**`, NEVER `###` (so the auto-目次 stays short); NO in-article infographic; tables as
     markdown, diagrams as ```mermaid; setup screenshots as ![](images/automaton/<file>.png).
  1. cookies: extract from the daily-driver profile → ~/.cloak/note-work/note-cookies.json (NEVER /tmp).
  2. render assets → ~/.cloak/note-work/<slug>-assets/ : each markdown table → PNG (HTML, uniform 1080px width),
     each ```mermaid → kroki PNG. Persistent dir, NEVER /tmp.
  3. rebuild-note-body.py: upload every asset, place at its @@TBLn@@/@@FIGn@@/@@FUNDn@@ marker; NO infographic,
     NO body-hero; update_article (DRAFT). (This is the image-safe path — do structure in markdown, not the editor.)
  4. set-eyecatch: 見出し画像 = thumb.png via the top `画像を追加` button (cover only; not a body image).
  5. insert auto-目次 after block 0 (gutter [＋]→目次). Short because sub-points are bold, not 小見出し.
  6. publish.py: 記事タイプ=無料 + メンバー全員に公開 + 試し読みエリア line just before `--paywall-before`.
  7. toggle-plan: plan 公開 ON. (guarded — needs enable-publish + NOTE_MODE=go)
  8. ★ VERIFY GATE (mandatory, before --mode go) ★: verify-note.py (API can_read/eyecatch) + screenshot EVERY
     section; the agent Reads them and confirms the IMAGE COUNT matches the asset count and nothing is crushed/
     merged. Image count dropped or a section lost its figure → FAIL, do not publish.
NEVER: bulk keyboard edits on a body with images; /tmp; in-article infographic; publishing without the per-section
image-count verify. The scripts are still Automaton-hardcoded — parameterize (md/key/slug/paywall) when wiring F.

## ★ ZENN ONE-SHOT PUBLISH (git-based sibling of the note pipeline) ★ (2026-06-24, built+verified)
Zenn = funnel（正本）: 全文無料を恒久維持。役割は発見面と信頼構築、note/Substack subscription への導線。常に有料化対象ではない。
Scripts: scripts/zenn-publish/ (zenn-adapt.py + publish-to-zenn.sh). Zenn = `git push` to the configured Zenn articles repo. The checkout is Life Manager-managed at `$WRITER_STATE_DIR/checkouts/zenn-articles`; its remote and public identity come from `ZENN_REPOSITORY_URL` and `ZENN_ACCOUNT` (never write the operator's handle into an article).
(SSH remote, NO inline PAT) deploys the article; mermaid + markdown tables render NATIVELY (NO image upload).
The article is a FREE HONEST explainer — it must NEVER claim a run/result (that lives only in the paid note).
  1. ADAPT (zenn-adapt.py): source md → zenn md. Frontmatter (single emoji, type tech/idea, ≤5 topics, stable
     a-z0-9 slug, published:false). CUT: the paid section (from `--paid-from` heading to end) + every first-person
     run/test claim (やったこと/結果 blocks, run-promise, 次の章 refs) + all local images. Un-blockquote;
     blank line around EVERY table (GFM needs it — else the next paragraph sticks to the table); honest closing,
     NO upsell/note link. (Article-specific stray lines → ZENN_CUT_LINES env.)
  2. NO-LIE GATE (`gate`): grep -F the run-claim phrase list (+ optional per-article numeric blacklist). Any hit
     = FAIL, block publish. This is the honesty guarantee.
  3. RENDER VERIFY (`render`): npm install (once) → `npx zenn preview` → AGENT screenshots EVERY section + Reads
     them (mermaid SVG count, tables not broken, no slop). The run-and-verify caught a mangled leftover sentence
     in the live article — ALWAYS do this. Zenn has NO browser-session login anywhere in this publish path
     (git push only, see the line above) — the STEP 6.5 render-verify-draft.sh gate for platform=zenn is
     therefore git/file-based (frontmatter + mermaid + table checks on the pushed .md), never a zenn.dev
     dashboard screenshot (task #76, 2026-07-17: an earlier attempt wasted a full session trying to browser-login
     to a session zenn never needs — read this section before touching zenn session/login code again).
  4. DRAFT (`draft`): push published:false → confirm NOT in Zenn public API.
  5. PUBLISH (`publish`, gated): needs `enable` sentinel + ZENN_MODE=go; re-runs the no-lie gate as a hard guard;
     sets published:true; push ONCE; verify LIVE (200 + API + render).
  6. RATE-LIMIT: Zenn limits NEW articles by 直近24時間の投稿数. Publish 1 new article/window; NEVER toggle
     published or burst-rename old articles (that tripped it). A 403/not-in-API after push = rate limit, not a
     bug. Persist `state/runs/<run>/gates/zenn-deferred.json`, hand it off, and let the independent one-shot
     launchd worker re-trigger the same slug after the window clears; neither `article-daily.sh` nor the worker
     sleeps. Canonical username = `anicca` (anicca123 redirects).
  7. ★ SLUG (= filename minus .md) MUST be a-z0-9/hyphen/underscore only, 12-50 chars (measured 2026-07-16,
     zenn.dev/zenn/articles/what-is-slug). A Japanese/mixed filename is silently SKIPPED at deploy — Zenn never
     errors, and an anonymous 404 check cannot tell "skipped" from "not yet rate-limit-flushed", so verify with
     an authenticated dashboard screenshot, not a public API/HTTP check, when in doubt.

## MORE LESSONS — 体験/観光 récit (2026-06-27, Corgi Cafe SF live ground-truth)
Dais published a fully-edited version of `docs/articles/corgi-cafe/article-jp.md` to X Articles after I shipped the
draft; comparing draft↔published surfaced 14 systematic edits worth turning into rules. ★ Apply these to EVERY
体験/観光 récit (= I visited X, here is what it is) ★ — subject choice still follows the reader-job and evidence rules above.

27. **[0] verdict box is optional, and for récits = DELETE it.** The hamburger [0] box (おすすめ/しない bullets above
    the fold) is correct for mechanism/run pieces where the reader needs a fast verdict. For 体験 récits the
    lede paragraph IS the verdict — adding [0] doubles the opening. Dais removed the entire [0] block.
28. **「結局〜は何なのか」 まとめ block is BANNED.** A trailing bullet list that re-states what the body just
    described = AI-slop tell. If the article is doing its job, the reader already knows. Dais deleted the entire
    `## 結局このカフェは何なのか` section. (Keep the おすすめ/しない 2-bullet summary at the very end — that has a
    different job: filtering the reader.)
29. **CURRENCY: `$` → `ドル`, K/M略 → 数字展開.** `$100k` → `年収10万ドル`. `$2M` → `200万ドル`. `$5,000` → `5,000ドル`.
    `$7.20` → `6.43ドル`. Never mix `$ + 円換算` (`$5.50 (約850円)` = redundant) — pick ONE currency, the local one
    the audience uses.
30. **OCCUPATIONS / CONCEPTS: 完全日本語化.** `Founder's Associate` → `創業者補佐` (NOT `創業者の右腕`). `bootstrap` →
    `資金調達なしで`. `equity` → `株式`. `Y Combinator 卒` → `Y Combinatorの卒業生`. `紹介料` → `紹介報酬` (more
    formal recruiting-flyer voice). `9-5希望` → `9-5を希望する者` (matches recruiting-flyer formality).
31. **PROPER NOUNS: stay English when they are brands/streets/cities.** `Corgi Cafe` `Y Combinator` `Founders Fund`
    `Boardy` `Goby` `Claude Lane` `Salt Lake City` `Atlanta` `Chicago` `London` `Dallas` `New York` `San Francisco`
    → all left English. Personal NAMES go katakana: `Nico` → `ニコ`, `Emily` → `エミリー`, `Trudy` → `テュルーディー`.
    Rule of thumb: brand/place/product = English; human name = カタカナ; SF (abbreviation) → サンフランシスコ when
    spelled out.
32. **REAL-WORLD固有名詞を現地で取る — invented placeholders are LIES.** I guessed `corgicafe` for the WiFi name;
    real network = `Corgi Patrons`. I wrote `ホットコーヒー`; what I actually drank = `コルタード`. The drink name and
    the WiFi SSID and the menu item are FACTS, not vibes. For any 実訪 piece, before writing add an OCR/transcribe
    pass over EVERY photo and capture: signage text, menu items, WiFi SSID, network branding, sticker text, posters.
    Those become the proper nouns in the body.
33. **ADD A PHOTO CAPTION (italic line directly under the image).** A photo without a caption is decoration; a
    photo with a caption is a fact. Dais added `*手前の柱にあるのは、Boardyというネットワーキング用のAIエージェントの宣伝*`
    under the counter photo, and that moved the Boardy fact OUT of the body into the picture, which let the body
    paragraph stay clean. Default: every content image (not the cover) gets one italic caption line beneath it
    that names the most interesting concrete thing visible.
34. **ADD ONE LINE OF 詩 (a wish/forecast sentence) to lift temperature.** Dais added
    「ここから次のGoogleが生まれるのかもしれません。」 after the customer-mix paragraph. That one line transforms a flat
    observation into a thesis. Permitted ONCE per piece (twice = sentimental). Use 推量 ending (`〜かもしれません`,
    `〜のような気がします`) — never断定 (`〜です`) for a wish.
35. **OPINION H2 of the form 「X が一番 Y だった」 is ALLOWED (overrides stop-ai-slop-jp命題型H2-ban).** stop-ai-slop-jp
    forbids 命題型 H2, but Dais's `机に置いてある「採用ビラ」が一番 SF っぽかった` is exactly that form and it works.
    The distinction: ★ asserting a PERSONAL JUDGMENT (「一番…だった」「いちばん刺さった」「最も…だと思う」) is fine — the
    reader knows it is YOU declaring it ★. Asserting a FACT in命題形 (`AI は世界を変える`) is the banned form. Use opinion-H2
    sparingly (1-2 per piece) for the section that carries your strongest take.
36. **AGGRESSIVELY CUT IF NOT LOAD-BEARING.** Dais's edits cut: the receipt-verification mid-step, the Goby-Whale-
    Laboratories aside, the `次のレシートまで: 60分` bullet, the `Erewhon 風の $14 スムージー` line, the
    `カフェに見えるオフィスに見える宗教施設` cute-but-empty trope, multiple bullets in おすすめ/しない (kept only the sharpest
    one of each). For every sentence ask: "if I cut this, does the reader lose anything?" If no, cut.
37. **CONDENSE BACKEND/MECHANISM ASIDES.** When the article is a 体験 récit, the WiFi-vendor backstory (Goby /
    Whale Laboratories / wlab.surf) does NOT belong in the body — the reader is here for the SF cafe, not the
    SaaS stack. Push such asides to a 📌補足 at the end, OR cut them entirely.
38. **CONSOLIDATE related H2-block content; resist serial-section creep.** I had 3 H2 for the WiFi-flow steps
    (QR → SMS → code); Dais collapsed them to one paragraph. Each H2 must carry its own weight; if 2 sections share
    a single thought, fuse them.
39. **MOVE color/personality fragments to where the thread lives.** I put the「週7日勤務 / オフィスで寝泊まり / 焼けた死体」
    paragraph at the END of the 採用ビラ section. Dais moved it UP into the 会社紹介 section because that thread is
    about WHO Corgi is, not the recruiting pitch. When editing, ask: "which earlier section is THIS paragraph really
    about?" and move it there.
40. **VERIFICATION GATE (mandatory for any 体験 récit) — fact-check against PHOTOS, not memory.** Before publishing,
    re-open every photo from the visit and for each: read signage/menus/labels and cross-check the draft body
    sentence-by-sentence. The 14 corrections Dais made were almost all "what's in the photo" vs "what I typed from
    memory" mismatches that a 60-second photo-rescan pass would have caught.

41. **★ NO DIARY — the article teaches a CONCEPT to a stranger; it is NOT a log of what happened to US (Dais 2026-06-28, furious) ★.**
    The reader does not know this field and came to UNDERSTAND THE CONCEPT. They do not care what happened to our
    machine, our disk, our infra, our session, our "security tax" moment, a cron we fixed, a tool that froze. ★ NEVER
    put an internal incident / accident / our-environment-failure into the article body. That is a diary entry and it
    is BANNED. ★ This is stronger than rules 1 and 16: even a "we ran it and our disk filled up" aside is OUT. Keep
    ONLY: the concept, explained for a beginner, with concrete examples that TEACH (a small worked example is fine if
    it illustrates the concept neutrally — 「例えばこうです」 for the reader — NOT 「私がやったらこうなった / こういう事故が起きた」).
    Strip all first-person experience-narration from an explainer (私が直した / 体で分かった / 〜を食らった瞬間). The ONLY
    allowed self-reference is the closing [8] about-us/CTA, and even there it names the product + link, not a story of
    our day. When in doubt: if a sentence is about OUR experience rather than the reader's understanding, cut it.
    **FORM SCOPE:** this rule is blocking for explainer, how-to, comparison, and report. A field-note, opinion, or
    case-study may narrate verified experience when that experience is evidence for the reader's job, but a queue
    card alone never grants that exception.

42. **[0] intro for an explainer: header is 「概要」 (or none), NOT 「この記事は何か」 (Dais 2026-06-28 — unnatural).**
    「この記事は何か」 reads wrong/AI-ish in JP. Use 「概要」 or open straight into the article. Keep the [0] block as a
    clean short list (un-blockquoted — note crushes `>` blockquotes), tight and organized, not a wall.

43. **All article diagrams = Mermaid (→ kroki PNG), never raw ASCII boxes in the body (Dais 2026-06-28).** ASCII
    box-art in a ``` code block is not a diagram; convert every conceptual figure to a ```mermaid block so the publish
    pipeline renders it to a clean PNG. Terminal/command OUTPUT (a real pytest result) may stay as a code block; only
    hand-drawn box diagrams must become Mermaid.

45. **★ DIAGRAM AUTHORING for cross-platform images — keep every Mermaid figure a SIMPLE chain of ≤6–7 nodes; NEVER use `subgraph`/2–3 columns to "shorten" it (Dais 2026-06-28, after a long X size fight) ★.** Mermaid auto-layout makes a multi-column/subgraph diagram absurdly WIDE (a 6-part anatomy went 1717px then 1920px wide), which at a fixed column width (X = 587px) downscales the text to microscopic. A plain `flowchart TD` chain of ≤6–7 nodes renders ~280–430px wide → readable. To avoid blow-up: (a) fold a gate/branch into the LAST node's label (`④外部接続（MCP）→ 人間ゲート`) instead of adding branch nodes; (b) short node labels; (c) if a concept needs >7 nodes, split into two figures or move detail to prose. The X pipeline auto-pads narrow PNGs to ≥600px (prep-x-md.py `_pad_to_col`) and `x_fullverify` FAILS on >650px (too tall) AND <110px (too flat/tiny-text) — heed both. (Real fight 2026-06-28: a 9-node vertical anatomy showed at X 587×783 = "too big"; 3-col/2-col rewrites went microscopic; the fix was a 6-node TD chain → 587×601 readable.)

44. **★ note diagram SIZE: embed Mermaid/diagram PNGs via generate_image_html at a CAPPED display width, NEVER full-width markdown `![](url)` (Dais 2026-06-28, angry) ★.** note upscales ANY body image to ~620px content width. A few-node mermaid PNG has a small canvas with ~16px text; blown up to 620px the text becomes huge and the diagram covers the whole page. FIX: for each diagram read its natural pixel width and embed with `generate_image_html(url, width=min(naturalW, 460), height=width*Hsrc/Wsrc)` so note does NOT upscale it. Data tables can stay wider (~560–620). After embedding, ★ JUDGE THE RENDERED SIZE on the browser ★: a diagram should occupy ≤ ~half the column and its text should be close to body-text size, not 2–3× it. If still too big, lower the cap and re-render. ★ VERIFYING ON THE BROWSER MEANS JUDGING SIZE/READABILITY/PROPORTION — not merely confirming "it rendered." ★ (Real failure 2026-06-28: I read the draft screenshot, said "clean, no breakage," and missed that every diagram's text was covering the page.)

## EDITORIAL-FORM ROUTING (replaces the old queue/lane voice split)

`lane` may remain in legacy ledger rows as provenance, but it does not select voice, structure, CTA, or gates.
The frozen `gates/topic-route.json` is authoritative:

1. **explainer** — define the subject and mechanism for a newcomer; reader-centred, no work-log chronology.
2. **how-to** — lead from the reader's target outcome through executable steps and verified constraints.
3. **case-study** — show a bounded situation, intervention, evidence, and result; first person only for a real
   attributable case.
4. **comparison** — establish decision criteria first, compare evidence symmetrically, end with who should choose what.
5. **field-note** — verified observation may use first person, but observation must earn a transferable reader lesson.
6. **opinion** — state the thesis and stakes early, then support it with evidence and answer the strongest objection.
7. **report** — organise measured findings by reader significance, not by the order in which work happened.

Queue cards provide angle/questions/sources as scaffolding, not a script. Do the research they point at; do not
reformat bullets into prose or infer field-note from their presence. Citations, identity safety, measurable CTA,
stop-ai-slop, editorial gate, and reader test apply to every form. Card selection remains deterministic:
`created` ASC, optional numeric `priority` ASC, then filename ASC.

## MORE LESSONS — xpub safe-iteration & EXIF (2026-06-28)

The Corgi Cafe EN translation needed 4 rounds of publish/verify and Dais had
to manually delete 2 duplicate images at the end because the engine had
failure modes the writer-skill didn't pre-check. Six more rules.

45. **EXIF orientation pre-check — apply BEFORE clipboard, never trust the source file as-is.** Every phone photo
    has an EXIF orientation tag (commonly 6 = "rotate 90° clockwise on display"). X Articles, note, Zenn, Substack
    all IGNORE EXIF — they render the raw pixels, so a portrait-shot photo appears 90° sideways in the published
    body. Pre-check: `python3 -c "from PIL import Image; print(Image.open(f)._getexif().get(274))"` for every source
    JPG. If != 1 / not None → run `ImageOps.exif_transpose(im).save(f, exif=b"")` to bake in the rotation and strip
    EXIF. The `x-article-publisher` engine now applies this inside `copy_to_clipboard.py` automatically (2026-06-28
    patch in `compress_image` + new `load_exif_corrected_bytes`), but the rule still applies at the writer level
    for any non-xpub channel.
46. **Consecutive images get a transition sentence.** Two `![]()` lines back-to-back in markdown with no text
    between them share the same `after_text` anchor in `parse_markdown.py`. The engine then pastes the 2nd image
    at the same paragraph the 1st was anchored to → 2nd lands BEFORE 1st → visible image swap. Fix: insert a
    1-line transition sentence between any two consecutive images (e.g. "You will be redirected to a screen with
    your WiFi code." between the QR-tablet photo and the WiFi-code screenshot). The engine now emits a stderr
    WARN + `consecutive_anchor_collision` JSON field whenever this pattern is detected; treat any such WARN as a
    HARD BLOCK and add the transition sentence before publishing.
47. **Iteration discipline — re-running `publish_md_to_x.py` DESTROYS the existing draft.** The engine's
    `cleanup_exact_title_duplicates` (default-on) deletes every prior draft with the same title before creating
    the new one. If a previous round had any manual fixes (Dais drag-reordering, an image manually re-uploaded),
    they are GONE the moment you re-publish. For incremental fixes after the first publish, use
    `replace_image_in_draft.py` (one image at a time) or `publish_md_to_x.py --no-cleanup-duplicates` (re-run
    without nuking the draft). Never re-publish for "I just want to swap one image."
48. **EN stop-slop = em-dash banned in body prose.** Apply the same `grep -F -- "—"` gate to EN articles that JP
    articles apply to `──`. Em-dashes in lede paragraphs ("Founders Fund — the Silicon Valley sound") slip past
    casual review but read as AI-generated EN. Replace with `:` / `;` / `,` / a period-split. Run the grep
    pre-publish; > 0 hits = block and rewrite.
49. **Verification is wheel-scroll + Read every screenshot — engine "POST-COND OK" is not enough.** The script's
    "image's previous block sibling contains anchor text" check gives false positives when two images share an
    anchor (returns OK when the image is actually mis-positioned). Real verification = open the draft in the
    daily-driver browser, drive `page.mouse.wheel(0, 700)` 15-20 times capturing screenshots into a folder, then
    `Read` every screenshot file yourself. The R1 Corgi run had 5 images mis-placed that the engine logged as OK;
    only the wheel-scroll pass caught it.
50. **EN translation rules for 体験 récit** (= my-visit-to-X pieces): currency stays native (`$X` in EN, `Xドル`
    in JP — don't back-translate `$5,000` to "5,000 dollars" or to `5,000ドル` when writing in EN), personal names
    stay in roman script (Trudy/Nico/Emily, not katakana back-romanizations), the opinion-H2 form
    `「X が一番 Y だった」` translates to `"X was the most Y thing about Z"` and is OK once per piece. The "結局"
    closing-summary block rule (= ban it) still applies in EN: no `"In conclusion"` / `"In summary"` bullets — if
    the body did its job, the reader already knows.

## MORE LESSONS (2026-07-13, agent-economy piece — title/概要/loop)

51. **★ TITLE: never lead with a term the reader doesn't know; the SUBTITLE must (a) define it and (b) promise the WHOLE scope.★**
    The reader has never heard "エージェント経済". A title that assumes it loses them. And a subtitle that
    picks ONE narrow angle ("誰も解けていない本物の証明" / "AIが自分でサーバー代を払う") betrays a piece that
    actually covers the whole field (定義→理想→現状→ハイプ→空白→我々) — Dais: "each subtitle seems too niche."
    ✅ FINAL FORM: `エージェント経済の作り方：誰が、どう作っているのか。そして何がまだ足りないのか`
    = main title names the topic, subtitle promises the full map (who/how) AND the gap. Test a subtitle by asking:
    "does this promise everything the article delivers, or just one section?"

52. **[0] 概要 = a plain summary. BAN the「この記事でわかること」frame (Dais 2026-07-13).**
    「この記事でわかること」/「この記事は何か」 reads as AI-listicle slop. It IS a 概要 — so just write the 概要.
    Each bullet = one complete thought (a claim, not a topic label). No meta-framing of the article itself.

53. **Surface and kill the reader's strongest COUNTER-argument in the body.** A beginner will push back with a
    smart objection; if the article doesn't answer it, they stop trusting you. (Real example: "AI同士で閉じた
    経済なら人間に頼ってなくて良いのでは?" → the answer is that the loop is NOT closed: AIs buy compute from the
    human world, so money leaks OUT constantly; internal circulation alone always drains to zero. That objection +
    answer became the strongest passage in the piece.) Hunt the objection BEFORE the reader does.

54. **PUBLISH ENGINE must be PARAMETERIZED before it can be a daily no-human loop.** The note/zenn scripts are
    still hardcoded to the old Automaton article (NOTE_ID/key/slug), so every new article needs a hand-written
    one-off script. That is the single blocker to "publish daily, no human." Parameterize (md path / note key /
    slug / paywall anchor) as the FIRST work item of the article loop — not the writing.

55. **Block-by-block editor review is the fastest path to a great piece.** Show the editor `title → [0] → [1]`,
    take the fix, then move to the next block. Per EDITOR PROTOCOL: change ONLY what is flagged; show un-flagged
    blocks byte-for-byte so the editor never re-reads the same text twice.

## MORE LESSONS (2026-07-13, block-review of the agent-economy piece — 3 real failures in one session)

56. **★ LOAD THE WRITING SKILLS BEFORE WRITING A SINGLE LINE — not after the editor rejects the draft.★**
    Real failure 2026-07-13: I rewrote [0] and [2] from raw instinct, without loading this SKILL.md or
    `stop-ai-slop-jp`. Both came back rejected. The rules already existed (rule 52 said exactly what to do
    with 概要). Writing from instinct when a written best-practice exists = reinventing the wheel = banned.
    Gate: before ANY draft/rewrite of ANY block, load (a) this SKILL.md PLAYBOOK and (b) `stop-ai-slop-jp`.

57. **★ FIX THE CONTENT, KEEP THE SHAPE. A format the editor approved is LOAD-BEARING.★**
    When told 「この記事でわかること は AI 臭い」 I deleted the whole bullet list and wrote prose paragraphs.
    The complaint was about ONE framing line, not the bullets. The [0] bullet shape exists so a reader skims
    the whole article above the fold (rule 52 + the old "don't change STYLE, only CONTENT" lesson). Kill the
    flagged phrase; keep every structural element the editor never flagged. When unsure which is flagged,
    make the SMALLEST possible diff and show it.

58. **★ EVERY BLOCK NEEDS ONE SPINE, STATED IN ITS FIRST PARAGRAPH, AND EVERY LATER PARAGRAPH HANGS OFF IT.★**
    The rejected [2] went: agent economy → sudden AI-agent lecture → detour into trading bots vs automation
    tools → 「次に、お金の側の言葉です」 → three definitions. No spine, so it read as a pile of glossary entries.
    The accepted rewrite declares the spine up front (「当事者になるには二つ要る。自分で判断できること。自分で
    払えること」) and then walks it in that order, folding the bot comparison into ONE clause instead of a
    paragraph. Test before shipping a block: can you say its spine in one sentence, and does every paragraph
    advance it? If a paragraph is a detour (a comparison, an FAQ, an aside), compress to one clause or cut.
    Terms get defined WHERE THE SPINE NEEDS THEM ("AI は銀行口座を持てない → だから ウォレット")、never as a
    glossary dump introduced by a connective like 「次に、お金の側の言葉です」 (unnatural JP, and a tell that the
    writer has no spine).

59. **★ MAINTAIN A REVIEW-STATUS LEDGER FILE, and never touch a REVIEWED block.★**
    `docs/articles/<slug>-REVIEW-STATUS.md`: one row per block = 見出し + 状態(REVIEWED / レビュー中 / 未).
    EDITOR PROTOCOL only works if "approved" is written down; chat memory is not a ledger. Update the row the
    moment the editor approves a block, and re-print REVIEWED blocks byte-for-byte when showing context.

## GENERAL LAWS (2026-07-13) — these hold for EVERY article this skill ever writes. Not about any one piece.

60. **★ FIGURES ARE PART OF WRITING, NOT PART OF PUBLISHING. Author them in the markdown, block by block.★**
    A block that describes a STRUCTURE (a stack, a layer cake, a taxonomy), a FLOW (a request/response, a state
    machine, a lifecycle) or a COMPARISON of positions carries a ```mermaid figure IN THE MARKDOWN, written at
    the same moment as the prose that needs it. Never leave "add a diagram later at publish time" — the publish
    pipeline renders figures, it does not invent them. Sizing law stays rule 45: a plain `flowchart TD` chain of
    ≤6–7 short-labelled nodes; more than 7 → SPLIT into two figures (or move detail to prose); never `subgraph`
    or multi-column (auto-layout blows the width up and the text goes microscopic).
    Per-block writer checklist, run BEFORE showing the block to anyone:
      (a) can I say this block's spine in one sentence?  (b) does every paragraph advance that spine?
      (c) does it describe a structure/flow → then where is its ```mermaid?  (d) is every term defined at the
      point the spine needs it?  (e) does every named thing have an owner (rule 62)?

61. **★ SILENCE = APPROVAL = FINAL. Touching an unflagged line is a net LOSS even when your version is better.★**
    Any edit the editor did not ask for forces them to re-read and re-approve the whole piece, which costs more
    than the improvement is worth and destroys trust in the diff. So: apply the flagged fix, leave everything
    else byte-for-byte, and SHOW the diff (not a rewritten wall). If you believe an unflagged part is weak,
    say so in chat as a proposal and wait — do not edit it. This is the highest-frequency failure mode of an
    AI writer, and it is the fastest way to lose an editor.

62. **★ NO OWNERLESS LANDSCAPE. Every component/standard/product named in a landscape block gets its builder,
    inline, at the place it appears.★** A list of parts with no builders is a glossary, and a glossary does not
    tell the reader who is actually building this field. Corollary: do NOT front-load a parenthetical roster
    (「世界のプレイヤー（A社、B社、C社など）が…」). The paren is dead weight, it duplicates what the body is about
    to say, and it robs each name of the one place it is meaningful — next to the thing that company built.
    Write 「④決済はCoinbaseが出したx402」, not 「（Coinbase、Google…など）が整理している」.

63. **★ WHEN A CLAIM SAYS "IT WORKS" OR "IT IS IMMATURE", PROVE IT WITH THE HONEST NUMBER.★** "已经成熟/未成熟"
    is an adjective; the reader wants the receipt. Ship the pair: what exists (name + builder) AND the number that
    exposes its real size (settled value, not transaction count; live users, not registrations). Where the honest
    number is embarrassing, that IS the story (a system with 14.5M transactions and $89k of lifetime settled value
    is a real finding, not a footnote). Where NO name exists for a component, say plainly that no serious
    contender exists — an empty slot named honestly is worth more than a hedge.

64. **★ NEVER ELIDE WHEN SHOWING WORK TO THE EDITOR. Print the block in full, always.★**
    Symptom: showed a revised block with 「（以下、既存のまま）」/「(unchanged)」 placeholders. Wrong instinct:
    "saving the editor's time by not repeating text they already read." Correct move: print every line of the
    block verbatim. GENERAL LAW: **an elision destroys the only thing a review produces — the editor's ability
    to see exactly what the text now is.** A summary of a diff is not a diff. This holds for any artifact under
    review (prose, code, spec, config): show the whole unit, not a description of it.

65. **★ THE ARTICLE NEVER TALKS ABOUT ITSELF. Deliver the information; do not perform the delivery.★**
    Symptom: 「誰が作っているのかも、名前で言えます」 before a list of builders; also 「この記事でわかること」,
    「ここで一つ、よくある疑問に先に答えます」, 「言葉を、必要な分だけ説明します」, 「まとめると」.
    Wrong instinct: signposting reads as helpful structure. Reality: it is the writer demonstrating to the
    editor that the writer did the work. The reader does not care that names CAN be given; they want the names.
    GENERAL LAW: **cut every sentence whose subject is the article, the writer, or the act of explaining.**
    Start with the content itself. (This is the JP-slop 「主体の不在／メタ枠」 tell — always run stop-ai-slop-jp.)
    **FORM SCOPE:** field-note, opinion, and case-study may refer to the writer only when a verified personal stake
    is necessary evidence. Even then, remove delivery-performance phrases and give each reference a reader payoff.

66. **★ RESEARCH IS PERSISTED. READ YOUR OWN NOTES BEFORE YOU SEARCH AGAIN.★**
    Symptom: spawned a fresh web-research agent for facts that were already written up in the project's research
    MD (and burned the editor's tokens + a paid scraping quota). Wrong instinct: "a fresh search is more
    rigorous." Reality: the research was already done, verified, and persisted precisely so it would never be
    re-run. GENERAL LAW: **before any external search, read the local evidence store first** (this skill's
    research MDs / state dir / the spec that cites them), and search externally ONLY for the specific facts that
    are demonstrably missing from it — and name which fact is missing when you do. Corollary: whatever you learn
    from a new search goes straight back into that MD in the same turn, or the next run repeats the mistake.

## MORE LESSONS — 2026-07-15 (first real image-bearing note draft; four publish-path bugs)

67. **★ EXIT CODE 0 MEANS THE PROCESS SURVIVED. IT DOES NOT MEAN THE WORK GOT DONE.★**
    Symptom: `note-stage2-publish.py` caught every per-image upload failure, replaced the marker with an empty
    string, and let the process exit 0 — so an article that quietly lost a picture was recorded as a success.
    Wrong instinct: per-item try/except is defensive programming; a failure that does not crash the run is a
    failure that was handled. Reality: swallowing the error deleted the evidence. The marker vanished, so even a
    human reviewer could not see that anything was missing. GENERAL LAW: **a per-item try/except is only honest
    if it aggregates the failures and reflects them in the exit status.** Count what you attempted and what
    succeeded, emit `embedded=N/M`, and exit non-zero when N<M — while still saving the partial artifact, so the
    draft survives for review. Corollary: when a step degrades instead of failing, the degradation must reach a
    machine-readable field (meta.json), not just a stderr WARN nobody greps.

68. **★ LOCAL TESTS VERIFY THE SHAPE OF THE CALL. ONLY THE REAL API VERIFIES THE CONTRACT.★**
    Symptom: py_compile + the whole regression suite + fixture-based negative tests were green; the first real
    shot at note.com died instantly — `update_article` was being handed a numeric id and requires the key form.
    Wrong instinct: "all tests pass" felt like evidence the path worked. Reality: fixtures encode what we
    BELIEVE the API wants, so they can only re-confirm our own misunderstanding. GENERAL LAW: **a mocked test
    can never discover that your mock is wrong. Publish paths must be proven by one real end-to-end shot before
    they are called done** — and the moment the real API teaches you its contract, encode it as a negative test
    (mutate the fixture to reproduce the real error, confirm the suite FAILS without the fix) so the suite stops
    being a mirror of your assumptions.

69. **★ THE DOM IS THE TRUTH. A SCREENSHOT IS A RENDERING OF IT — AND IT LIES.★**
    Symptom: the full-page screenshot of an 18,539px draft showed the eyecatch, title and body a second time;
    it read exactly like a duplicated article, and the next move would have been to "fix" a healthy draft.
    Counting the DOM settled it in one shot: one h1, one 「この記事でわかること」, 8,692 chars matching the
    editor's own counter — the repeat was stitching plus a sticky toolbar. Wrong instinct: seeing is believing.
    Reality: the screenshot is an artifact of how a long page was captured, not of what the page contains.
    GENERAL LAW: **assert on the DOM (counts, `naturalWidth>0`, marker scans); keep the screenshot only as the
    thing a human looks at last.** An `<img>` tag proves nothing — a 404 still has a tag; only decoded pixels
    count. Corollary: this cuts both ways — the eye that invents a bug will also miss a real one, so the gate
    must be the query, never the glance.

70. **★ DELETE THE DANGEROUS CODE PATH; DO NOT GATE IT.★**
    Symptom: the existing eyecatch script set the thumbnail AND published, with `assert_publish_allowed()`
    standing between them; the review loop only ever needs the thumbnail. Wrong instinct: reuse it and rely on
    the guard. Reality: a guard is a runtime condition — one env var, one stray sentinel file, one refactor and
    it fires. GENERAL LAW: **when a script does not need a capability, remove the capability rather than guard
    it** (this is why publish-note.sh dropped its `publish_article` import instead of wrapping it: code that
    does not exist cannot misfire). Reserve guards for paths that genuinely must stay reachable.

71. **★ FIT THE DIAGRAM INTO A BOX. SIZING BY WIDTH ALONE MAKES TALL DIAGRAMS HUGE.★**
    Symptom: the editor kept saying 「図がデカすぎる」 across many articles and it never stuck, because each
    time it was "fixed" by nudging one width number. Measured reality: kroki renders a `flowchart TD` PORTRAIT
    (276x606 natural), and the embed forced `width=480` → the browser displayed it at **480x1052** — taller
    than a laptop viewport, and blurry, since 480 is wider than the 276 pixels that exist.
    Wrong instinct: "too big" means "reduce the width number." Reality: width was never the binding constraint;
    HEIGHT is what runs off the screen, and for a vertical flowchart shrinking width does not help until it is
    absurdly narrow. Worse, a width larger than the source upscales it — bigger AND softer at once.
    GENERAL LAW: **fit every generated image inside BOTH a max-width and a max-height, and clamp the scale at
    1.0 so it is never enlarged past its own pixels** — `scale = min(max_w/w, max_h/h, 1.0)`. The reader's
    screen, not the column width, is the constraint a diagram has to satisfy.
    Corollary (the real lesson): **a complaint that recurs is a defect in this file, not in that article.**
    When the same note comes back a second time, stop fixing the instance and write the law here.
    Verify the way the reader experiences it: read back `getBoundingClientRect()` from the live DOM, not the
    number you passed in — the attribute you set and the pixels that render are different facts.

## MORE LESSONS — 2026-07-16 (first article actually SOLD on note: ¥1,000, key nbcb93e6fc711)

72. **★ WHICH MONETIZATION SHAPE TO USE IS A MEASUREMENT, NOT A PREFERENCE.★**
    Measured in our own niche (note search API q=Claude Code, then creators API per author):
    punimaru_dev 177 followers / 3 posts / NO membership / **¥3,480 single article, 928 likes**;
    kon_ai 188/4/no/¥3,980; shiro_life0 2,334/11/no/¥100,000. Membership only appears at 6,409+
    followers, and its floor is 65 posts (ebithai). Wrong instinct: "we have no audience, so we cannot
    charge — publish free and build a following first." Reality: **followers are what MEMBERSHIP needs.
    A single paid article needs PROOF.** 177 followers sold ¥3,480. GENERAL LAW: **single = proof play,
    membership = scale play.** Below the measured post floor, sell singles; do not open an empty
    membership (you would be charging monthly for a room with nothing in it).
    Pick the price from the same data, by matching what the article PROMISES: ¥3,480 works for
    「2ヶ月目に月10万円」 (the reader can earn the same); a pure explainer sells at ¥1,000 (like=165
    「「AIを雇う」という設計」). Never carry a number over from last month — re-measure (see rule 73).

73. **★ NUMBERS BAKED INTO THIS FILE ROT. BAKE THE MEASUREMENT, NOT THE NUMBER.★**
    Two baked numbers were both wrong within a month: "Price = 500円/月" (nobody in the niche charges
    ¥500; the mode is ¥980) and "copy ChatGPT研究所 (¥3,980/月, 240記事読み放題)" (that creator has
    CLOSED its note membership and moved to its own domain — `GET /api/v2/creators/chatgpt_lab` returns
    hasCircle=false). Nobody noticed for a month. Wrong instinct: a decision recorded in the skill is
    settled. Reality: it is a snapshot of a market that keeps moving. GENERAL LAW: **before acting on a
    recorded market decision, re-run the one call that would falsify it.** The four that settle it:
      note.com/api/v3/searches?context=note&q=<niche>&size=50&sort=popular → price / like_count / who
      note.com/api/v2/creators/<urlname>                → hasCircle, followerCount, noteCount
      note.com/api/v2/hashtags/<tag>                    → count (skip the giants: #AI is 794k = drown)
      note.com/api/v3/notes/<key>                       → price / is_limited / can_read (OUR result)
    Fields are snake_case (`like_count`, not likeCount) — reading the wrong case returns 0 and looks
    like "nothing is popular". Prefer a check that returns a FACT over an article that describes one.

74. **★ AT PUBLISH TIME THE BROWSER FORM IS AUTHORITATIVE. IT OVERWRITES THE API.★**
    Symptom: `--tags` was passed, stage2's update_article set them, and the article still went out with
    `hashtag_notes=[]`. Cause: clicking 投稿する submits the publish form, whose hashtag field was empty,
    and the empty field won. Wrong instinct: the API is the "real" layer and the UI is a view of it.
    Reality: for note's publish path they are two writers of the same field, and the last writer wins.
    GENERAL LAW: **when a value can be set through two paths, find out which one runs last — that is the
    only one that matters.** Type the tags into the form in the same run that clicks publish.
    Related measured facts, all of which cost a run to learn:
    - 有料/price/paywall-line are TRANSIENT form state on a draft: set them, reload, and price reads
      back NONE with the paid radio unchecked. note only commits them on 投稿する. So there is no
      "configure now, have a human check later" — configure and publish must be one run.
      (On an already-PUBLISHED article the price does persist: reopening shows 1000.)
    - The commit button lives ONLY inside the 有料エリア設定 overlay: 投稿する on a draft, 更新する once
      public. There is no 更新する on the settings screen — looking for one there finds nothing, clicks
      nothing, and reports success while saving nothing.
    - eyecatch SURVIVES update_article (payload carries name/body/body_length/hashtags only), so the
      eyecatch step and body updates can run in any order.

75. **★ NEW WRITER ARTICLES USE THE EXECUTABLE ¥500 MONEY CONTRACT.★**
    First run `note-publish/note_monetization_policy.py desired-state`; it must return one-time purchase, JPY,
    price 500, paywall required. Then run
    `note-publish/publish-paid.py --key <draft> --price 500 [--after-chars 2500] [--tags "t1,t2"] [--arm]`.
    `--free` is outside the Writer money contract. An armed success prints `PAID_PUBLISHED verified=true` only
    after the note API reads back price=500.
    (verified 2026-07-16, key n0bba0d52f007). Without `--arm` it configures everything and prints
    PRICE_READBACK/PAYWALL_PLACED/FREE_ENDS_WITH/PAID_STARTS_WITH then stops — 投稿する is never
    clicked, and per rule 74 nothing persists (reload shows the paid radio unchecked, no price field),
    so a guard-stop run is always safe to repeat. `--arm` clicks publish and self-verifies against
    `GET /api/v3/notes/{key}`; a mismatch is FATAL non-zero even though the article did go out.
    Pick tags by measuring first: `scripts/_shared/tag-counts.py <candidates...>` returns real counts
    from `note.com/api/v2/hashtags/{tag}` (e.g. AIエージェント=43,821, Claude=99,592, 生成AI=416,162 —
    avoid giants like #AI at 794k, they drown a small article); choose up to 5 from the numbers, not vibes.
    If `extract-note-cookies.py` ever returns 0 cookies, see `note-publish/TROUBLESHOOTING.md` — the
    on-disk Cookies sqlite can lag the live browser's in-memory session by hours; read CDP directly.
