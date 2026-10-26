# Render で公開する

aica を Render の Web サービス(Standard。音声相談も同じコンテナで動かす)に、1 つのコンテナで公開する手順です。
**【運営者】** と書いた手順は、Render のアカウントを持つ運営者がします。それ以外は、リポジトリにあるものだけで済みます。

## 構成

| もの | 中身 |
|---|---|
| `Dockerfile` | Python 3.12 の image。Web プロセス(`line_runtime.server`)と、その子プロセスの dip の MCP サーバーが入る |
| `scripts/container_start.sh` | コンテナの起動。`0.0.0.0:$PORT` で Web プロセスを動かす。`AICA_VOICE_WORKER=1` なら音声ワーカーも動かす |
| `render.yaml` | Render の Blueprint。Web サービス 1 つ、永続ディスク 1 つ、ヘルスチェック、環境変数 |
| `GET /healthz` | ヘルスチェック。起動を終えていれば 200。LLM も dip も呼ばず、Cookie も発行しない |
| `scripts/deploy_smoke.py` | デプロイの smoke。ヘルスチェックと初回の挨拶の 1 往復を確かめる(実 API を 1 回使う) |

- 状態と出力は、すべてコンテナの `/app/data` に書きます(会話、dip の状態、プロフィール、費用台帳、ターン数、音声の下書き)。
  ここに永続ディスクをつなぎます。ほかの場所は、デプロイと再起動のたびに消えます
- ディスクは 1 つのインスタンスにしかつながりません。インスタンスは 1 つ、Web プロセスのワーカーも 1 つです
  (ターン数の上限 `data/quota.json` を 1 つのプロセスで数えるため)
- ディスクをつなぐと、デプロイのたびに数秒止まります(Render は古いインスタンスを止めてから新しいものを起動する)

## 手元で smoke を通す

docker と、リポジトリ直下の `.env` の `ANTHROPIC_API_KEY` と `OPENAI_API_KEY` が要ります。

```bash
./.venv/bin/python -m scripts.deploy_smoke              # build(初回は約 5 分)→ 起動 → 確かめる → 再起動 → 確かめる → 片付け
./.venv/bin/python -m scripts.deploy_smoke --no-build   # 作ってある image(aica:smoke)を使う
```

確かめることです。どれかが違えば `NG` と出て、終了コードが 1 になります。

- image に `.env` が入っていない(`.dockerignore`)。API キーは `docker run` の環境変数でだけ渡す
- `/healthz` が 200 になる。`GET /` の Cookie に `Secure` と `HttpOnly` が付く
- 新しいブラウザで開くと、まず使い始める同意を求める(入口は無く、LLM を呼ばない)。画面の「同意して始める」と同じく
  今の版で同意すると(`POST /api/consent`)初回訪問(`visit`)になり、挨拶の吹き出しが返る
- 画面(`/`、`/dev`、`/static/talk-core.js`)と返信と履歴に、`ANTHROPIC_API_KEY` と `OPENAI_API_KEY` の値が出てこない
- `/app/data` がコンテナの外のボリュームで、コンテナを再起動しても会話の履歴と同意と dip の状態が残る

2026-10-25 に手元(OrbStack)で通しました。挨拶の後のコンテナのメモリは約 200MB でした(Starter は 512MB)。

## Render に公開する

### 1. Blueprint からサービスを作る **【運営者】**

