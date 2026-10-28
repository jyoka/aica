# Render で公開する

aica を Render に公開する手順です。Web サービス `aica`(ディスクつき)と、音声ワーカーのサービス `aica-voice` の 2 つで動かします。
**【運営者】** と書いた手順は、Render のアカウントを持つ運営者がします。それ以外は、リポジトリにあるものだけで済みます。

## 構成

| もの | 中身 |
|---|---|
| `Dockerfile` | Python 3.12 の image。Web プロセス(`line_runtime.server`)と、その子プロセスの dip の MCP サーバーが入る。2 つのサービスで同じものを使う。`AICA_VOICE_MODELS=1` で build すると、音声のモデルも入る |
| `scripts/container_start.sh` | Web サービスの起動。`0.0.0.0:$PORT` で Web プロセスを動かす。`AICA_DIP_PORT` があれば、音声ワーカー向けの dip の MCP サーバーも動かす。`AICA_VOICE_WORKER=1` なら音声ワーカーも同じコンテナで動かす(手元の docker と、Render で別サービスに移す間) |
| `scripts/voice_worker_start.sh` | 音声ワーカーのサービスの起動(`render.yaml` の `dockerCommand`)。Web サービスの private network の名前から、結果を返す先と dip の URL を作る |
| `scripts/check_image_certs.py` | image の CA 証明書の確かめ。certifi と ca-certificates の版(実 API は使わない。下の「CA 証明書の版を確かめる」) |
| `render.yaml` | Render の Blueprint。Web サービス 1 つ、永続ディスク 1 つ、ヘルスチェック、音声ワーカー 1 つ、環境変数 |
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

## CA 証明書の版を確かめる

外向きの HTTPS が信頼する CA の束は 2 つあります。どちらも build のたびに新しい版を入れます。

| 束 | 使うもの | 入れ方 |
|---|---|---|
| certifi | httpx、openai、anthropic(OpenAI、Anthropic、LINE の呼び出し) | `requirements.txt` の `certifi>=2026.10.19` |
| OS の `/etc/ssl/certs`(Debian の `ca-certificates`) | aiohttp を使うもの(LiveKit など) | `Dockerfile` の `apt-get install ca-certificates` |

