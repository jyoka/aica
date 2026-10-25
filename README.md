# aica

**求職者と話して、アルバイト・パートから正社員まで、その人に合う仕事探しを相談と提案で支える AI エージェント。**

- LINE 公式アカウントの AI アシスタント(バイトル)を手元で再現する検証環境(旧称 line-sim)から始まりました
- LINE 側(会話の頭脳)と dip 側(求人とユーザー理解)に分け、**エージェントは dip のツール(MCP)を通してだけ dip の事実を得る**という境界を守っています。
  Web サーバーは dip の関数を直接呼びます([ADR-0003](docs/adr/0003-agent-gets-facts-only-through-dip-tools.md))
- 確かめたいのは「dip が書ける Prompt とツールの中身だけで、狙った会話体験になるか」です
- 求人、店舗、ユーザー、FAQ はすべて架空のデータです。社内資料の数値は含めていません

旧リポジトリ `jyoka/aica_ra_a2a_poc`(main、コミット `cda42fd`)から、履歴なしで移植しました。旧リポジトリの評価結果、
作業ログ、アーカイブは持ってきていません。履歴書の読み取り(`resume_intake/`)は task 63、音声相談(`voice_resume/`)は task 62 で移しました。

## しくみ

```mermaid
flowchart LR
    seeker(["求職者<br/>ブラウザ"])
    lineu(["求職者<br/>LINE アプリ"])
    lineapi[("LINE プラットフォーム")]
    web["Web プロセス(line_runtime)<br/>Web サーバー + エージェント"]
    dip["dip の MCP サーバー(dip_mcp)<br/>dip のツール、dip の Prompt、playbook"]
    lk[("LiveKit Cloud")]
    worker["音声ワーカー(voice_resume)<br/>音声アドバイザー"]
    data[("data/<br/>dip の状態、会話")]
    llm[("OpenAI / Anthropic")]

    seeker -- "トーク、履歴書" --> web
    lineu -- "トーク" --> lineapi -- "webhook" --> web
    web -- "MCP" --> dip
    web --> llm
    seeker -- "音声" --> lk --> worker
    worker -- "MCP" --> dip
    worker -- "結果" --> web
    dip --> data
    web --> data
```

- エージェントは業務の知識を持ちません。LINE の共通ルールと、dip から受け取る dip の Prompt と playbook で話します
- dip のツールは LLM を使わず、事実だけを返します。求人カードは、dip が返した求人だけをコードで描きます
- 音声相談もトークと同じ雇用区分、playbook、プロフィールを使い、結果はトークに戻ります
- 求職者と話すチャネルは、Web のトーク画面(LINE 風の画面)と LINE 公式アカウントの 2 つです。LINE は設定したときだけ開きます(下の「LINE で試す」)

部品とデータの流れは [HLD](docs/architecture/hld.md) にあります。

## 動かし方

必要なもの: Python 3.12、OpenAI の API キー。

```bash
# リポジトリ直下で
python3.12 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
# リポジトリ直下の .env に OPENAI_API_KEY=... を書く(git には入らない)
./.venv/bin/python -m line_runtime.server   # トーク画面 http://127.0.0.1:8030/ 、開発者画面 http://127.0.0.1:8030/dev
./.venv/bin/python -m pytest -q             # テスト(実 API を呼ばない)
```

- 既定(`LINE_SIM_PROVIDER=openai`)では、`OPENAI_API_KEY` が無いと Web プロセスは起動の途中で止まります
- トーク画面を開くと、エージェントが挨拶のために実 API を呼び、費用がかかります(最後のやりとりから 30 分以内の開き直しは除く)。
  開発者画面は、シナリオを選ぶまで呼びません。話すとターンごとに呼びます
- `LINE_SIM_PROVIDER=anthropic` にすると、エージェントは Anthropic のモデルになります(`ANTHROPIC_API_KEY` が要る)。
  履歴書の読み取り、意味検索、音声相談は常に OpenAI です

### 音声相談も使う

```bash
# .env に足す: LIVEKIT_URL、LIVEKIT_API_KEY、LIVEKIT_API_SECRET、DEEPGRAM_API_KEY、CARTESIA_API_KEY、AICA_INTERNAL_KEY
./.venv/bin/python -m voice_resume.agent download-files   # 初回だけ(VAD と turn detector のモデル)
./.venv/bin/python -m line_runtime.server                 # 1 つ目のターミナル: Web プロセス
./.venv/bin/python -m voice_resume.agent dev              # 2 つ目のターミナル: 音声ワーカー
```