1. [Render のダッシュボード](https://dashboard.render.com/) で「New」→「Blueprint」を選び、GitHub の `jyoka/aica` をつなぐ。
   ブランチは `main`
2. `render.yaml` が読まれ、Web サービス `aica`(Standard、Singapore)とディスク `aica-data`(2GB、`/app/data`)が出る
3. `ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`、`DEEPGRAM_API_KEY`、`CARTESIA_API_KEY` の値を入れる
   (`sync: false` なので、ここで聞かれる。リポジトリには書かない)
4. 「Deploy Blueprint」でサービスを作る(前の画面では「Apply」だった)。最初の build に数分かかる。「Events」で `Deploy live` になるまで待つ

- `autoDeployTrigger: "off"` なので、`main` に push しても自動ではデプロイしません。更新は「Manual Deploy」→「Deploy latest commit」でします
- `AICA_TRUST_PROXY=1` と `AICA_INTERNAL_KEY`(乱数)は Blueprint が入れます。`AICA_INTERNAL_KEY` は同じコンテナの Web プロセスと
  音声ワーカーが同じ値を読むので、そろえる手間はありません。音声相談の設定(`LIVEKIT_*`)があるのに `AICA_INTERNAL_KEY` が無い、
  短い(32 文字未満)ときは、Web プロセスも音声ワーカーも起動で止まります。ダッシュボードで作るときは、忘れずに長い乱数を入れます
- Blueprint を使わずにダッシュボードで作るときは、下の「環境変数」の表と、Health Check Path `/healthz`、
  ディスクの Mount Path `/app/data` を同じに設定します。Docker Command は空のままにします(`Dockerfile` の CMD を使う)

### 2. 公開 URL で smoke を通す **【運営者】**

サービスの URL(`https://<名前>.onrender.com`)で、手元から smoke を流します。build はしません。

```bash
./.venv/bin/python -m scripts.deploy_smoke --url https://<名前>.onrender.com
```

- 手元の smoke と同じ確かめに加え、`http://` で開くと `https://` に飛ぶことも確かめます
- 画面にキーが出ないかは、手元の `.env` の `ANTHROPIC_API_KEY` と `OPENAI_API_KEY` の値で探します。Render に入れたキーと同じときだけ意味があります
- 最後に、このときの匿名ユーザーID(`w-…`)が出ます

### 3. トークの 1 往復と再訪の挨拶を確かめる **【運営者】**

1. スマホのブラウザで公開 URL を開く。同意の画面で「同意して始める」を押し、挨拶が出たら、1 回書いて返事が来ることを確かめる
2. 30 分より後に、同じブラウザで開き直す。前回の続きを踏まえた再訪の挨拶になることを確かめる
3. 2 の手順の smoke で出た匿名ユーザーIDでも確かめられます

   ```bash
   ./.venv/bin/python -m scripts.deploy_smoke --url https://<名前>.onrender.com --cookie w-… --expect revisit
   ```

4. 「Manual Deploy」で再デプロイした後にも 1 と同じブラウザで開き直し、会話とプロフィールが残っていることを確かめる

### 4. IP の数え方を確かめる **【運営者】**

ターン数の IP ごとの上限が、本人の IP で数えられているかを確かめます(`line_runtime/server.py` の `_client_ip`)。

1. 自宅の Wi-Fi の PC と、Wi-Fi を切った携帯の 2 台から公開 URL を開き、1 ターンずつ送る
2. 各機器で `curl https://ifconfig.me` と `curl -6 https://ifconfig.me` を実行し、外向きの IP を控える
3. Render のサービスの「Shell」で `cat data/quota.json` を実行し、`ips` のキーが控えた IP と一致するかを見る
   - 一致しない、2 台が同じキー、内部の IP(`10.x`、`172.16`〜`31.x`、`100.64.x`)や Cloudflare の範囲、`:ポート` 付きのどれかなら、
     `X-Forwarded-For` の取り方(今は最後の値)を直します
   - IPv6 のキーは `/64` にまとめた形(`2001:db8:1:2::/64` など)です
4. 詐称できないことを確かめる。新しい匿名ユーザーIDで `X-Forwarded-For: 1.2.3.4` を付けてプロフィールに書き、
   3 と同じく `data/quota.json` を見て、`ips` に `1.2.3.4` が無く、自分の IP のキーに 1 人増えていること

   ```bash
   curl -s -c /tmp/aica.jar -o /dev/null https://<名前>.onrender.com/     # 新しい匿名ユーザーIDの Cookie を受け取る
   curl -s -b /tmp/aica.jar -X DELETE -H 'X-Forwarded-For: 1.2.3.4' https://<名前>.onrender.com/api/profile/track
   ```

   - プロフィールの書き込みは LLM を呼ばず、ターンも使いません。この IP の匿名ユーザーIDとして、0 ターンで数えます(`_admit`)
   - 空の発言(400)では確かめられません。ターンを受け付ける前に断るので、IP を数えないためです

- 開始コマンドに `--forwarded-allow-ips` を付けたり、`FORWARDED_ALLOW_IPS` を設定したりしないでください。付けると uvicorn が
  `X-Forwarded-For` で接続元を書き換え、IP を詐称できます(`scripts/container_start.sh` は付けません)

### 5. デモの日の上限 **【運営者】**

社内の回線から大勢でデモをする日は、IP ごとの上限を緩めます。10 人が同じ回線から 15 ターンずつ使うと、IP の上限 150 に届くためです。

1. 「Environment」で `AICA_DAILY_TURN_LIMIT_PER_IP=0`(上限なし。または 500)と `AICA_DAILY_NEW_IDS_PER_IP=100` にする
2. 「Save Changes」で再起動される。デモが終わったら消して、もう一度保存する

全体の上限(`AICA_DAILY_TURN_LIMIT_TOTAL`、既定で 1 日 1000 ターン)は残るので、1 日の費用の天井は変わりません。

## 環境変数

Render の「Environment」に置きます。`.env` はコンテナに入れません。表に無いものは README の「設定」の既定のままです。

| 環境変数 | 値 | 入れる者 | 用途 |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic の API キー | **【運営者】** Blueprint を作るとき | エージェント、ゲームの AI、履歴書の読み取り、音声相談(既定の `LINE_SIM_PROVIDER=anthropic`) |
| `OPENAI_API_KEY` | OpenAI の API キー | **【運営者】** Blueprint を作るとき | 求人の意味検索の埋め込み(無ければ部分一致の検索に戻る) |
| `AICA_TRUST_PROXY` | `1` | Blueprint | IP を `X-Forwarded-For` の最後の値から取る(Render のプロキシの後ろ) |
| `AICA_INTERNAL_KEY` | 乱数 | Blueprint(`generateValue`) | 音声ワーカーが結果を返すときの共有鍵 |
| `VOICE_RESUME_STT_PROVIDER` | `deepgram` | Blueprint | 本番の文字起こしを Deepgram に固定する。変えない(`openai` にすると声そのものが OpenAI に届く。[privacy.md](../design/privacy.md) の 3.2) |
| `AICA_SECURE_COOKIE` | `1` | `Dockerfile` | 匿名ユーザーIDの Cookie に `Secure` を付ける |
| `PORT` | `10000` | Render | `scripts/container_start.sh` が `LINE_SIM_PORT` にする |

音声相談のものです(下の「音声相談」)。Blueprint が入れるか、Blueprint を作るときに聞かれます。

| 環境変数 | 値 | 入れる者 |
|---|---|---|
| `AICA_VOICE_WORKER` | `1`(同じコンテナで音声ワーカーを動かす) | Blueprint |
| `LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET` | LiveKit Cloud のプロジェクト | **【運営者】** Blueprint を作るとき |
| `DEEPGRAM_API_KEY`、`CARTESIA_API_KEY` | 文字起こしと読み上げ | **【運営者】** Blueprint を作るとき |

LINE 公式アカウントもつなぐときに足すものです(README の「LINE で試す」)。Webhook URL は `https://<名前>.onrender.com/line/webhook` です。

| 環境変数 | 値 |
|---|---|
| `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN` | LINE Developers のチャネル |
| `AICA_ID_KEY` | LINE のユーザーIDを作る鍵(32 バイト以上)。**一度決めたら変えない**([ADR-0008](../adr/0008-keyed-line-user-id.md)) |
| `AICA_PUBLIC_URL` | `https://<名前>.onrender.com` |

## API キーはサーバーの中だけ

- API キーは Render の環境変数にだけ置き、image にも リポジトリにも入れません(`.dockerignore` が `.env` を外す)
- ブラウザに渡すのは、匿名ユーザーIDの Cookie(HttpOnly)と、音声相談の部屋に入る数分だけの LiveKit の接続トークンです。
  LLM はすべてサーバーから呼びます
- dip の MCP サーバー(子プロセス)と音声ワーカーは、同じコンテナで親の環境変数を受け取ります
- `/dev`(開発者画面)も公開 URL で開けます。キーは出ませんが、誰でもシナリオを動かせて費用がかかります
  ([review-2026-10.md の R10](../architecture/review-2026-10.md#r10-本番の-web-サーバーが評価のパッケージと開発者画面を抱えている))

## 音声相談

音声ワーカーは Web プロセスと同じ `/app/data`(dip の状態と下書き)を使うので、同じコンテナで動かします
([review-2026-10.md の R9](../architecture/review-2026-10.md#r9-音声ワーカーがweb-プロセスと同じマシンにいる前提で作られている))。
Blueprint は `AICA_VOICE_WORKER=1` で、Standard(2GB)とディスク 2GB にしています。LiveKit の環境変数が無ければ、音声のボタンを押すと「準備ができていません」(503)と出て、ターンは使いません。
LiveKit の環境変数は `AICA_VOICE_WORKER=1` と一緒に入れます(LiveKit だけ入れると、ターンを使って部屋に入っても、答えるワーカーがいません)。

`AICA_VOICE_WORKER=1` にすると、起動のたびに `voice_resume.agent download-files` でモデルを `/app/data/cache/huggingface` に置き
(初回だけ約 460MB をダウンロードし、手元では 1〜3 分かかった。2 回目からはディスクの分を使い、約 7 秒)、`voice_resume.agent start` を裏で動かします。
モデルを取れなかったときは音声ワーカーなしで、ワーカーが落ちたときは 10 秒後に起動し直して、どちらも Web プロセスは止めません
(「Logs」に `WARNING: … voice …` と出ます)。

確かめていないこと、気をつけることです。

- コンテナの中で音声ワーカーが LiveKit につながり、相談の結果がトークに届くところまでは、まだ確かめていません(手元で確かめたのは、モデルの取得と、取れないときやワーカーが落ちたときに Web プロセスが動き続けること)
- Starter の 512MB では足りないと推測して、Standard(2GB)にしました。turn detector のモデルが大きいためです。2GB で足りるかも、まだ確かめていません
- 音声ワーカーは、相談がなくても `VOICE_RESUME_MAX_JOBS`(既定 4)個のプロセスを温めておきます(手元で 1 つ約 200MB)。
  メモリの小さいプランでは `VOICE_RESUME_MAX_JOBS` を下げます(同時に受ける相談の数も同じだけ減ります)
- ディスクの 2GB のうち約 460MB をモデルが使います(増やせるが減らせない)
- 音声相談の費用は、まだ費用台帳に載りません([review-2026-10.md の R6](../architecture/review-2026-10.md#r6-音声相談の費用が台帳に載らず上限も効かない))

## 置くデータ(プライバシーの説明に入れること)

公開する前に、使う人向けのプライバシーの説明を用意します。説明の文と、保存の期間は運営者が決めます
([review-2026-10.md の R7](../architecture/review-2026-10.md#r7-個人情報を保存する範囲と期間が決まっていない))。
少なくとも、次のことを含めます。

| 場所 | 中身 | 消えるとき |
|---|---|---|
| `/app/data/line_checkpoints.db` | 会話 | 本人が「すべて消す」。履歴書を消すと、その前の会話も消える |
| `/app/data/state.json`、`dip_*.db` | プロフィール(希望、経歴、履歴書の読み取り結果、保存した求人、応募) | 本人が消す、「すべて消す」 |
| `/app/data/voice-resume/` | 本番では書かない。`VOICE_RESUME_WRITE_FILES` を付けないと、音声相談の下書きと文字起こしをファイルに書き出さない(下書きは `state.json` に入る) | - |
| `/app/data/costs.jsonl` | 匿名ユーザーIDごとの費用 | 消さない(上限を逃れられないように) |
| `/app/data/quota.json` | 当日のターン数と、当日の接続元の IP | 日付(日本時間)が変わった後、最初にターンかプロフィールの書き込みがあったときに書き直されて消える(それまではファイルに残る) |
| Render のアクセスログ | 要求のパス、時刻、接続元など | Render の保存の期間による(不明。Render の設定と規約で確かめる) |

- 履歴書のファイルそのものは保存しません。氏名、住所、電話番号、メールアドレスは読み取り結果にも残しません
- 匿名ユーザーIDは Cookie だけで、aica のログインはありません

## 公開の前に残っている課題

[review-2026-10.md](../architecture/review-2026-10.md) で「公開の前」とした課題のうち、この手順では直していないものです。
公開するかどうかは、運営者が決めます。

- R6 音声相談の費用台帳と、長さに応じたターンの数え方
- R7 個人情報を保存する範囲と期間
- R8 dip の状態が 1 つの JSON ファイル
- R10 開発者画面を公開に含めるか
- R11 LiveKit のブラウザ用ライブラリの版
- R18 ターン数の上限の数の書き方、R20 音声の結果を届けられなかったときの扱い

## うまくいかないとき

- デプロイが 15 分たっても `Deploy live` にならない: 「Logs」で起動の途中のエラーを見る。`ANTHROPIC_API_KEY` が無いと、Web プロセスは
  起動の途中で止まる
- 「Logs」に `WARNING: /app/data is not a mounted disk.` と出る: ディスクがつながっていないか、Mount Path が `/app/data` でない。
  このままでは再起動で会話が消える
- 手元のブラウザ(`http://127.0.0.1`)で image を試したら、開くたびに初回訪問になる: `Secure` の Cookie は http で送り返されないので、
  `docker run -e AICA_SECURE_COOKIE=0 …` で起動する

参考にした Render の文書(2026-10-25 に確認): [Blueprint YAML Reference](https://render.com/docs/blueprint-spec)、
[Persistent Disks](https://render.com/docs/disks)、[Web Services](https://render.com/docs/web-services)、
[Health Checks](https://render.com/docs/health-checks)。