eMudhra の中間証明書の秘密鍵の漏洩([Render の通告](https://render.com/docs/advisory-emudhra)、2026-10-27)で、
certifi は 2026.10.19 以上が要ります。image の版は次で確かめます。API キーは要りません。

```bash
./.venv/bin/python -m scripts.check_image_certs              # build → certifi と ca-certificates の版を確かめる
./.venv/bin/python -m scripts.check_image_certs --no-build --probe   # 作ってある image(aica:certs)。通告の試験用のホストにもつなぐ
```

- ca-certificates は、image の中で `apt-get update` をして Debian の今の候補の版と比べます。古いキャッシュの層で build すると NG になるので、
  `docker build --no-cache` で作り直します
- `--probe` の試験用のホストは、鎖の根(emSign ECC Root CA - G3)がどちらの束にも無く、古い certifi(2026.10.18)でも
  つながりません。版が古いことはこれでは見分けられないので、版の確かめが本体です

2026-10-27 に手元(OrbStack)で通しました。certifi 2026.10.23、ca-certificates 20250419+deb13u1(Debian の候補と同じ)でした。

## Render に公開する

### 1. Blueprint からサービスを作る **【運営者】**

1. [Render のダッシュボード](https://dashboard.render.com/) で「New」→「Blueprint」を選び、GitHub の `jyoka/aica` をつなぐ。
   ブランチは `main`
2. `render.yaml` が読まれ、Web サービス `aica`(Standard、Singapore)とディスク `aica-data`(2GB、`/app/data`)、
   音声ワーカー `aica-voice`(Standard、Singapore)が出る
3. `ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`、`DEEPGRAM_API_KEY`、`CARTESIA_API_KEY` の値を入れる
   (`aica` の `sync: false` なので、ここで聞かれる。リポジトリには書かない)。`aica-voice` は `aica` から写すので、入れない
   (ここで入れた値がそのまま写るかは、まだ確かめていない。作った後に `aica-voice` の「Environment」を見る)
4. 「Deploy Blueprint」でサービスを作る(前の画面では「Apply」だった)。最初の build に数分かかる。「Events」で `Deploy live` になるまで待つ

- `autoDeployTrigger: "off"` なので、`render.yaml` を変えない push では、自動ではデプロイしません。更新は「Manual Deploy」→「Deploy latest commit」でします
- Blueprint の Auto Sync は有効のままにします。`render.yaml` を変えて `main` に入れると、Render がサービスをその内容に合わせ、
  変わったサービスをデプロイし直します(Render の文書 [Render Blueprints](https://render.com/docs/infrastructure-as-code))。
  つまり、`render.yaml` を変える PR のマージはデプロイです。入れた時点で本番が動き続ける形にし、相談の少ない時間に入れます
  (`aica` はディスクつきなので数秒止まり、`aica` の音声ワーカーの相談は切れる。例: 下の「1 つのコンテナで動かしていたサービスを分ける」)
- Blueprint を更新するときは、`sync: false` の値を聞かれません。新しいサービスの鍵は、`fromService` で `aica` から写します
- `AICA_TRUST_PROXY=1` と `AICA_INTERNAL_KEY`(乱数)は Blueprint が入れます。`AICA_INTERNAL_KEY` は Blueprint が `aica` から
  `aica-voice` に写すので、そろえる手間はありません。音声相談の設定(`LIVEKIT_*`)があるのに `AICA_INTERNAL_KEY` が無い、
  短い(32 文字未満)ときは、Web プロセスも音声ワーカーも起動で止まります。ダッシュボードで作るときは、忘れずに長い乱数を入れます
- Blueprint を使わずにダッシュボードで作るときは、下の「環境変数」の表と、Health Check Path `/healthz`、
  ディスクの Mount Path `/app/data` を同じに設定します。`aica` の Docker Command は空のままにします(`Dockerfile` の CMD を使う)。
  `aica-voice` はバックグラウンドワーカーで作り、Docker Command を `scripts/voice_worker_start.sh` にし、`AICA_VOICE_MODELS=1` と、
  `aica` と同じ鍵を入れます

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

### 6. 通知を設定して確かめる **【運営者】**

デプロイの失敗、`aica` の unhealthy、ディスクの 80% 超えなどの失敗の通知を、メールで受けます。
設定はワークスペースに 1 つ置き、`aica` と `aica-voice` はそれに従わせます。決めた理由と現状は [Architecture Brief の関門 6](../architecture.md#6-可観測性) にあります。

| 項目 | 決めたこと | 理由 |
|---|---|---|
| 送り先(Notification Destination) | メールだけ。宛先は、運営者が Render にログインしているアカウント | 失敗の通知は、すべてメールで届く。Slack でしか届かないのは、デプロイの成功、unhealthy からの回復、one-off job の失敗で、気づくためには要らない。通知のために Slack を用意しなくて済む |
| 段階(Default Service Notifications) | ワークスペースの既定を「Only failure notifications」 | 「All notifications」にしても、メールで増えるのは「ディスクの使用が 80% を下回った」だけ |
| サービスごとの上書き | しない。`aica` と `aica-voice` は「Use workspace default」 | 既定を 1 か所で変えれば両方に効く。上書きがあると、既定を変えてもそのサービスには効かない |
| Preview Notifications | 変えない | aica はプレビューを使わない(`render.yaml` に `previews` がない)。Pro 以上のワークスペースだけの設定 |

「Only failure notifications」で、メールで届く通知です(Render の文書 [Email and Slack Notifications](https://render.com/docs/notifications) の表から)。

| 通知 | `aica` | `aica-voice` | 確かめ方 |
|---|---|---|---|
| build かデプロイが失敗した | 届く | 届く | 試せる(下の「届くことを確かめる」) |
| 動いているサービスが unhealthy になった | 届く(`/healthz`) | 未確認。Render のヘルスチェックは Web サービスと private service だけで、バックグラウンドワーカーにはない | 試せない |
| ディスクの使用が 80% を超えた | 届く(`aica-data` 2GB) | 対象外(ディスクがない) | 試せない |
| Blueprint の同期が失敗した(`render.yaml` を変えた PR のマージなど) | 届く(Blueprint ごと) | 同じ | 試せない |
| 24 時間、起動して healthy になることに失敗し続け、Render がサービスを Suspend した | 届く | 届く | 試せない |
| Docker の image の取得が失敗した | 対象外(登録済みの image ではなく、Render が build する) | 対象外 | - |

届かないものです。

- メモリ不足(`Out of memory`)、`Instance Failed`、インスタンスの起動し直し。Render の通知の一覧にありません。「Events」には出ます
  (毎日見ます。下の「7. メモリ不足とインスタンスの失敗を毎日見る」)
- デプロイの成功と、unhealthy からの回復(Slack だけ)。回復したかは「Events」で見ます
- `aica-voice` が固まったこと(ヘルスチェックがない。Brief の「次にやること」の 10)

Render の文書に書いておらず、未確認のことです。下の「届くことを確かめる」で分かるものは、そこで控えます。

- メールがワークスペースの誰に届くか(メンバー全員か、持ち主だけか)
- 送り元のアドレス
- `aica-voice` で unhealthy の通知が出るか

**設定する**

1. [Render のダッシュボード](https://dashboard.render.com/) のワークスペースのホームで、左の「Integrations」→「Notifications」を開く
2. 「Notification Destination」を「Email」にする。Slack はつながない
3. 「Default Service Notifications」を「Only failure notifications」にする(「None」だと何も届かない)
4. 同じ画面の「Notification Overrides」に `aica` と `aica-voice` が無いことを見る。あれば、そのサービスの「Settings」→「Notifications」で、
   すべての項目を「Use workspace default」に戻す
5. `aica` と `aica-voice` の「Settings」→「Notifications」が「Use workspace default」になっていることを見る

**届くことを確かめる**

本番の `aica` と `aica-voice` には触れず、build が必ず失敗する試しのサービスを作って、デプロイの失敗の通知を出します。
新しいサービスもワークスペースの既定に従うので、送り先と段階の設定を確かめられます。

1. 「New」→「Web Service」→「Public Git Repository」で、`https://github.com/render-examples/flask-hello-world`(Render の例)を入れる
2. 名前は `notify-test`、Language は「Python 3」、Build Command は `exit 1`、Instance Type は「Free」にして作る
   (Free が選べなければ、いちばん安いものにし、確かめたらすぐ消す)
3. build が失敗し、デプロイの失敗のメールが届くことを見る。宛先と送り元のアドレスを控える。
   迷惑メールに入っていたら、送り元を受信箱に残すフィルタを作る。携帯でメールの通知が出ることも見る
4. `notify-test` の「Settings」で「Delete Web Service」を押して消す
5. 設定した日、届いた宛先、送り元を、Brief の関門 6 に書く(「設定待ち」を外す)

届かないときは、迷惑メール、手順 2 と 3 の設定、`notify-test` の「Settings」→「Notifications」の順に見ます。

ほかの通知は、本番を止めるかディスクを埋めないと出ないので、試しません。実際に起きたときに、「Events」と照らして届いたかを見ます。

- unhealthy: `aica` の `/healthz` が失敗し続けたとき
- ディスクの 80% 超え: 今の使用量は、`aica` の「Shell」で `df -h /app/data` を実行して見る
- Blueprint の同期の失敗、Suspend: 起きたときに見る

### 7. メモリ不足とインスタンスの失敗を毎日見る **【運営者】**

メモリ不足(`Out of memory`)とインスタンスの失敗(`Instance Failed`)は、Render の通知では届きません。Render が起動し直すので、
回数が増えていても気づけません。毎日 1 回、`aica` と `aica-voice` の回数を数えます。
届く通知にしなかった理由は [Architecture Brief の関門 6](../architecture.md#6-可観測性) にあります。

**準備(1 回だけ)**

1. [Render のダッシュボード](https://dashboard.render.com/) の右上のアカウントから「Account Settings」→「API Keys」で、鍵を作る。
   名前は `aica-events` など。鍵は作ったときに 1 回だけ表示される
2. 鍵を手元の Mac のキーチェーンに入れる。最後の `-w` で、鍵を聞かれるので貼る(シェルの履歴に残さないため)

   ```bash
   security add-generic-password -a "$USER" -s render-api-key -w
   ```

- 鍵は、運営者が属するすべてのワークスペースを読み書きでき、環境変数(API キー)も読めます。`.env`、CI、Render の環境変数には入れません
- Mac をなくしたとき、鍵が漏れたかもしれないときは、「API Keys」でその鍵を消し、作り直します

**毎日見る**

1. リポジトリ直下で動かす。Render の API を読むだけで、何も変えない

   ```bash
   RENDER_API_KEY=$(security find-generic-password -s render-api-key -w) ./.venv/bin/python -m scripts.render_events
   ```

   次のように出ます。回数は「直近 24 時間 / 7 日」で、`*` は直近 24 時間のものです。

   ```text
   2026-10-28 09:00 時点(日本時間)。直近 24 時間 / 7 日
     aica: メモリ不足(Out of memory) 1 / 1、インスタンスの失敗(Instance Failed) 0 / 0
     aica-voice: メモリ不足(Out of memory) 0 / 0、インスタンスの失敗(Instance Failed) 0 / 0
     * 10-28 06:00 aica メモリ不足(Out of memory) 上限 2Gi evt-…
   判定: look(* の日時を記録し、その時刻の Metrics と Logs を見る)
   ```

2. 最後の行の判定のとおりにする

| 判定 | 回数(サービスごと) | すること |
|---|---|---|
| ok | 直近 24 時間に 0 回 | 何もしない |
| look | 直近 24 時間に 1 回以上で、act に当たらない | `*` の日時、サービス、種類を控える。そのサービスの「Metrics」で、その時刻の前後のメモリを見る。「Logs」で、落ちる直前のログを見る。メモリ不足でない失敗は、終了コードと直前のエラーを控える |
| act | 直近 24 時間にメモリ不足が 2 回以上、7 日で 3 回以上、またはメモリ不足でない失敗が直近 24 時間に 2 回以上 | 増えています。その日のうちに、下の「act のとき」をする |

- 週末などで空けたときは、`--hours 72` のように、空けた時間を渡します。7 日より前の分は数えません(API が出来事を何日残すかは未確認)
- 公開 URL の `/healthz` が答えない、サービスが「Live」でない(落ちたまま戻っていない)ときは、判定によらず、すぐ対応します
  (runbook は Brief の「次にやること」の 4 で書く。まだない)
- 急にメモリが増えて落ちたときは、「Metrics」に山が出ないことがあります(Render の文書 [Troubleshooting Your Deploy](https://render.com/docs/troubleshooting-deploys))

**act のとき**

1. 「Metrics」で、メモリの増え方を見る。待機だけで上限近くにいる、相談のたびに山になる、時間とともに増え続ける(漏れ)のどれかを控える
2. `aica` で、音声ワーカーを移している間(`AICA_VOICE_WORKER=1`)なら、上の「1 つのコンテナで動かしていたサービスを分ける」の 2 つ目の PR を先にする
3. 移し終えた後の `aica`、または `aica-voice` なら、Brief の関門 3 の見直しのきっかけです。日時、回数、1 の増え方を書いて、カードにする
4. メモリ不足でない失敗が続くときは、「Logs」の終了コードと直前のエラーを書いて、カードにする

**鍵がないとき、API が答えないとき**

コマンドの終了コードが 2(`NG: …`)のときは、ダッシュボードで同じことを見ます。

1. `aica` と `aica-voice` の「Events」を開き、直近 7 日の `Out of memory` と `Instance Failed` を数える
2. 上の表の回数の決まりで判定する

- 終了コードは、ok が 0、look と act が 1、鍵がない・API が答えないが 2 です。毎日の実行を launchd などで自動にするときに使います
- ダッシュボードの `Out of memory` と `Instance Failed` は、API ではどちらも `server_failed` の出来事で、メモリ不足のときだけ
  理由に `oomKilled` が付く、と読んでいます(Render の API の型から。本番の出来事では確かめていない)。
  初めて動かしたときに、「Events」と数が合うかを見て、Brief の関門 6 に書きます

## 環境変数

Render の「Environment」に置きます。`.env` はコンテナに入れません。表に無いものは README の「設定」の既定のままです。

| 環境変数 | 値 | 入れる者 | 用途 |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic の API キー | **【運営者】** Blueprint を作るとき | エージェント、ゲームの AI、履歴書の読み取り、音声相談(既定の `LINE_SIM_PROVIDER=anthropic`) |
| `OPENAI_API_KEY` | OpenAI の API キー | **【運営者】** Blueprint を作るとき | 求人の意味検索の埋め込み(無ければ部分一致の検索に戻る) |
| `AICA_TRUST_PROXY` | `1` | Blueprint | IP を `X-Forwarded-For` の最後の値から取る(Render のプロキシの後ろ) |
| `AICA_INTERNAL_KEY` | 乱数 | Blueprint(`generateValue`) | 音声ワーカーが結果を返すときと、dip の口につなぐときの共有鍵 |
| `AICA_SECURE_COOKIE` | `1` | `Dockerfile` | 匿名ユーザーIDの Cookie に `Secure` を付ける |
| `PORT` | `10000` | Render | `scripts/container_start.sh` が `LINE_SIM_PORT` にする |

音声相談のものです(下の「音声相談」)。Blueprint が入れるか、Blueprint を作るときに聞かれます。

| 環境変数 | サービス | 値 | 入れる者 |
|---|---|---|---|
| `AICA_VOICE_WORKER` | `aica` | 移す間は `1`(Web のコンテナでも音声ワーカーを動かす)。移し終えたら `0` | Blueprint |
| `AICA_DIP_PORT` | `aica` | `8020`(音声ワーカー向けの dip の口) | Blueprint |
| `LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET` | `aica` | LiveKit Cloud のプロジェクト | **【運営者】** Blueprint を作るとき |
| `DEEPGRAM_API_KEY`、`CARTESIA_API_KEY` | `aica` | 文字起こしと読み上げ | **【運営者】** Blueprint を作るとき |
| `ANTHROPIC_API_KEY`、`LIVEKIT_*`、`DEEPGRAM_API_KEY`、`CARTESIA_API_KEY` | `aica-voice` | `aica` から写す。`aica` の値を変えたら、Blueprint の「Manual sync」で写し直し、`aica-voice` を「Manual Deploy」する | Blueprint(`fromService`) |
| `VOICE_RESUME_STT_PROVIDER` | 両方 | `deepgram`。変えない(`openai` にすると声そのものが OpenAI に届く。[privacy.md](../design/privacy.md) の 3.2) | Blueprint |
| `VOICE_RESUME_MAX_JOBS` | 両方 | `2`(同時に受ける相談と、温めておくプロセスの数) | Blueprint |
| `VOICE_RESUME_JOB_MEMORY_LIMIT_MB` | `aica-voice` | `400`(相談 1 件のプロセスの上限。`aica` は既定の 2000) | Blueprint |
| `AICA_VOICE_MODELS` | `aica-voice` | `1`(build のときにモデルを image に入れる) | Blueprint |
| `AICA_WEB_HOSTPORT`、`AICA_DIP_PORT`、`AICA_INTERNAL_KEY` | `aica-voice` | `aica` から写す | Blueprint(`fromService`) |

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
- dip の MCP サーバー(Web プロセスの子プロセスと、音声ワーカー向けの HTTP の口)は、Web のコンテナで親の環境変数を受け取ります。
  音声ワーカーは別サービスで、`render.yaml` に書いた環境変数だけを持ちます(`OPENAI_API_KEY` は持たない)
- 音声ワーカー向けの dip の口(`AICA_DIP_PORT`)は Render の private network にだけ開き、`AICA_INTERNAL_KEY` を付けない要求には
  401 を返します。鍵が無ければ、その口は開きません
- `/dev`(開発者画面)も公開 URL で開けます。キーは出ませんが、誰でもシナリオを動かせて費用がかかります
  ([review-2026-10.md の R10](../architecture/review-2026-10.md#r10-本番の-web-サーバーが評価のパッケージと開発者画面を抱えている))

## 音声相談

音声ワーカーは、Web サービスと別のサービス(`aica-voice`、バックグラウンドワーカー)で動かします。
同じコンテナに置くと、相談の数だけ増えるメモリで Web も一緒に落ちるためです(2026-10-27 に、待機中だけで 2GB のうち 1.83GB を使っていた)。

- 音声ワーカーは、Render の private network で Web サービスにつなぎます。相談の結果は `aica` の `/api/voice/result` へ、
  dip(本人の要約、希望の保存、結果の記録)は `aica` のコンテナの dip の MCP サーバー(`AICA_DIP_PORT`)へ送ります。
  dip の状態は `aica` のディスクにだけあります([review-2026-10.md の R9](../architecture/review-2026-10.md#r9-音声ワーカーがweb-プロセスと同じマシンにいる前提で作られている))
- モデル(VAD、turn detector。約 460MB)は build のときに image に入れます。`aica-voice` にディスクは要らず、起動のたびに取りません
- LiveKit の環境変数が無ければ、音声のボタンを押すと「準備ができていません」(503)と出て、ターンは使いません。
  LiveKit の環境変数は `aica` に入れ、`aica-voice` は Blueprint が写します(`aica-voice` に無いと、部屋に入っても答えるワーカーがいません)
- `aica-voice` が落ちても、Render が起動し直します。`aica` は止まりません
- `aica` を入れ替える間(ディスクつきなので数秒止まる)の dip の呼び出しは、15 秒で諦めます。相談は、その道具なしで続きます
  (`voice_resume/dip.py` の `CALL_TIMEOUT`)

### メモリの見積もり

2026-10-27 に、1 つのコンテナで動かしていたときに測った値(PSS)です。

| プロセス | メモリ |
|---|---|
| turn detector の推論のプロセス | 約 820MB |
| 音声ワーカーの本体と forkserver | 約 300MB |
| 温めておく相談のプロセス(1 つ) | 約 124MB |
| Web プロセスと dip の子プロセス | 約 210MB |
| 音声ワーカー向けの dip の MCP サーバー(`AICA_DIP_PORT`) | 測っていない(手元で import しただけで約 61MB RSS) |

`aica-voice`(2GB)は、待機中は約 1.4GB(推論 + 本体 + 待機 2 つ)の見込みです。相談中は待機のプロセスが相談のプロセスになり、
1 つ `VOICE_RESUME_JOB_MEMORY_LIMIT_MB`(400MB)まで使えます。2 件で約 1.9GB です。

- `VOICE_RESUME_MAX_JOBS` は、同時に受ける相談と、温めておくプロセスの数の両方です。livekit-agents は、相談が 0 件の間、
  受付中の 1 件の重さを「上限 / 温めておく数」と見積もるので、2 つを別にすると、最初の相談が始まるまでの数秒に来た 2 人目を断ります
  (`voice_resume.agent.worker_options`)
- 相談のプロセスが 400MB を超えると、livekit-agents がその相談だけを止めます(ほかの相談と Web は止まらない)。
  上限が見るのは相談のプロセスだけです。`aica` の音声ワーカーでは、相談ごとの dip の子プロセスは数えず、コンテナ全体が 2GB を
  超えると Web も一緒に止まります(下の 1 つ目の PR の間)

### 1 つのコンテナで動かしていたサービスを分ける **【運営者】**

前の形(`aica` のコンテナだけで音声ワーカーを動かす)から移すときの手順です。Auto Sync は有効のままにし、
2 つの PR に分けて `main` に入れます。どちらも、入れた時点で音声相談が動き続ける形です。

**1 つ目の PR(`aica-voice` を足す)**

`main` に入れると、Render が `aica-voice` を作り、`aica` に `AICA_DIP_PORT=8020` と `VOICE_RESUME_MAX_JOBS=2` を入れて
デプロイし直します。`aica` の音声ワーカーは残るので、2 つのワーカーが LiveKit に登録され、相談はどちらかが受けます。

`aica-voice` は、登録した時点で本番の相談を受けます。なので、相談の少ない時間にマージし、すぐに 1〜5 をします。
3〜5 のどれかがうまくいかなければ、`aica-voice` の「Settings」で「Suspend」にします。相談はすべて `aica` の音声ワーカーが受けます。

1. Blueprint の「Syncs」で、同期が成功したことを見る(失敗していれば何も変わっていない。`fromService` の写し方を Render に断られたときも、ここで分かる)
2. `aica` の「Events」で、入れたコミットのデプロイが `Deploy live` になったことを見る。なっていなければ「Manual Deploy」→
   「Deploy latest commit」でする(dip の口は新しいコードで開く)。「Logs」に `WARNING: dip MCP server` が繰り返し出ないことも見る
3. `aica-voice` の「Environment」に、`aica` から写した値(`ANTHROPIC_API_KEY`、`LIVEKIT_*`、`DEEPGRAM_API_KEY`、`CARTESIA_API_KEY`、
   `AICA_INTERNAL_KEY`、`AICA_WEB_HOSTPORT`、`AICA_DIP_PORT`)が入っていることを見る。空のものがあれば、Blueprint の「Manual sync」を
   押してから `aica-voice` を「Manual Deploy」する(`AICA_DIP_PORT` は同じ同期で `aica` に入るので、写す順番によっては空になりうる)
4. `aica-voice` の「Logs」で、ワーカーが LiveKit に登録されたことを見る。build のログにモデルの取得(`download-files`)が
   出ていることも見る(Render は環境変数を build の引数として渡す。出ていなければ image にモデルが無いので、`AICA_VOICE_MODELS=1` を確かめる)
5. トークの音声相談のボタンから相談し、`aica-voice` の「Logs」に相談が出て、結果がトークに届くことを確かめる。
   `aica` の音声ワーカーが受けたときは、もう一度相談する

**2 つ目の PR(`aica` の音声ワーカーを止める)**

1 つ目の 5 まで確かめてから出します。`render.yaml` の `aica` の `AICA_VOICE_WORKER` を `"0"` にします
(合わせて、`tests/test_deploy.py` と、この文書、README、HLD の「移す間は 1」の書き方を直す)。
`main` に入れると、`aica` がデプロイし直され、音声ワーカーが止まります。相談は `aica-voice` だけが受けます。1 つ目と同じく、相談の少ない時間に入れます。

6. もう一度相談し、結果がトークに届くことを確かめる
7. 後片付け(しなくても動く): `aica` のディスクの `/app/data/cache/huggingface`(約 460MB、前のモデル)を消す。
   `aica` の `DEEPGRAM_API_KEY`、`CARTESIA_API_KEY` は消さない(`aica-voice` がここから写す)

確かめていないこと、気をつけることです。

- Render の上で `aica-voice` が `aica` の dip と結果の口につながるところは、まだ確かめていません。手元では、docker のネットワークで
  別のコンテナから dip の口につながること、鍵が無いと断られることを確かめました
- `fromService` の `envVarKey` で、ダッシュボードで入れた値(`sync: false`)を写せるかは、まだ確かめていません。
  Render の文書には、ほかのサービスの環境変数を写せるとだけあります。写せなければ `aica-voice` は LiveKit に登録できず、
  相談は `aica` の音声ワーカーが受け続けます(手順 3 で分かる。そのときは `aica-voice` の「Environment」に手で入れる)
- 1 つ目の PR の間、`aica` は待機だけで約 1.6〜1.7GB(Web + 推論 + 本体 + 待機 2 つ + 音声ワーカー向けの dip の口。上の表からの見積もり)
  になる見込みです。前の形(待機 4 つ。待機だけで 1.83GB)よりは少ないですが、`aica` の音声ワーカーが 2 件を同時に受けると
  2GB を超えうるので、2 つ目の PR までの間は短くします
- 相談中のプロセスが 400MB に収まるかは、まだ測っていません。手順 5 の相談の間に、`aica-voice` の SSH で
  `/sys/fs/cgroup/memory.current` と各プロセスの `/proc/<pid>/smaps_rollup` を見て決め直します
- `aica` は、音声ワーカーが抜けたので Starter(512MB)で足りる見込みです。確かめるまで Standard のままにしています
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
通知の手順は、2026-10-28 に [Email and Slack Notifications](https://render.com/docs/notifications) と
[Health Checks](https://render.com/docs/health-checks) を確認して書きました。
メモリ不足の点検は、2026-10-28 に [Webhooks](https://render.com/docs/webhooks)、[The Render API](https://render.com/docs/api)、
[List events](https://api-docs.render.com/reference/list-events) を確認して書きました。
