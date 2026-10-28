# Render で公開する

aica を Render に公開する手順です。Web サービス `aica`(ディスクつき)と、音声ワーカーのサービス `aica-voice` の 2 つで動かします。
**【運営者】** と書いた手順は、Render のアカウントを持つ運営者がします。それ以外は、リポジトリにあるものだけで済みます。

## 構成

| もの | 中身 |
|---|---|
| `Dockerfile` | Python 3.12 の image。Web プロセス(`line_runtime.server`)と、その子プロセスの dip の MCP サーバーが入る。2 つのサービスで同じものを使う。`AICA_VOICE_MODELS=1` で build すると、音声のモデルも入る |
| `scripts/container_start.sh` | Web サービスの起動。`0.0.0.0:$PORT` で Web プロセスを動かす。`AICA_DIP_PORT` があれば、音声ワーカー向けの dip の MCP サーバーも動かす。`AICA_VOICE_WORKER=1` なら音声ワーカーも同じコンテナで動かす(手元の docker 向け。Render の `aica` は `0`) |
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
  (`aica` はディスクつきなので数秒止まる。`aica` で音声ワーカーを動かしていれば、その相談は切れる。例: 下の「1 つのコンテナで動かしていたサービスを分ける」)
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
| `AICA_VOICE_WORKER` | `aica` | `0`(Web のコンテナでは音声ワーカーを動かさない)。`1` は移す間と、戻すとき(下の「戻し方」) | Blueprint |
| `AICA_DIP_PORT` | `aica` | `8020`(音声ワーカー向けの dip の口) | Blueprint |
| `LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET` | `aica` | LiveKit Cloud のプロジェクト | **【運営者】** Blueprint を作るとき |
| `DEEPGRAM_API_KEY`、`CARTESIA_API_KEY` | `aica` | 文字起こしと読み上げ | **【運営者】** Blueprint を作るとき |
| `ANTHROPIC_API_KEY`、`LIVEKIT_*`、`DEEPGRAM_API_KEY`、`CARTESIA_API_KEY` | `aica-voice` | `aica` から写す。`aica` の値を変えたら、Blueprint の「Manual sync」で写し直し、`aica-voice` を「Manual Deploy」する | Blueprint(`fromService`) |
| `VOICE_RESUME_STT_PROVIDER` | 両方 | `deepgram`。変えない(`openai` にすると声そのものが OpenAI に届く。[privacy.md](../design/privacy.md) の 3.2) | Blueprint |
| `VOICE_RESUME_MAX_JOBS` | 両方 | `2`(同時に受ける相談と、温めておくプロセスの数。`aica` では `AICA_VOICE_WORKER=1` のときだけ読む) | Blueprint |
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

| PR | 中身 | 入れる前に |
|---|---|---|
| 1 つ目 | `aica-voice` を足す。`aica` の音声ワーカーも残す | - |
| 2 つ目 | `aica` の `AICA_VOICE_WORKER` を `"0"` にする | 下の 1〜5 を上から順に確かめる |

**1 つ目の PR(`aica-voice` を足す)**

`main` に入れると、Render が `aica-voice` を作り、`aica` に `AICA_DIP_PORT=8020` と `VOICE_RESUME_MAX_JOBS=2` を入れて
デプロイし直します。`aica` の音声ワーカーは残るので、2 つのワーカーが LiveKit に登録され、相談はどちらかが受けます。

`aica-voice` は、登録した時点で本番の相談を受けます。下の 3〜5 のどれかがうまくいかなければ、`aica-voice` の「Settings」で
「Suspend」にします。相談はすべて `aica` の音声ワーカーが受けます。

**2 つ目の PR の前に確かめること(1〜5)**

上から順に、Render のダッシュボードで確かめます。どれかが OK にならなければ、そこで止め、2 つ目の PR を入れません。
「Logs」の行は、ログの検索に下の文字を入れて探します。

1. Blueprint の同期: Blueprint の「Syncs」を見る
   - OK: 1 つ目の PR を入れた後の同期が、成功になっている
   - OK でないとき: 失敗した同期では何も変わっていません。失敗の理由を読みます(`fromService` の写し方を Render に断られたときも、ここで分かる)
2. `aica` のデプロイと dip の口: `aica` の「Events」と「Logs」を見る
   - OK: 1 つ目の PR より後のコミットのデプロイが `Deploy live` になっている。「Logs」に
     `WARNING: dip MCP server for the voice worker exited` が繰り返し出ていない
   - OK でないとき: `Deploy live` でなければ「Manual Deploy」→「Deploy latest commit」でします(dip の口は新しいコードで開く)。
     警告が繰り返し出るなら、`aica` の「Environment」に `AICA_INTERNAL_KEY` があるかを見ます(無ければ dip の口は開かない)