- 2 つのプロセスは、リポジトリ直下で同じ venv と同じ `.env` を使います
- `AICA_INTERNAL_KEY` は、2 つのプロセスで同じ値の長い乱数です(例: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`)
- 画面で入力欄の「+」→「音声で相談する」を押し、マイクを許可します。声が途切れるときはイヤホンを使います

### LINE で試す

LINE 公式アカウントからも、Web のトーク画面と同じ会話ができます。今はテキストの会話と、友だち追加の挨拶と、アカウント連携(下の「アカウント連携を試す」)だけです
(履歴書のファイルと音声相談は、LINE ではまだ使えません。エージェントがそれを勧めたときは、文で書くよう案内します)。
設定(`LINE_CHANNEL_SECRET` と `LINE_CHANNEL_ACCESS_TOKEN`)が無ければ `POST /line/webhook` は 404 で、ほかは何も変わりません。
設定があるときは、LINE のユーザーIDを作る鍵 `AICA_ID_KEY` も要ります(下の 3)。

人がする操作です。LINE のアカウントと、トンネルのツール(ここでは cloudflared)が要ります。

1. LINE 公式アカウントを作る。[LINE Official Account Manager](https://manager.line.biz/) でアカウントを作り、
   「設定」→「Messaging API」で Messaging API を使うようにする(プロバイダーを選ぶか作る)
2. 同じ画面の「応答設定」で、「Webhook」をオン、「応答メッセージ」と「あいさつメッセージ」をオフにする。
   オンのままだと、LINE の自動の返事とエージェントの返事が両方届きます
3. [LINE Developers コンソール](https://developers.line.biz/console/) で、そのプロバイダーのチャネルを開く
   - 「チャネル基本設定」のチャネルシークレットを `.env` の `LINE_CHANNEL_SECRET` に書く
   - 「Messaging API設定」でチャネルアクセストークン(長期)を発行し、`.env` の `LINE_CHANNEL_ACCESS_TOKEN` に書く
   - LINE のユーザーIDを作る aica 専用の鍵を作り、`.env` の `AICA_ID_KEY` に書く(32 バイト以上。無い、短いと Web サーバーは起動で止まります)

     ```bash
     python -c "import secrets; print(secrets.token_hex(32))"
     ```

     **鍵を変えると、LINE のユーザー全員の ID が変わり、それまでのデータ(会話、プロフィール、アカウント連携)とつながらなくなります**。
     一度決めたら変えず、なくさないように控えておきます([ADR-0008](docs/adr/0008-keyed-line-user-id.md))
4. 手元の Web サーバーを、トンネルで https に出す(cloudflared は `brew install cloudflared` で入れる)

   ```bash
   ./.venv/bin/python -m line_runtime.server                 # 1 つ目のターミナル
   cloudflared tunnel --url http://127.0.0.1:8030            # 2 つ目のターミナル。https://<ランダム>.trycloudflare.com が出る
   ```

5. 出た URL を `.env` の `AICA_PUBLIC_URL` に書き(例 `AICA_PUBLIC_URL=https://xxxx.trycloudflare.com`)、
   `.env` に `AICA_TRUST_PROXY=1` も書いて、Web サーバーを起動し直す。
   トンネル越しの接続元は全員 `127.0.0.1` なので、これが無いと全員が 1 つの IP として数えられます。
   1 人が Cookie を替えて匿名ユーザーIDを 20 個使うと、ほかの新しい Web の利用者はその日ずっと 429 になります(下の「設定」のターン数の上限)
6. LINE Developers の「Messaging API設定」で、Webhook URL を `https://xxxx.trycloudflare.com/line/webhook` にし、
   「Webhookの利用」をオンにして「検証」を押す(成功と出れば、署名とつながりが合っています)
7. 同じ画面の QR コードで友だち追加すると、挨拶が届きます。あとはトークに書けば、エージェントが返します

- cloudflared の一時のトンネル(`--url`)は、起動するたびに URL が変わります。そのたびに 5 と 6 をやり直します
- トンネルを開いている間は、URL を知っている人は誰でも Web のトーク画面と開発者画面も開けます(費用がかかります)。試し終わったら止めます
- 返信には LINE の返信トークを使います。間に合わなかったときだけ push で返し、push は LINE 公式アカウントの月のメッセージ数に数えられます
- 求人カードのボタンと LIFF 画面へのリンクは、LINE の中のブラウザで `AICA_PUBLIC_URL` の画面を開きます。LIFF 画面は Cookie で
  本人を決めるので、LINE のユーザーの画面にはなりません(今は LINE でこのリンクが出る流れはありません。[HLD の 4.8](docs/architecture/hld.md#48-line-のトークmessaging-api))
- ターン数の上限は LINE のユーザーごとと全体で数えます。IP ごとの上限は LINE にはかかりません
- `AICA_TRUST_PROXY=1` のとき、IP は `X-Forwarded-For` の最後の値です。Cloudflare は本人が送った値の最後に、Cloudflare へつないだ接続元を足します
  ([Cloudflare の HTTP ヘッダー](https://developers.cloudflare.com/fundamentals/reference/http-headers/))。
  本人が書いた値は見ないので、書き換えても IP の上限を迂回できません。トンネルを止めて手元だけで使うときは、`.env` から消してかまいません

### アカウント連携を試す

LINE のユーザーと dip の会員を、LINE の「アカウント連携」の流れで結べます。連携すると、dip のツールがその会員の行動の要約を読みます。
流れと守っていることは [HLD の 4.10](docs/architecture/hld.md#410-line-のアカウント連携) にあります。

- LINE で: トークに「アカウント連携」と送ると、dip のログインの画面へのボタンが届きます(`AICA_PUBLIC_URL` が要ります)。
  ログインすると LINE の画面に移り、トークに「つながりました」と届きます。「連携解除」と送ると外れます
- Web で: 開発者画面(`/dev`)の左の「4 アカウント連携(偽物の LINE)」で「連携を始める」を押すと、本番のトーク画面(`/`)の
  匿名ユーザーで同じ流れを試せます。LINE の役は偽物の LINE がします。実 LINE は呼びません

dip の会員(シミュレーション。架空)のログインです。会員の行動は fixtures のシナリオのユーザーと同じです。

| ログインID | パスワード | 会員 | 行動 |
|---|---|---|---|
| `yoru@dip.test` | `demo-pass-1001` | D1001 | シーンA: 新宿の居酒屋に応募・夜希望 |
| `exit@dip.test` | `demo-pass-1002` | D1002 | シーンB: コンビニ夜勤の応募フォームで離脱 |
| `applied@dip.test` | `demo-pass-1004` | D1004 | シーンD: 居酒屋に応募・連絡待ち |
| `browsing@dip.test` | `demo-pass-1005` | D1005 | 比較検討層: 閲覧・保存あり |

- 続けて 5 回まちがえると、15 分ログインできません
- LINE と Web の匿名ユーザーは実際の時計なので、fixtures の会員の「2 時間前に離脱」などは、何日も前の出来事として読まれます。
  傾向と 30 日の数は要約のまま読めます
- シナリオのユーザーは最初から連携済みです。前から手元にある `data/` のシナリオユーザーは、シナリオを始め直すと連携済みに戻ります

## 画面

| 画面 | URL | 中身 |
|---|---|---|
| トーク画面 | `/` | 求職者が使うスマホ向けの 1 画面。トーク、保存した求人、プロフィール。履歴書のアップロードと音声相談は、入力欄の「+」か、エージェントが出すボタンから入る |
| 開発者画面 | `/dev` | 評価と説明のため。左でシナリオを選んで dip から見た状態を見て、中央で話し、右で構成図とこのターンの処理の流れを見る |
| LIFF 画面 | `/liff/*` | dip の画面のモック。応募フォーム、読み取り結果、下書き |
| レジュメ・アーケード | `/liff/arcade` | 遊びながら職務経歴書を作るゲームのハブ。サーバーを起動して `http://127.0.0.1:8030/liff/arcade` を開く。今は AI を使わない簡易分析だけ。ハブに出ない 6 本は開発者画面の左からリンクする |
| dip のログイン | `/dip/login` | dip の画面のモック。アカウント連携のログイン(上の「アカウント連携を試す」) |
| LINE の webhook | `POST /line/webhook` | 画面ではなく、LINE プラットフォームが呼ぶ口。設定したときだけ開く(上の「LINE で試す」) |

- ブラウザごとに匿名ユーザーIDを Cookie で発行します。aica のログインはありません(URL を知っている知人だけが使う前提)。
  dip のログインは、アカウント連携のときだけ使います
- 1 日あたりのターン数に上限があります(下の「設定」)
- シナリオは 15 件で、開発者画面と評価で同じ一覧(`evals/scenarios.json`)です

画面の細かい動きは [LLD の 4.10](docs/architecture/lld.md#410-画面) にあります。

## 主なコマンド

`python` は `./.venv/bin/python` のことです。「実 API」の列が「使う」のコマンドは費用がかかります。

| コマンド | すること | 実 API |
|---|---|---|
| `python -m line_runtime.server` | Web プロセスを起動する(`127.0.0.1:8030`) | 挨拶と会話で使う |
| `python -m voice_resume.agent dev` | 音声ワーカーを起動する | 使う |
| `python -m dip_mcp.server --http 8020` | dip の MCP サーバーを別プロセスで起動する。Web プロセスには `DIP_MCP_URL=http://127.0.0.1:8020/mcp` を渡す | - |
| `python -m pytest -q` | テスト | 使わない |
| `node --test tests/js/arcade-ja.test.js` | レジュメ・アーケードの日本語の判定のテスト(pytest からも回る) | 使わない |
| `python -m evals.run [--only B-exit] [--judge] [--repeat 2]` | 評価。模擬ユーザーとシナリオを会話させて採点する。予算台帳の累計が $4 で止まる | 使う |
| `python -m evals.run --rejudge 結果.json` | 保存した会話を、ジャッジだけで判定し直す | 使う |
| `python3 evalkit/aggregate.py diff 前.json 後.json` | 評価の結果の前後を比べる | 使わない |
| `python -m evals.budget` | 予算台帳の累計と、実行ごとの内訳 | 使わない |
| `python -m evals.search_eval eval` | 意味検索の再現率(保存済みのベクトルで測る) | 使わない |
| `python -m line_runtime.ledger [--date 2026-09-27] [--by-user-id] [--json]` | 費用台帳を人と日ごとに表で集計する(合計の行つき)。`--by-user-id` はシナリオユーザーを匿名ユーザーIDにまとめない | 使わない |
| `python -m resume_intake.cli <ファイル>` | 履歴書を 1 つ読み取る | 使う |
| `python -m scripts.gen_jobs` | 架空のアルバイト・パートの求人(`fixtures/jobs.json`)を作り直す | 使う |
| `python -m scripts.gen_job_vectors` | 求人のベクトルを作り直す。求人を変えたら回す | 使う |
| `python -m scripts.gen_dip_events [--members 1000] [--months 6] [--seed 0] [--replace]` | dip の行動の元データ(`data/dip_events.db`)を作る。同じ種なら同じデータ。`--replace` で作り直したら、要約のバッチも回す | 使わない |
| `python -m dip_mcp.summarize [--as-of 2026-09-23T12:00:00+09:00]` | 要約のバッチ。行動の元データから、会員 1 人 1 行の状況の要約(`data/dip_summaries.db`)を全員分作り直す。dip のツールが読むのはこの要約で、会話の中で書いたこと(保存、応募など)は次のバッチまで本人の分だけ重ねて読む。要約が無ければ、最初にツールを使うときにシナリオのユーザーの分を作る。`data/` で回すと、開発者画面のシナリオユーザーが書いた分も要約に入る(シナリオの初期に戻すには `data/dip_events.db` と `data/dip_summaries.db` を消す) | 使わない |

評価の判定と回し方は [LLD の 4.8](docs/architecture/lld.md#48-評価の-1-シナリオevalsrunrun_scenario)、テストの仕組みは
[LLD の 10 章](docs/architecture/lld.md#10-テストと評価の仕組み)にあります。

## 設定

API キーなどの秘密は、リポジトリ直下の `.env` に書きます(すでにある環境変数は上書きしません)。

**`.env` で効くのは、下の表で「`.env` 可」としたものだけです。** ほかは起動のときに読むので、コマンドの前に環境変数として渡します
(例: `SIM_NOW=2026-10-01T09:00:00+09:00 ./.venv/bin/python -m line_runtime.server`)。理由は
[review-2026-10.md の R21](docs/architecture/review-2026-10.md#r21-設定の一部が-env-では効かない)にあります。

| 環境変数 | 既定 | 用途 | `.env` |
|---|---|---|---|
| `OPENAI_API_KEY` | なし(必須) | エージェント、履歴書の読み取り、意味検索、音声アドバイザー、下書き | 可 |
| `ANTHROPIC_API_KEY` | なし | `LINE_SIM_PROVIDER=anthropic` のとき | 可 |
| `LINE_SIM_PROVIDER` | `openai` | LLM のプロバイダ(`openai`、`anthropic`)。エージェント、模擬ユーザー、ジャッジのモデルが変わる | 不可 |
| `LINE_SIM_MODEL` | `gpt-6-sol`(anthropic なら `claude-opus-5`) | エージェントのモデル | 不可 |
| `LINE_SIM_EFFORT` | `low` | エージェントの思考の深さ | 不可 |
| `LINE_SIM_PORT` | `8030` | Web サーバーのポート | 不可 |
| `LINE_SIM_DATA_DIR` | `data/` | 会話、費用台帳、ターン数の置き場 | 不可 |
| `DIP_MCP_URL` | なし(stdio の子プロセス) | 別プロセスの dip の MCP サーバーの URL | 不可 |
| `DIP_STATE_PATH` | `data/state.json` | dip の状態。行動の元データと要約(`dip_events.db`、`dip_summaries.db`)、ログインと連携の表(`dip_accounts.db`)も同じディレクトリに置く | 不可 |
| `SIM_NOW` | `2026-09-23T12:00:00+09:00` | シナリオユーザーと評価の dip の時計(固定)。本番のユーザーは実際の時刻 | 不可 |
| `RESUME_INTAKE_MODEL` | `gpt-6-sol` | 履歴書の読み取りのモデル | 不可 |

ターン数の上限です。`0` 以下にすると、その上限はありません。既定値の理由は [LLD の 5.3](docs/architecture/lld.md#53-費用台帳とターン数) にあります。

| 環境変数 | 既定 | 上限 | `.env` |
|---|---|---|---|
| `LINE_SIM_DAILY_TURN_LIMIT` | 50 | 匿名ユーザーIDごとのターン数 | 可 |
| `AICA_DAILY_TURN_LIMIT_PER_IP` | 150 | 同じ IP からのターン数の合計 | 可 |
| `AICA_DAILY_NEW_IDS_PER_IP` | 20 | 同じ IP から 1 日に使える匿名ユーザーIDの数 | 可 |
| `AICA_DAILY_TURN_LIMIT_TOTAL` | 1000 | 全体のターン数の合計 | 可 |
| `AICA_DAILY_NEW_IDS_TOTAL` | 300 | 全体で 1 日に使える匿名ユーザーIDの数 | 可 |
| `AICA_TRUST_PROXY` | なし | `1` のときだけ、IP を `X-Forwarded-For` の最後の値から取る。プロキシの後ろに置くとき(Render、cloudflared のトンネル)に使う。無いと全員がプロキシの IP として数えられる | 可 |

LINE 公式アカウントで使うものです。LINE で試さないなら要りません。

| 環境変数 | 既定 | 用途 | `.env` |
|---|---|---|---|
| `LINE_CHANNEL_SECRET` | なし | チャネルシークレット。webhook の署名(`X-Line-Signature`)を確かめる。`LINE_CHANNEL_ACCESS_TOKEN` と両方あるときだけ `POST /line/webhook` を開く(無ければ 404) | 可 |
| `LINE_CHANNEL_ACCESS_TOKEN` | なし | チャネルアクセストークン(長期)。入力中の表示、返信、push、アカウント連携の linkToken の発行に使う | 可 |
| `AICA_ID_KEY` | なし(LINE の 2 つがあるときは必須) | LINE のユーザーID(`l-…`)を作る HMAC-SHA256 の鍵。32 バイト以上(例 `python -c "import secrets; print(secrets.token_hex(32))"`)。LINE の 2 つがあるのに無い、短いときは起動で止まる。変えると全員の ID が変わり、それまでのデータとつながらない([ADR-0008](docs/adr/0008-keyed-line-user-id.md)) | 可 |
| `AICA_PUBLIC_URL` | なし | 手元の Web サーバーの公開 URL(トンネルの https の URL)。LINE で出す LIFF 画面と dip のログインの画面へのリンクの前に付ける。無ければリンクを出さず、アカウント連携も始めない | 可 |

音声相談で使うものです。トークだけを試すなら要りません。

| 環境変数 | 既定 | 用途 | `.env` |
|---|---|---|---|
| `LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET` | なし(必須) | LiveKit Cloud(URL は `wss://<project>.livekit.cloud`)。Web プロセスが部屋の接続トークンを発行し、音声ワーカーが部屋に入る | 可 |
| `DEEPGRAM_API_KEY` | なし(必須) | 文字起こし | 可 |
| `CARTESIA_API_KEY` | なし(必須) | 読み上げ | 可 |
| `AICA_INTERNAL_KEY` | なし(必須) | 内部の鍵。音声ワーカーが `POST /api/voice/result` に付け、Web サーバーが照合する。無いと結果は 401 で届かない | 可 |
| `VOICE_RESUME_TALK_URL` | `http://127.0.0.1:8030` | 音声ワーカーが結果を返す Web サーバーの URL | 不可 |
| `VOICE_RESUME_OUT_DIR` | `data/voice-resume` | 下書き、文字起こし、求人の提案の置き場 | 不可 |
| `VOICE_RESUME_MAX_SESSION_MINUTES` | `10` | 1 回の音声相談の長さの上限(分)。時間が来たら、下書きを保存していなければ締めの返答を作り、部屋を閉じる | 不可 |
| `VOICE_RESUME_MAX_WEB_SEARCHES` | `5` | 1 回の音声相談で web を調べる回数の上限(調べもの、平均時給、会社の 3 つの合計) | 不可 |
| `VOICE_RESUME_*`(モデル、ボイス、ターンの判定) | [LLD の 7.1](docs/architecture/lld.md#71-音声の部品の設定) | STT、LLM、読み上げの設定 | 不可 |

## 文書

| 知りたいこと | 文書 |
|---|---|
| 全体の図、部品、データの流れ、信頼の境界、用語 | [docs/architecture/hld.md](docs/architecture/hld.md)(HLD) |
| モジュール、HTTP の口、dip のツール、主な流れ、データの形、失敗の扱い、テスト | [docs/architecture/lld.md](docs/architecture/lld.md)(LLD) |
| 設計の問題と直し方の案 | [docs/architecture/review-2026-10.md](docs/architecture/review-2026-10.md) |
| 業務の語の定義 | [CONTEXT.md](CONTEXT.md) |
| シナリオごとの会話設計と経緯 | [docs/design/line-sim-scenarios.md](docs/design/line-sim-scenarios.md) |
| aica v2(正社員まで広げる)の要件と予算 | [docs/prd/aica-v2-fulltime.md](docs/prd/aica-v2-fulltime.md)(PRD) |
| レジュメ・アーケード(遊びながら職務経歴書を作るゲーム)の要件 | [docs/prd/resume-arcade.md](docs/prd/resume-arcade.md)(PRD) |
| 決めたことの記録 | [ADR-0001](docs/adr/0001-track-and-playbook-as-dip-tools.md) 雇用区分と playbook を dip のツールにする、[ADR-0002](docs/adr/0002-one-job-model-with-employment-type.md) 正社員の求人を同じ形で扱う、[ADR-0005](docs/adr/0005-separate-event-log-and-member-summary.md) 行動の元データと状況の要約を分ける、[ADR-0006](docs/adr/0006-tools-read-summary-and-overlay-own-writes.md) dip のツールは要約だけを読み、会話の中で書いたことは本人の分だけ重ねる、[ADR-0007](docs/adr/0007-line-account-link-table.md) LINE のアカウント連携で結び、連携の表から dip_id を引く、[ADR-0008](docs/adr/0008-keyed-line-user-id.md) LINE のユーザーIDは aica 専用の鍵つきのハッシュで作る |
| イベントの受け付けと Resume Draft の契約を 1 か所にする設計メモ | [docs/design/consolidate-resume-draft-and-event-turn.md](docs/design/consolidate-resume-draft-and-event-turn.md) |
| 応募の先の設計の調査 | [docs/research/apply-flow.md](docs/research/apply-flow.md) |
| 評価の結果の集計と比較 | [evalkit/README.md](evalkit/README.md) |
