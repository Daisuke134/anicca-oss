---
name: anicca-life-manager
description: |
  Push-type AI 電話 エージェント。Google Calendar + 位置情報 + Bland.ai/Twilio + AgentMail で、user (Dais / OSS buyer) の人生を 全管理。出発時刻に電話 (RELENTLESS until 動く)、遅刻時に 謝罪 mail 自動 送信、wake/sleep/meditation/run/work/LT/comedy 全 event に 個別 buffer (15分前到着 + 空港 60-180min) を 適用。Conway-Research/automaton の Buddhist edition。
metadata:
  tags: [voice, calendar, reminder, twilio, bland-ai, gemini-live, hard-rule-19]
  type: life-manager
  requires:
    bins: [python3, gog, curl, jq]
    env: [TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, GEMINI_API_KEY, GOOGLE_API_KEY, GOG_ACCOUNT, GOG_KEYRING_PASSWORD, OWNTRACKS_USER, OWNTRACKS_PASS]
    services: [loco (anicca-alarm/loco/server.js)]
  spec: ~/.local/state/life-manager/docs/ANICCA_LIFE_MANAGER_SPEC.md
---

# anicca-life-manager

User の **人生 全管理** を Anicca が引き受ける skill。User は gcal も Gmail も見なくていい。Anicca が 常駐 harness で 5min 毎に gcal を読み、位置を把握し、出発時刻に call し、移動を guide し、遅刻が確定したら stakeholder に 謝罪 mail を自動送る。

Source of truth: `~/.local/state/life-manager/docs/ANICCA_LIFE_MANAGER_SPEC.md` (v0.8, 38 sections, 2900+ 行)

## Architecture

```
[Google Calendar] ← Single Source of Truth
      ↓ gog cal events (poll every 5min)
[gcal_departures.py] — travel-time aware + event_type classifier (default 15min, airport 60-180, sleep 10, wake 0, lt 15, comedy 25)
      ↓
[lateness_check.py] — decision engine:
   ・depart_by ≤ now+5min & home → call_leave
   ・stale loc + last=home → call (no silent skip; Power of Free 5/30 教訓)
   ・event past & not @venue → late_flow
      ↓
[Bland.ai or Twilio + Gemini Live] — relentless call until vel>2 OR 移動>300m
[renraku.py] — 遅刻 mail (event名/名前なし、申し訳ございません必須) + Firecrawl fallback for stakeholder lookup
```

## Inputs

- `~/.local/state/life-manager/identity/profile.json`
  - `alarm.wakeTime`, `alarm.eventStyles[type].buffer`, `alarm.defaultArrivalBufferMinutes`
  - `goals.northStar`, `goals.ideal_state[]`, `goals.anti_goals[]`
  - `lateness.blocklistApply`, `lateness.blocklistRenraku` (Power of Free 分離)
  - `lateness.stakeholders[]`
  - `location.homeLat / homeLon`
- `~/.local/state/life-manager/.env` — Twilio / Gemini / Google Maps / OwnTracks keys (gitignored)

## Cron (Anicca 自走)

| name | schedule | what |
|---|---|---|
| `calendar-event-call` | `*/5 6-23 * * *` | 5min polling, depart_by call + late_flow + stale-loc handle |
| (廃止予定) `dais-lateness-heartbeat` | `8,23,38,53 6-23 * * *` | 15min backup (今は redundant) |

廃止済: `dais-phone-wake-call-daily`, `dais-audio-wake-up-daily`, `dais-wake-up-daily`, `dais-morning-leave-check`

## Run

```bash
bash $LIFE_MANAGER_REPO/skills/anicca-life-manager/scripts/run.sh
# stdout last line: SUMMARY_JSON: {...}
```

## Failure modes

- Twilio/Bland.ai dialout fail → 30s 後 再試行 max 3 回 → Slack DM
- Gemini Live drop → bridge restart → 60s 後 再 call
- OwnTracks 完全沈黙 24h+ → call to ask "iPhone 大丈夫?"
- Google Directions quota → fallback haversine + 1.5x
- gcal-policy.sh 経由しない event 挿入 → HARD RULE #19 違反

## HARD RULE #14 verify

末尾 mandatory:
```bash
grep -q '"action":' $LIFE_MANAGER_REPO/skills/anicca-life-manager/state/run.log
```
`verify-public-state.sh` は URL 向けなので、この skill では local `run.log` を直接確認する。

## Capafy Disclosure（reject R1/R2/R3 解消・Download/BYOK・全データ端末内）

| # | 開示項目 | 内容 |
|---|---|---|
| ① アクセスデータ | GPS座標/velocity(OwnTracks)・calendarイベント(Google Calendar)・電話番号/home address/stakeholder連絡先(profile.json)。**全てユーザー端末内ローカル保存・外部送信なし**(Download/BYOK) |
| ② 使用外部サービス | Twilio(発信) / Bland.ai(発信代替) / Gemini Live(音声) / Google Directions(移動時間) / Google Calendar(読取) / OwnTracks(位置) / AgentMail or Gmail(謝罪mail) / Firecrawl(stakeholder lookup fallback) |
| ③ 必要認証情報(BYOK・ユーザー自前) | `TWILIO_ACCOUNT_SID` `TWILIO_AUTH_TOKEN` `TWILIO_PHONE_NUMBER` `GEMINI_API_KEY` `GOOGLE_API_KEY`(Directions) `OWNTRACKS_USER` `OWNTRACKS_PASS` `GOG_ACCOUNT` `GOG_KEYRING_PASSWORD`(Gmail)。Bland/AgentMailを使う場合はその鍵。**全てユーザーが自分の`~/.local/state/life-manager/.env`に投入** |
| ④ call/mail 発火条件 | call: `depart_by ≤ now+5min` かつ 自宅判定時 / 謝罪mail: イベント時刻超過かつ未到着時。それ以外は発火しない |
| ⑤ max retry / rate limit | **call: 最大3回(`LATE_RELENTLESS_MAX`既定3)・間隔60-120s**(コードで強制 `RELENTLESS_MAX_DEFAULT=3`) / mail: 1イベント1通 |
| ⑥ pause / stop 方法 | `profile.json` の `lifeManager.enabled:false` で**全停止**(コードで強制 `life_manager_enabled()`)。quiet-hours中も routine 抑制 |
| ⑦ 第三者連絡前の確認 | 謝罪mail自動送信は**既定OFF**。`lateness.autoSendMail:true` の明示opt-in時のみ自動送信、未設定なら下書きをSlack通知して送信せず確認待ち(コードで強制 `auto_send_allowed()`) |

## Related

- `$LIFE_MANAGER_REPO/skills/anicca-booking/` — sister skill: empty gcal slot detection + ideal_state apply
- `$LIFE_MANAGER_REPO/skills/_shared/lib/gcal-policy.sh` — HARD RULE #19 helper (全 event 挿入時 必須経由)
- `anicca-alarm` repo (OSS): https://github.com/Daisuke134/anicca-alarm
- Conway-Research/automaton (理論的 spine): https://github.com/Conway-Research/automaton
