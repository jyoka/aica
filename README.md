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
- トーク画面を初めて開くと、まず同意の画面が出ます(「同意して始める」を押すまで実 API を呼びません)。
  押すと、エージェントが挨拶のために実 API を呼び、費用がかかります(最後のやりとりから 30 分以内の開き直しは除く)。
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
- `AICA_INTERNAL_KEY` は、2 つのプロセスで同じ値の長い乱数です(例: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`)。
  音声相談の設定(`LIVEKIT_*`)があるのに無い、短い(32 文字未満)と、Web プロセスも音声ワーカーも起動で止まります
- 画面で入力欄の「+」→「音声で相談する」を押し、マイクを許可します。声が途切れるときはイヤホンを使います
- 1 人が開ける音声相談は同時に 1 つまでです(Web プロセスを再起動した直後は、再起動の前から続いている相談を数えません)。
  音声ワーカーが同時に受ける相談は `VOICE_RESUME_MAX_JOBS`(既定 4)までです。
  ワーカーは、相談がなくても同じ数のプロセスを温めておきます(`dev` でも。1 つ約 200MB)

### Render で公開する

1 つのコンテナ(`Dockerfile`)を、Render の Web サービスに永続ディスクつきで置きます。Blueprint は `render.yaml` です。
手元で `./.venv/bin/python -m scripts.deploy_smoke` を流すと、コンテナを作ってヘルスチェック(`GET /healthz`)と、同意してから初回の挨拶の
1 往復、再起動の後も会話が残ることを確かめます(docker が要る。実 API を 1 回使う)。
Render のサービスを作る手順と、運営者がする操作は [docs/deploy/render.md](docs/deploy/render.md) にあります。

### LINE で試す

LINE 公式アカウントからも、Web のトーク画面と同じ会話ができます。トークで使えるのは、テキストの会話と、友だち追加の挨拶と、
アカウント連携(下の「アカウント連携を試す」)です。履歴書の読み込みと音声相談は、LIFF の設定(下の「LINE で LIFF 画面を開く」)があれば、トークのボタンから LIFF 画面で使えます。
結果は LINE のトークに届きます。レジュメ・アーケードも、LIFF の設定があれば dip が LINE のユーザーにゲームの URL を渡すので、
エージェントがトークにゲームのボタンを出します。LIFF 画面のゲームは AI と保存を LINE の本人で使え、保存すると LINE のトークに知らせが届きます
([PRD](docs/prd/resume-arcade.md) の「LINE のとき」)。
トークに直接送ったファイルと写真は読みません。LIFF の設定が無いときは、エージェントにも「LINE では使えない」と伝え、
それでも勧めたときは文で書くよう案内します。
LIFF の中でマイクが使えるかは、下の「LINE の中でマイクが使えるかを確かめる」の手順で実機で確かめます。
設定(`LINE_CHANNEL_SECRET` と `LINE_CHANNEL_ACCESS_TOKEN`)が無ければ `POST /line/webhook` は 404 で、ほかは何も変わりません。
設定があるときは、LINE のユーザーIDを作る鍵 `AICA_ID_KEY` も要ります(下の 3)。

人がする操作です。LINE のアカウントと、手元を https で出すツール(開発中は Tailscale Funnel。cloudflared でもよい)が要ります。

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
4. 手元の Web サーバーを、Tailscale Funnel で https に出す。URL は `https://<マシン名>.<tailnet>.ts.net` で、起動し直しても変わりません

   ```bash
   ./.venv/bin/python -m line_runtime.server                 # 1 つ目のターミナル
   tailscale funnel 8030                                     # 2 つ目のターミナル。https://<マシン名>.<tailnet>.ts.net が出る
   ```

   - 初めてのときは、tailnet で HTTPS の証明書と Funnel を使えるようにする必要があります。`tailscale funnel` が出す案内の
     リンクから、管理画面で許可します([Tailscale Funnel](https://tailscale.com/kb/1223/funnel))
   - cloudflared の一時のトンネル(`cloudflared tunnel --url http://127.0.0.1:8030`)でも試せます。ただし起動するたびに
     URL が変わるので、そのたびに 5 と 6 と、下の「LINE で LIFF 画面を開く」のエンドポイント URL をやり直します
5. 出た URL を `.env` の `AICA_PUBLIC_URL` に書き(例 `AICA_PUBLIC_URL=https://mymac.tail1234.ts.net`)、
   `.env` に `AICA_TRUST_PROXY=1` も書いて、Web サーバーを起動し直す。
   Funnel 越しの接続元は全員 `127.0.0.1` なので、これが無いと全員が 1 つの IP として数えられます。
   1 人が Cookie を替えて匿名ユーザーIDを 20 個使うと、ほかの新しい Web の利用者はその日ずっと 429 になります(下の「設定」のターン数の上限)
6. LINE Developers の「Messaging API設定」で、Webhook URL を `https://<マシン名>.<tailnet>.ts.net/line/webhook` にし、
   「Webhookの利用」をオンにして「検証」を押す(成功と出れば、署名とつながりが合っています)
7. 同じ画面の QR コードで友だち追加すると、挨拶が届きます。あとはトークに書けば、エージェントが返します。
   挨拶には、LINE で使える入口(LIFF の設定があれば履歴書の読み込みとゲーム、LiveKit の設定もあれば音声相談)のボタンと、
   文字の選択肢(求人を探す / まず相談する / 今はいい)が出ます([設計メモの 12 章](docs/design/line-sim-scenarios.md#12-挨拶から-3-つの入口へ案内する2026-10-25会話の改善-23))

- Funnel を開いている間は、URL を知っている人は誰でも Web のトーク画面と開発者画面も開けます(費用がかかります)。
  試し終わったら止めます(`tailscale funnel 8030` を Ctrl+C。`--bg` で動かしたときは `tailscale funnel reset`)
- 返信には LINE の返信トークを使います。間に合わなかったときだけ push で返し、push は LINE 公式アカウントの月のメッセージ数に数えられます
- 求人カードのボタンと LIFF 画面へのリンクは、LIFF の設定(下の「LINE で LIFF 画面を開く」)があれば LIFF アプリとして開き、
  トークの本人の画面になります。無ければ LINE の中のブラウザで `AICA_PUBLIC_URL` の画面を開き、Cookie で本人を決めるので、
  LINE のユーザーの画面にはなりません([HLD の 4.8](docs/architecture/hld.md#48-line-のトークmessaging-api))
- ターン数の上限は LINE のユーザーごとと全体で数えます。IP ごとの上限は LINE にはかかりません
- `AICA_TRUST_PROXY=1` のとき、IP は `X-Forwarded-For` の最後の値です。本人が書いた値は見ないので、書き換えても IP の上限を迂回できません。
  手元だけで使うときは、`.env` から消してかまいません
  - Tailscale Funnel: 本人が送った `X-Forwarded-For` は捨て、Funnel につないだ本人の IP を 1 つだけ入れて渡します。
    最後の値(1 つだけの値)が本人の IP なので、`AICA_TRUST_PROXY=1` の数え方と合います。tailnet の中から(Funnel でなく)開いたときは、
    その端末の tailnet の IP(`100.x.y.z`)になります。Tailscale のソース(`ipn/ipnlocal/serve.go` の `addProxyForwardedHeaders`、
    2026-10-25 の main)で確かめました。実機の Funnel では測っていません
  - cloudflared: Cloudflare は本人が送った値の最後に、Cloudflare へつないだ接続元を足します
    ([Cloudflare の HTTP ヘッダー](https://developers.cloudflare.com/fundamentals/reference/http-headers/))。最後の値が本人の IP なので、同じく合います
  - どちらも、Web サーバーが `127.0.0.1` だけで待ち受けている(`python -m line_runtime.server` の既定)ので、プロキシを通らずに
    `X-Forwarded-For` を書いて直接つなぐことはできません。`AICA_TRUST_PROXY=1` のまま外から直接つなげる形で動かさないでください

### LINE で LIFF 画面を開く

LINE のトークに出るボタン(履歴書の読み込み、音声相談、レジュメ・アーケード、応募フォームの続き、履歴書の読み取り結果、下書き)を、
本物の LIFF アプリ(LINE ミニアプリ)として開けます。LINE のアプリで開くと、LIFF の ID トークンで本人を確かめ、
トークと同じ本人(LINE のユーザーID `l-…`)の画面になります。音声相談と履歴書の結果は、その本人の LINE のトークに届きます。
設定(`LINE_LIFF_ID` と `LINE_LIFF_CHANNEL_ID`)が無ければ今までどおりで、起動も止めません。流れは [HLD の 4.8](docs/architecture/hld.md#48-line-のトークmessaging-api) にあります。

人がする操作です。上の「LINE で試す」の 1 から 6 が済んでいる前提です。

1. [LINE Developers コンソール](https://developers.line.biz/console/) で、**Messaging API のチャネルと同じプロバイダー**を開き、
   「新規チャネル作成」で「LINE ミニアプリ」のチャネルを作る(未認証のままでかまいません)。
   **LINE の userId はプロバイダーごとに違います**。別のプロバイダーに作ると、同じ人でもトークの本人とは別の人になります。
   LINE ログインのチャネルに作った LIFF アプリは、あとでミニアプリに移せません
2. ミニアプリのチャネルには、開発中・審査用・公開済みの環境があり、それぞれに LIFF ID とエンドポイント URL があります。
   手元で試すときは「開発中」の環境を使います
   - エンドポイント URL: `https://<マシン名>.<tailnet>.ts.net/liff/line`(公開 URL の後ろに `/liff/line`)
   - Scope: `openid` に印を付ける(ID トークンに要ります。`profile` などは要りません)
3. その環境の LIFF ID(`1234567890-AbcdEfgh` の形)を `.env` の `LINE_LIFF_ID` に、チャネル ID を `LINE_LIFF_CHANNEL_ID` に書き、
   Web サーバーを起動し直す。チャネル ID は ID トークンの `aud` と照らします。環境ごとに違う見込みなので、使う環境のものをコンソールで確かめます
4. 音声相談も使うときは、Web と同じく LiveKit の設定(`LIVEKIT_URL` など)と音声ワーカーが要ります。無ければ、音声相談のボタンは出さず、
   エージェントにも LINE では使えないと伝えます
5. 開発中の環境は、チャネルの管理者とテスターしか開けません。知人に試してもらうときは、テスターに足します

- 公開用(Render の URL、jyoka/tasks#64)は、同じチャネルの公開済みの環境のエンドポイント URL を Render の URL にし、その環境の LIFF ID と
  チャネル ID を Render の環境変数に書きます。1 つの環境にエンドポイント URL は 1 つです
- 前の名前の `LINE_LOGIN_CHANNEL_ID` も読みます(`LINE_LIFF_CHANNEL_ID` があればそちらが先)。名前を変えたのは、LIFF アプリを LINE ログインの
  チャネルではなくミニアプリのチャネルに置くようにしたからです。前の `.env` はそのまま動きます
- LINE のボタンは `https://liff.line.me/<LIFF ID>/<画面>` の形です(ミニアプリの LIFF ID でも、この形で開けます)。
  LINE はまずエンドポイント URL を `liff.state=/<画面>` つきで開き、`liff.init` が `/liff/line/<画面>` へ送ります。この下の画面は Cookie の匿名ユーザーIDを見ません。
  画面は 1 つのアプリの中でパスで分けます

  | 画面 | LIFF URL の後ろ | 中身 |
  |---|---|---|
  | 音声相談 | `/voice` | Web と同じ音声相談のシート。終わると、まとめと求人と下書きのボタンが LINE のトークに届く |
  | 履歴書の読み込み | `/resume/upload` | ファイル(PDF / JPEG / PNG)を選んで読み取る。確認とエージェントの次の 1 問が LINE のトークに届く |
  | レジュメ・アーケード | `/arcade`、`/arcade/<ゲーム>` | Web と同じゲーム。AI(回数の上限は Web と同じ)と保存は LINE の本人で使う。保存すると「下書きができました」と「下書きを見る」が LINE のトークに届く |
  | 応募フォームの続き、読み取り結果、下書き | `/apply?job=…`、`/resume?rid=…`、`/voice/draft?rid=…`、`/arcade/draft?rid=…` | Web と同じ画面 |
- トークに届けるのは push です(LINE 公式アカウントの月のメッセージ数に数えられます。ゲームは保存 1 回につき 1 通で、1 人 1 日 3 通まで)。宛先の LINE の userId は、操作を始めるとき
  (ゲームは保存するとき)に ID トークンからもらい、届けるまでメモリにだけ置きます。面談の途中で Web サーバーを起動し直すと、そのまとめはトークに届きません(会話には入ります)
- 確かめた本人は、署名つきの Cookie で覚えます。ブラウザ(LINE の中のブラウザ)を閉じるまでで、長くても 1 時間です。
  過ぎたら、開いたときにもう一度 ID トークンで確かめます。1 時間を過ぎてから応募フォームを送ると、入力は残らず、開き直しになります
- ID トークンを確かめられない(LINE が断った、期限切れ、別のチャネルのトークン)ときは「本人を確かめられませんでした」と出して止まり、
  ほかの人の画面にはなりません

### LINE のリッチメニュー

LINE のトークの下に、常に出しておけるメニュー(リッチメニュー)を置けます。ボタンは 4 つです。

| ボタン | 押すと |
|---|---|
| 音声で相談する | 音声相談の LIFF 画面(`https://liff.line.me/<LIFF ID>/voice`)を開く。LiveKit の設定があるときだけ置く |
| 履歴書を読み込む | 履歴書の読み込みの LIFF 画面(`…/resume/upload`)を開く |
| ゲームで書類を作る | レジュメ・アーケードの LIFF 画面(`…/arcade`)を開く |
| 求人を探す | 「求人を探す」を本人の発言としてトークに送る(エージェントが返信トークで返す) |

前の 3 つは、トークのボタンと同じ LIFF 画面で、ボタンの文言も同じです(`line_channel.LIFF_ACTIONS` と `ARCADE_LABEL`。絵文字は付けません)。押した後の流れ(結果がトークに届く)も同じです。
LIFF の設定が無いときは作りません(スクリプトが止まります)。登録するのは人で、`scripts/line_rich_menu.py` が画像(2500x843 の PNG)も作ります。
上の「LINE で試す」と「LINE で LIFF 画面を開く」が済んでいる前提です。

1. Web サーバーと同じ `.env`(`LINE_CHANNEL_ACCESS_TOKEN`、`LINE_LIFF_ID`、`LINE_LIFF_CHANNEL_ID`、音声相談も置くなら `LIVEKIT_*`)を用意する
2. 画像と本文を作って見る(LINE は呼びません)。`data/richmenu/richmenu.png` と `richmenu.json` ができます

   ```bash
   ./.venv/bin/python -m scripts.line_rich_menu --dry-run
   ```

   日本語のフォントは macOS のヒラギノか Linux の Noto Sans CJK を探します。無ければ `--font <ファイル>` で渡します
3. 登録する。LINE で形を確かめ、作り、画像を上げ、友だち全員の既定のメニューにします

   ```bash
   ./.venv/bin/python -m scripts.line_rich_menu             # 初めて
   ./.venv/bin/python -m scripts.line_rich_menu --replace   # 作り直すとき(前にこのスクリプトで作ったメニューを消してから)
   ```

   `--replace` は、新しいメニューを既定にした後で、前にこのスクリプトで作ったメニュー(名前が `aica-menu`)だけを消します。
   途中で LINE に断られたら、そこで止まり、前のメニューは出たままです。
   **既定のメニューは友だち全員に 1 つです**。LINE Official Account Manager などで既定にしていた別のメニューは、既定でなくなります
   (消しはしません。スクリプトがその ID を出します)
4. LINE のアプリでトークを開き、下のメニューを押して確かめる。LIFF ID やエンドポイント URL を変えたら、もう一度 3 をします

月のメッセージ数(無料のコミュニケーションプランは月 200 通)との関係(2026-10-25 に LINE の公式の資料で確かめた):

- 月のメッセージ数に数えると公式が書いているのは、メッセージ配信と、Messaging API の Push、Multicast、Broadcast、Narrowcast だけです。
  応答メッセージ(Reply API)、あいさつメッセージ、チャットは数えません([Messaging API の料金](https://developers.line.biz/ja/docs/messaging-api/pricing/)、
  [LINE公式アカウントの料金プラン](https://www.lycbiz.com/jp/service/line-official-account/plan/))
- **リッチメニューの登録と表示が数えられるかは不明です**。どちらの資料にも、数える側にも数えない側にも書いてありません。
  リッチメニューの API(作る、画像を上げる、既定にする)は、数えると書かれたメッセージの送り方のどれでもありません(数えられないと推測しますが、確かめていません)。
  確かめるときは、LINE Official Account Manager の「分析」か、Messaging API の「当月のメッセージ利用状況」で、登録の前後の通数を比べます
- 「求人を探す」を押したときの返事は返信トークで返すので、数えません(間に合わなかったときの push だけ数えます。上の「LINE で試す」)。
  前の 3 つから開いた画面の結果は、トークのボタンから開いたときと同じく push で届き、数えます(上の「LINE で LIFF 画面を開く」)

### LINE の中でマイクが使えるかを確かめる

音声相談を LINE から使えるかを決めるために、LIFF の画面の中でマイク(`getUserMedia`)と通話の部品(WebRTC、LiveKit)が
使えるかを、人が実機で確かめます。上の「LINE で LIFF 画面を開く」が済んでいる前提です。画面は `/liff/line/mic-check` です。

- 声は送らず、残しません。サーバーに送るのは結果(使えたか、エラーの名前、音の大きさの最大、OS、LINE の版、LIFF SDK の版、
  画面のサイズ、ブラウザの User-Agent)だけで、本人(LINE のユーザーID)と IP は書きません
- 通話の部品は手元で試すだけで、どこにもつなぎません(LiveKit の部屋にも入りません)。本物の音声相談がつながるかは、この確認の後です

人がする操作です。iPhone と Android の両方で、1 から 5 をします。

1. Web サーバーと Tailscale Funnel を起動しておく(上の「LINE で試す」の 4)
2. 自分だけのトークルーム(Keep メモなど)に `https://liff.line.me/<LIFF ID>/mic-check` を送り、LINE のアプリの中でそのリンクを押す。
   LIFF の画面「マイクの確認」が開きます。aica の公式アカウントとのトークに送ると、エージェントが返事をします(費用がかかる)
3. 「マイクを試す」を押し、マイクの許可を求められたら許可して、5 秒ほど声を出す。画面の結果を見ます
   - 「マイク」: 使えた / 断られた / エラー(と、エラーの名前) / この画面には無い
   - 「音の大きさ」: 声を出したのに 0.01 未満なら、許可されても音が届いていません
   - 「通話の部品(WebRTC)」と「LiveKit」: 使える、対応している、なら音声相談の部品は動きます
   - 「OS」「LINE のアプリの中」「LINE の版」「LIFF SDK の版」「画面のサイズ」
   - 「LIFF の初期化: 5 秒より遅れて終わった」が出たら、通信が遅く、サーバーのログの行は OS などが空のままです。画面の結果を正として報告します
4. 断られたときは、端末の設定(iPhone は「設定」→「LINE」→「マイク」、Android は「設定」→「アプリ」→「LINE」→「権限」)で許可してから、
   LIFF の画面を閉じて 2 から試し直す。許可しても使えないかも見ます
5. 「外のブラウザで同じ確認を開く」を押し、開いた外のブラウザ(iPhone は Safari、Android は Chrome など)で 3 をもう一度する。
   LIFF の中で使えなかったときの逃げ道が使えるかを確かめます

報告すること(端末ごと、LIFF の中と外のブラウザのそれぞれ):

- 端末と OS の版(例: iPhone 15、iOS 18.1)、画面の結果のスクリーンショット
- マイクの許可を求める表示が出たか、出たのは LINE のものか OS のものか、許可したか
- サーバーのログの 1 行(Web サーバーの 1 つ目のターミナルに `LIFF のマイクの確認: {…}` と出ます。Render では Logs の画面)

ログの行は 1 回の確認につき 1 行です。1 日に 500 行を超えると、その日はもう書きません(本人を確かめない口なので、ログをあふれさせない)。

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
| LIFF 画面 | `/liff/*` | dip の画面のモック。応募フォーム、読み取り結果、下書き。Web のブラウザでは Cookie の匿名ユーザー、LINE のアプリでは `/liff/line/*`(LIFF の ID トークンで確かめた LINE のユーザー。音声相談と履歴書の読み込みの画面もある。上の「LINE で LIFF 画面を開く」) |
| マイクの確認 | `/liff/line/mic-check` | LINE の LIFF の中でマイクと通話の部品が使えるかを試す。本人を決めない。LIFF の設定があるときだけ(上の「LINE の中でマイクが使えるかを確かめる」) |
| レジュメ・アーケード | `/liff/arcade` | 遊びながら職務経歴書を作るゲームのハブ。先にトーク画面(`/`)を開いてから `http://127.0.0.1:8030/liff/arcade` を開くと、ハブの 2 本は AI(`LINE_SIM_PROVIDER` のプロバイダ)で遊べて、できた書類をプロフィールに保存できる。API キーが無い、上限に達した、AI が失敗したときは簡易分析で続く。プロフィールの「ゲームで作った下書き」からも入れる。ハブに出ない 6 本は開発者画面の左からリンクする(AI なし) |
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
| `python -m evals.run --only F-follow-new --channel line --features resume_upload,arcade` | 評価を LINE のトークの窓口で回す(`--features` は LIFF 画面で使えるもの)。`--ledger <台帳> --limit-usd 1.0` で、PRD とは別の予算のカードの台帳と止める額を使う | 使う |
| `python -m evals.run --rejudge 結果.json` | 保存した会話を、ジャッジだけで判定し直す | 使う |
| `python3 evalkit/aggregate.py diff 前.json 後.json` | 評価の結果の前後を比べる | 使わない |
| `python -m evals.budget` | 予算台帳の累計と、実行ごとの内訳 | 使わない |
| `python -m evals.search_eval eval` | 意味検索の再現率(保存済みのベクトルで測る) | 使わない |
| `python -m line_runtime.ledger [--date 2026-09-27] [--by-user-id] [--json]` | 費用台帳を人と日ごとに表で集計する(合計の行つき)。`--by-user-id` はシナリオユーザーを匿名ユーザーIDにまとめない | 使わない |
| `python -m line_runtime.purge [--url https://…]` | 期間で消す掃除を今すぐ走らせる。動いている Web プロセス(既定 `http://127.0.0.1:8030`)の内部の口を `AICA_INTERNAL_KEY` で呼ぶだけで、このコマンドは何も消さない。掃除は Web プロセスが起動時と 1 日 1 回、自分でも走らせる | 使わない |
| `python -m resume_intake.cli <ファイル>` | 履歴書を 1 つ読み取る | 使う |
| `python -m scripts.line_rich_menu [--dry-run] [--replace]` | LINE のリッチメニューを作って登録する(上の「LINE のリッチメニュー」)。`--dry-run` は画像と本文を作るだけ | 使わない(LINE を呼ぶ) |
| `python -m scripts.deploy_smoke [--url https://…] [--cookie w-… --expect revisit]` | デプロイの smoke。手元ではコンテナを作って起動し、ヘルスチェック、初回の挨拶、再起動の後に残ることを確かめる。`--url` は公開 URL で確かめる([docs/deploy/render.md](docs/deploy/render.md)) | 使う |
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

保存する期間は環境変数ではなく、コードの定数です(数の理由は [docs/design/privacy.md](docs/design/privacy.md) の 3.3)。
Web プロセスが起動時と 1 日 1 回、期間を過ぎたものを消します。

| 消すもの | 期間 | 定数 |
|---|---|---|
| 会話(`data/line_checkpoints.db`) | 会話ごとに、最後のやりとりから 30 日 | `line_runtime.purge.CONVERSATION_TTL` |
| 使われなくなった人(`data/state.json` の行、連携、音声の下書きのファイル) | 最後の利用から 180 日 | `dip_mcp.retention.USER_TTL` |
| 「すべて消す」で持ち越した部屋の名前 | 消した時刻から 1 日 | `dip_mcp.retention.FORGOTTEN_ROOMS_TTL` |
| 費用台帳(`data/costs.jsonl`)のユーザーID | 90 日。過ぎたら日ごとの合計にする。「すべて消す」では、すぐにその人のユーザーIDを外す(金額は残す) | `line_runtime.ledger.ID_TTL` |
| Cookie `aica_uid` | 最後に画面を開いた日から 180 日 | `line_runtime.server.ANON_COOKIE_MAX_AGE` |

| 環境変数 | 既定 | 用途 | `.env` |
|---|---|---|---|
| `OPENAI_API_KEY` | なし(必須) | エージェント、履歴書の読み取り、意味検索、音声アドバイザー、下書き | 可 |
| `ANTHROPIC_API_KEY` | なし | `LINE_SIM_PROVIDER=anthropic` のとき | 可 |
| `LINE_SIM_PROVIDER` | `openai` | LLM のプロバイダ(`openai`、`anthropic`)。エージェント、模擬ユーザー、ジャッジのモデルが変わる | 不可 |
| `LINE_SIM_MODEL` | `gpt-6-sol`(anthropic なら `claude-opus-5`) | エージェントのモデル | 不可 |
| `LINE_SIM_EFFORT` | `low` | エージェントの思考の深さ | 不可 |
| `LINE_SIM_PORT` | `8030` | Web サーバーのポート(コンテナでは `PORT`、既定 10000) | 不可 |
| `LINE_SIM_HOST` | `127.0.0.1` | Web サーバーが待ち受けるアドレス(コンテナでは `0.0.0.0`) | 不可 |
| `AICA_SECURE_COOKIE` | なし(コンテナの image では `1`) | `1` のとき、匿名ユーザーIDの Cookie に Secure を付ける。https で公開するときに使う。http で開くと Cookie が送り返されない | 可 |
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
| `AICA_TRUST_PROXY` | なし | `1` のときだけ、IP を `X-Forwarded-For` の最後の値から取る。プロキシの後ろに置くとき(Render、Tailscale Funnel、cloudflared のトンネル)に使う。無いと全員がプロキシの IP として数えられる | 可 |

レジュメ・アーケードのゲームの AI の上限です([HLD の 4.11](docs/architecture/hld.md#411-レジュメアーケード))。AI つきで始めるとき、ターンを 1 つ数えます。

| 環境変数 | 既定 | 上限と用途 | `.env` |
|---|---|---|---|
| `AICA_ARCADE_AI` | `1` | `0` にするとゲームの AI を止める(すべて簡易分析。保存はできる) | 可 |
| `AICA_ARCADE_DAILY_AI_PLAYS` | 3 | 匿名ユーザーIDごとに、1 日に AI つきで始められる遊びの数。超えたら AI なしで始める | 可 |
| `AICA_ARCADE_REACTIONS_PER_PLAY` | 50 | 1 回の遊びのゲームの中の反応の数。最後のまとめの 2 回は別に取っておく | 可 |
| `AICA_ARCADE_QUICK_MODEL` | `gpt-6-luna`(anthropic なら `claude-haiku-4-5`) | ゲームの中の反応のモデル。最後のまとめは `LINE_SIM_MODEL` | 可 |

LINE 公式アカウントで使うものです。LINE で試さないなら要りません。

| 環境変数 | 既定 | 用途 | `.env` |
|---|---|---|---|
| `LINE_CHANNEL_SECRET` | なし | チャネルシークレット。webhook の署名(`X-Line-Signature`)を確かめる。`LINE_CHANNEL_ACCESS_TOKEN` と両方あるときだけ `POST /line/webhook` を開く(無ければ 404) | 可 |
| `LINE_CHANNEL_ACCESS_TOKEN` | なし | チャネルアクセストークン(長期)。入力中の表示、返信、push、アカウント連携の linkToken の発行に使う | 可 |
| `AICA_ID_KEY` | なし(LINE の 2 つか LIFF の 2 つがあるときは必須) | LINE のユーザーID(`l-…`)を作る HMAC-SHA256 の鍵。LIFF の本人の Cookie の署名の鍵も、ここから作る。32 バイト以上(例 `python -c "import secrets; print(secrets.token_hex(32))"`)。LINE の 2 つか LIFF の 2 つがあるのに無い、短いときは起動で止まる。変えると全員の ID が変わり、それまでのデータとつながらない([ADR-0008](docs/adr/0008-keyed-line-user-id.md)) | 可 |
| `AICA_PUBLIC_URL` | なし | 手元の Web サーバーの公開 URL(Tailscale Funnel かトンネルの https の URL)。LINE で出す LIFF 画面(LIFF の設定が無いとき)と dip のログインの画面へのリンクの前に付ける。無ければリンクを出さず、アカウント連携も始めない | 可 |
| `LINE_LIFF_ID` | なし | LIFF アプリ(LINE ミニアプリのその環境)の LIFF ID。`LINE_LIFF_CHANNEL_ID` と両方あるときだけ、LINE のボタンと操作(履歴書、音声相談、レジュメ・アーケード)を LIFF アプリで開き、`/liff/line/…` の画面と `POST /api/liff/…` を開く(無ければ 404 で、ほかは今までどおり)。そのときは `AICA_ID_KEY` も要る(無い、短いときは起動で止まる) | 可 |
| `LINE_LIFF_CHANNEL_ID` | なし | LIFF アプリを置いたチャネル(LINE ミニアプリのその環境)の ID。LIFF の ID トークンを LINE に確かめてもらうときの `client_id`(`aud`)。前の名前の `LINE_LOGIN_CHANNEL_ID` も読む(こちらが先) | 可 |

音声相談で使うものです。トークだけを試すなら要りません。

| 環境変数 | 既定 | 用途 | `.env` |
|---|---|---|---|
| `LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET` | なし(必須) | LiveKit Cloud(URL は `wss://<project>.livekit.cloud`)。Web プロセスが部屋の接続トークンを発行し、音声ワーカーが部屋に入る | 可 |
| `DEEPGRAM_API_KEY` | なし(必須) | 文字起こし | 可 |
| `VOICE_RESUME_STT_PROVIDER` | `deepgram` | 文字起こしの提供元。**本番は `deepgram` に固定する**(`render.yaml` も `deepgram`)。`openai` は開発の比較用で、声そのものが OpenAI に届く。替えるならプライバシーポリシーと音声相談の同意の文言も直す([privacy.md](docs/design/privacy.md) の 3.2)。Deepgram には `mip_opt_out=true` を付けて送り、改善プログラムに使わせない | 不可 |
| `CARTESIA_API_KEY` | なし(必須) | 読み上げ | 可 |
| `AICA_VOICE_WORKER` | なし | コンテナで `1` のとき、Web プロセスと同じコンテナで音声ワーカーも動かす(`scripts/container_start.sh`) | 不可 |
| `AICA_INTERNAL_KEY` | なし(音声相談の設定があるときは必須) | 内部の鍵(32 文字以上)。音声ワーカーが `POST /api/voice/result` と `POST /api/voice/session` に付け、Web サーバーが照合する。音声相談の設定(`LIVEKIT_*`)があるのに無い、短いときは、Web サーバーも音声ワーカーも起動で止まる。2 つのプロセスで値が違うと結果は 401 で届かず、ワーカーのログにエラーが出る。手で掃除を走らせる `python -m line_runtime.purge` も、`POST /api/internal/purge` に付ける | 可 |
| `VOICE_RESUME_TALK_URL` | `http://127.0.0.1:8030` | 音声ワーカーが結果を返す Web サーバーの URL | 不可 |
| `VOICE_RESUME_WRITE_FILES` | なし | `1` のときだけ、相談ごとの下書き、文字起こし、求人の提案を `VOICE_RESUME_OUT_DIR` に書き出す(開発用)。本番では付けない。無くても下書きは dip の状態に入り、トークに届く([privacy.md](docs/design/privacy.md) の 3.3) | 不可 |
| `VOICE_RESUME_OUT_DIR` | `data/voice-resume` | `VOICE_RESUME_WRITE_FILES=1` のときの、下書き、文字起こし、求人の提案の置き場 | 不可 |
| `VOICE_RESUME_MAX_SESSION_MINUTES` | `10` | 1 回の音声相談の長さの上限(分)。時間が来たら、下書きを保存していなければ締めの返答を作り、部屋を閉じる | 不可 |
| `VOICE_RESUME_MAX_WEB_SEARCHES` | `5` | 1 回の音声相談で web を調べる回数の上限(調べもの、平均時給、会社の 3 つの合計) | 不可 |
| `VOICE_RESUME_MAX_JOBS` | 4 | 音声ワーカーが同時に受ける相談の数。超えた部屋にはアドバイザーが入らない(`dev` でも) | 不可 |
| `VOICE_RESUME_JOB_MEMORY_LIMIT_MB` | 2000 | 相談 1 つのプロセスのメモリの上限(MB)。超えたらその相談を止める。0 で上限なし | 不可 |
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
| 個人情報の流れ、保存の期間、同意の設計(公開の前) | [docs/design/privacy.md](docs/design/privacy.md) |
| 利用規約とプライバシーポリシー(版 1.0。画面は `/legal/terms`、`/legal/privacy`。法律の確認は利用者がする) | [docs/legal/terms.md](docs/legal/terms.md)、[docs/legal/privacy-policy.md](docs/legal/privacy-policy.md)、[docs/legal/README.md](docs/legal/README.md) |
| 評価の結果の集計と比較 | [evalkit/README.md](evalkit/README.md) |
| Render での公開の手順、環境変数、置くデータ | [docs/deploy/render.md](docs/deploy/render.md) |