3. `aica-voice` に写した値: `aica-voice` の「Environment」を見る
   - OK: 次の値が、どれも空でない。`ANTHROPIC_API_KEY`、`LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`、
     `DEEPGRAM_API_KEY`、`CARTESIA_API_KEY`、`AICA_INTERNAL_KEY`、`AICA_DIP_PORT`(`8020`)、`AICA_WEB_HOSTPORT`(`<内部の名前>:<ポート>` の形)
   - OK でないとき: Blueprint の「Manual sync」を押してから、`aica-voice` を「Manual Deploy」します
     (`AICA_DIP_PORT` は同じ同期で `aica` に入るので、写す順番によっては空になりうる)。それでも空なら、`aica` と同じ値を手で入れます
4. `aica-voice` の起動とモデル: `aica-voice` の「Events」と「Logs」を見る
   - OK: 最新のデプロイが `Deploy live` で、その後に起動し直しが続いていない
   - OK: build のログに `Finished downloading files for livekit.plugins.turn_detector` がある(モデルが image に入った)
   - OK: 「Logs」に `音声ワーカーを起動しません`(鍵が無い、短い)も `failed to connect to livekit`(LiveKit の URL か鍵が違う)も出ていない
   - OK でないとき: モデルの行が無ければ、`aica-voice` の `AICA_VOICE_MODELS` が `1` かを見て、「Manual Deploy」で build し直します
     (Render は環境変数を build の引数として渡す)。`音声ワーカーを起動しません` なら 3 に戻ります。
     `failed to connect to livekit` が続くなら、`LIVEKIT_URL`(`wss://<project>.livekit.cloud`)と鍵を `aica` と見比べます
   - 本番の「Logs」には、LiveKit に登録したことを示す行(livekit-agents の `registered worker`)は出ません。本番(`start`)では
     SDK の INFO のログを出さないためです(`line_runtime/log_policy.py`)。登録できたことは、5 の相談で確かめます
5. 相談 1 件: トークの音声相談のボタンから相談し、2〜3 回話してから「面談を終える」で終える
   - OK: `aica-voice` の「Logs」に `dip MCP サーバーに接続: http://<内部の名前>:8020/mcp` と、
     `面談を開始: user_id=w-…`(終わりが `playbook=あり`)が出る
   - OK: 終えた後、トークに相談のまとめが届く
   - OK: `aica-voice` の「Logs」に `dip MCP サーバーに接続できません`、`トークへ部屋の始まりを知らせられません`、
     `LINE のトークへの結果送信に失敗` が出ていない
   - `面談を開始` が `aica` の「Logs」に出て、`aica-voice` に出なかったときは、`aica` の音声ワーカーが受けています。もう一度相談します
   - OK でないとき: dip につながらないなら、2 の dip の口と、3 の `AICA_DIP_PORT`、`AICA_INTERNAL_KEY` を見ます。
     まとめが届かないなら、3 の `AICA_WEB_HOSTPORT` と `AICA_INTERNAL_KEY` を見ます(鍵が違うと、結果は 401 で断られる)

**2 つ目の PR(`aica` の音声ワーカーを止める)**

`render.yaml` の `aica` の `AICA_VOICE_WORKER` を `"0"` にします。ほかの値は変えません(`VOICE_RESUME_MAX_JOBS` は戻すときのために残す)。
Auto Sync なので、マージはそのまま本番のデプロイです。`aica-voice` が相談を受けられないうちに入ると、音声相談が止まります。
なので PR は draft で出し、運営者が 1〜5 を確かめてから draft を外してマージします。

`main` に入れると、`aica` がデプロイし直され(ディスクつきなので数秒止まる)、`aica` の音声ワーカーが止まります。
相談は `aica-voice` だけが受けます。1 つ目と同じく、相談の少ない時間に入れます。マージの後に確かめることです。

6. `aica` の「Events」で、そのコミットのデプロイが `Deploy live` になる。`https://<名前>.onrender.com/healthz` が 200
   (`{"status": "ok"}`)を返す。`aica` の「Environment」で `AICA_VOICE_WORKER` が `0` になっている
7. 音声相談を 1 件する。5 と同じく `aica-voice` の「Logs」に `面談を開始` が出て、まとめがトークに届く。
   `aica` の「Logs」には `面談を開始` が出ない。うまくいかなければ、下の「戻し方」をする
