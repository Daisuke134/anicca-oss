# Agent Economy

Agent EconomyはLife Managerで最初のfinancially independent loopです。1つのLife Manager instanceが、
独立したcitizen identityとBase walletを1つ作ります。citizenは外部で検証済みの収益を得て、compute・hosting
費用を記録し、自分が稼いだ残高から支払えます。ユーザーのwalletを借りず、owner入金、自己送金、pending award、
modelの自己申告を収益として数えません。

実際の利益発生はrepository installationの条件ではありません。新しいcitizenは検証済みfree compute routeから
始まります。公式の外部receiptがそのcitizen自身の収益を証明し、reserveとsession capを満たす時だけpaid computeを
許可します。証拠がなければfree computeへfail closedします。

## 1つの実装、2つのruntime

```text
repository-owned Agent Economy contracts
├── identity + wallet
├── earning / cost adapter
├── FinancialRecord + provider receipt
├── treasury / compute-routing policy
├── Telegram transition / daily delivery
└── bounded wake + next-wake scheduling
    ├── Local: private filesystem state + launchd/systemd adapter
    └── Cloud: tenant Postgres/private signer + Railway worker adapter
```

LocalとCloudは同じwake・economic contractを実行します。違うのはsupervisor、private-state store、signer adapter
だけです。OpenClaw、Hermes、Franklin、別checkout、ユーザー固有home directoryのコードをimportしません。
Cloud runtimeはユーザーのPCがonlineでなくても動きます。

## Local setup

```bash
git clone https://github.com/Daisuke134/life-manager.git
cd life-manager
./install.sh
./bin/lm-loop status agent-economy-loop
```

`./install.sh`は1つのcitizen identityとwalletを作成・保持し、daemon install有効時にはrepo-owned compute proxyと
Agent Economy ownerをinstallして開始します。daemonなしのinstallation確認には
`LIFE_MANAGER_INSTALL_DAEMON=0 ./install.sh`を使います。private identity、signer、state、receipt、logはGit外の
`LIFE_MANAGER_HOME`配下に置きます。

Agent Economy contractはowner walletやChatGPT subscriptionを要求しません。任意のearning providerはcredentialを
必要とする場合があります。未設定providerは`setup_required`となり、wallet-native laneを妨げません。

## Cloud setup

Telegramで最初に必要な操作は`/start`だけです。tenantを作成・再開し、provisioningが暗号化されたcitizen walletと
idempotentなinitial Agent Economy jobを1つ作ります。Railway workerが同じbounded wakeを実行し、terminal receiptを
記録して次のwakeをtransaction内で予約します。`/economy`は任意のstatus・emergency controlです。通常運用にcommandや
local deviceは不要です。

## Telegram experience

検証済みtransitionは即時、financial snapshotは重複なしで毎日報告します。変化のないwakeはsilentです。

```text
Agent Economy
状態: free computeで稼働中
Wallet: 0x12…89ab (Base)
検証済み外部収益: USDC 0.00
Compute費用: USDC 0.00
Hosting費用: USDC 0.00
Reserve/runway: earned fundsではまだ未充足
次のaction: wallet-native earning laneを継続
```

即時報告はverified revenue、refund/chargeback、compute・hosting支払い、reserve breach、funding mode変更、継続不能を
対象にします。Telegram deliveryはdurableにclaimし、provider message IDを保存します。送信結果不明時はblind retryせず
quarantineします。

## 現在の事実

- Localのzero-command citizen bootstrapとsupervised wakeは実装済みです。
- Cloudの`/start` provisioning、encrypted signer boundary、bounded wake scheduling、`/economy` projectionは実装・本番実証済みです。
- Local/Cloudのfinancial transitionは同じreceipt-backed Telegram contractを使います。
- free bootstrapとreceipt/reserve/session-gated paid routingは実装し、実費を使わず検証済みです。
- live profit、実際のself-funded compute purchase、他loopへの資金化、citizen replicationはcleanup gateではなく、ここでは達成を主張しません。

詳細architecture、証拠、残るportability作業は
[`docs/superpowers/specs/2026-09-06-life-manager-one-repo-two-runtimes-design.md`](superpowers/specs/2026-09-06-life-manager-one-repo-two-runtimes-design.md)を参照してください。