8. 後片付け(しなくても動く): 数日動かして戻さないと決めたら、`aica` のディスクの `/app/data/cache/huggingface`
   (約 460MB、前のモデル)を消す。消した後に戻すと、`aica` の起動のときにモデルを取り直すので、`Deploy live` まで数分長くかかる。
   `aica` の `LIVEKIT_*`、`DEEPGRAM_API_KEY`、`CARTESIA_API_KEY` は消さない(`aica-voice` がここから写す。`LIVEKIT_*` は
   Web プロセスが部屋の接続トークンを出すのにも使う)

**戻し方**

7 で相談を受けられないとき、`aica-voice` が落ち続けるときは、`aica` の音声ワーカーを戻します。

1. `render.yaml` の `aica` の `AICA_VOICE_WORKER` を `"1"` にする PR を出し、`main` に入れる。
   `tests/test_deploy.py` の `AICA_VOICE_WORKER` の確かめも `"1"` に直す(2 つ目の PR を丸ごと revert すると、この手順の文書も戻る)
2. Auto Sync で `aica` がデプロイし直され、`aica` のコンテナでも音声ワーカーが動く。`aica` の「Logs」に
   `WARNING: could not download the voice models` が出ていないことを見る
3. 音声相談を 1 件し、`aica` か `aica-voice` の「Logs」に `面談を開始` が出て、まとめがトークに届くことを確かめる
4. 原因が `aica-voice` にあるなら、`aica-voice` の「Settings」で「Suspend」にする。相談はすべて `aica` の音声ワーカーが受ける

- 急ぐときは、PR の前に `aica` の「Environment」で `AICA_VOICE_WORKER` を `1` にして「Save Changes」してもかまいません(再起動される)。
  そのときも 1 の PR を入れます。`render.yaml` と値が違うままだと、次の同期で `0` に戻りうるためです(Render の上では確かめていない)
- `aica` を Starter(512MB)に下げた後は、`1` に戻しません。音声ワーカーは 2GB の中で動かす見積もりで、512MB には入りません。
  先に `plan` を `standard` に戻します

**移し終えた後の `aica` のメモリ**

`aica` には、Web プロセスと dip の子プロセス、音声ワーカー向けの dip の MCP サーバーだけが残ります。
上の「メモリの見積もり」の表からの見込みで、Render の上では測っていません。

| いつ | 見込み | 中身 |
|---|---|---|
| 待機 | 約 270MB | Web プロセスと dip の子プロセス(約 210MB)+ 音声ワーカー向けの dip の MCP サーバー(手元で import しただけで約 61MB RSS) |
| 山 | 約 430MB | 待機 + 履歴書の写真を縮める間(最大で約 160MB 増える。`resume_intake/extract.py` の `MAX_DECODED_IMAGE_PIXELS`) |

- 1 つ目の PR の間は、待機だけで約 1.6〜1.7GB の見込みでした。Standard(2GB)のままなら、余りは十分です
- Starter(512MB)では、山の余りは約 80MB です。相談中の dip の MCP サーバーの呼び出しと、長く動かしたときの増え方は入れていません
- Starter に下げるかは、Architecture Brief(`docs/architecture.md`)の「次にやること」の 15 で、測ってから決めます。
  案は、2 つ目の PR を入れてから数日、`aica` の「Metrics」でメモリの最大を見ることです。音声相談と履歴書の読み込みがあった日でも
  400MB ほどに収まれば下げる、という目安です(400MB は推測。512MB との間に余りを残すため)
- プランの変更も `render.yaml` の `plan` を変える PR で、マージがデプロイになります。下げた後は、上の「戻し方」で `1` に戻せません

**確かめていないこと、気をつけること**

- Render の上で `aica-voice` が `aica` の dip と結果の口につながるところは、まだ確かめていません(5 で分かる)。手元では、
  docker のネットワークで別のコンテナから dip の口につながること、鍵が無いと断られることを確かめました
- `fromService` の `envVarKey` で、ダッシュボードで入れた値(`sync: false`)を写せるかは、まだ確かめていません。
  Render の文書には、ほかのサービスの環境変数を写せるとだけあります。写せなければ `aica-voice` は LiveKit に登録できず、
  相談は `aica` の音声ワーカーが受け続けます(3 で分かる。そのときは `aica-voice` の「Environment」に手で入れる)
- 本番のログには LiveKit への登録の行が出ないので、`aica-voice` の登録が切れても、ログだけでは気づけません
  (Architecture Brief の「次にやること」の 10)
- 相談中のプロセスが 400MB に収まるかは、まだ測っていません。5 の相談の間に、`aica-voice` の SSH で
  `/sys/fs/cgroup/memory.current` と各プロセスの `/proc/<pid>/smaps_rollup` を見て決め直します
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
