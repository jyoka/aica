# 設定

環境変数、`.env`、保存する期間、ターン数の上限です。動かし方は [usage.md](usage.md) にあります。

API キーなどの秘密は、リポジトリ直下の `.env` に書きます(すでにある環境変数は上書きしません)。

**`.env` で効くのは、下の表で「`.env` 可」としたものだけです。** ほかは起動のときに読むので、コマンドの前に環境変数として渡します
(例: `SIM_NOW=2026-10-01T09:00:00+09:00 ./.venv/bin/python -m line_runtime.server`)。理由は
[review-2026-10.md の R21](architecture/review-2026-10.md#r21-設定の一部が-env-では効かない)にあります。

保存する期間は環境変数ではなく、コードの定数です(数の理由は [docs/design/privacy.md](design/privacy.md) の 3.3)。
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
| `ANTHROPIC_API_KEY` | なし(必須) | エージェント、ゲームの AI、評価の模擬ユーザーとジャッジ、履歴書の読み取り、音声アドバイザー、下書き、調べもの(既定の `anthropic` のとき)。無いと Web プロセスは起動の途中で止まる | 可 |
| `OPENAI_API_KEY` | なし(意味検索に要る) | 求人の意味検索の埋め込み(常に OpenAI)。無いと部分一致の検索に戻る。プロバイダを `openai` にしたところ(エージェント、履歴書、音声相談)でも使う | 可 |
| `LINE_SIM_PROVIDER` | `anthropic` | LLM のプロバイダ(`anthropic`、`openai`)。エージェント、ゲームの AI、模擬ユーザー、ジャッジのモデルが変わる | 不可 |
| `LINE_SIM_MODEL` | `claude-sonnet-5-5`(openai なら `gpt-6-sol`) | エージェントとゲームのまとめのモデル | 不可 |
| `LINE_SIM_EFFORT` | `low` | エージェントの思考の深さ | 不可 |
| `AICA_PROGRESS_UI` | `flex` | アルバイト・パートの相談(`BAITO_HARNESS_VERSION=v4`)で、プロフィールが育つのを見せる部品の出し方。`flex` は帯・プロフィールカード・完成を LINE の Flex で、`text` は Flex を切って吹き出しの文で、`no_band` は帯だけ切る。Web のトークはいつも文。ターンごとに読む | 可 |
| `CAREER_HARNESS_VERSION` | なし | 正社員のチャットの相談を dip のハーネスで進める版。`c1`(ADR-0010)か、`c2`(会話の層に載せ替えていく版。ADR-0011、[設計メモ](design/conversation-layer.md)。作っている途中で、試すときだけ)。無ければ playbook の相談。新しく始める相談のときに読み、始めた相談は始めた版のまま進む | 可 |
| `CAREER_HARNESS_FORM` | `C` | 正社員の相談(`CAREER_HARNESS_VERSION=c1` か `c2`)の形。`B` はゲームの仕組みなし、`C` は場面の二択と進み具合の 1 行あり、`D` は C に LINE の Flex(「始める」の 1 枚、職歴カード、帯、プロフィールカード、まとめ、全部の完成)を足す。相談を始めたときに読み、終わるまで変えない | 可 |
| `CAREER_UI_BAND` | `on` | 正社員の相談の形 D で、帯を付けるか。`off` で帯だけ切る(帯を切った D と比べるため)。相談を始めたときに読む | 可 |
| `LINE_SIM_PORT` | `8030` | Web サーバーのポート(コンテナでは `PORT`、既定 10000) | 不可 |
| `LINE_SIM_HOST` | `127.0.0.1` | Web サーバーが待ち受けるアドレス(コンテナでは `0.0.0.0`) | 不可 |
| `AICA_SECURE_COOKIE` | なし(コンテナの image では `1`) | `1` のとき、匿名ユーザーIDの Cookie に Secure を付ける。https で公開するときに使う。http で開くと Cookie が送り返されない | 可 |
| `LINE_SIM_DATA_DIR` | `data/` | 会話、費用台帳、ターン数の置き場 | 不可 |
| `DIP_MCP_URL` | なし(stdio の子プロセス) | 別プロセスの dip の MCP サーバーの URL。Web プロセスと音声ワーカーが読む。音声ワーカーは `AICA_INTERNAL_KEY` を付けて送る(Render では `scripts/voice_worker_start.sh` が作る) | 不可 |
| `DIP_STATE_PATH` | `data/state.json` | dip の状態。行動の元データと要約(`dip_events.db`、`dip_summaries.db`)、ログインと連携の表(`dip_accounts.db`)も同じディレクトリに置く | 不可 |
| `SIM_NOW` | `2026-09-23T12:00:00+09:00` | シナリオユーザーと評価の dip の時計(固定)。本番のユーザーは実際の時刻 | 不可 |
| `RESUME_INTAKE_PROVIDER` | `LINE_SIM_PROVIDER` の値(既定 `anthropic`) | 履歴書の読み取りのプロバイダ(`anthropic`、`openai`) | 不可 |
| `RESUME_INTAKE_MODEL` | `claude-sonnet-5-5`(openai なら `gpt-6-sol`) | 履歴書の読み取りのモデル | 不可 |
| `RESUME_INTAKE_EFFORT` | `none` | 履歴書の読み取りの思考の深さ(Anthropic だけ。`none` は思考を切る。書いてあることを写すだけなので) | 不可 |

ターン数の上限です。`0` 以下にすると、その上限はありません。既定値の理由は [LLD の 5.3](architecture/lld.md#53-費用台帳とターン数) にあります。

| 環境変数 | 既定 | 上限 | `.env` |
|---|---|---|---|
| `LINE_SIM_DAILY_TURN_LIMIT` | 50 | 匿名ユーザーIDごとのターン数 | 可 |
| `AICA_DAILY_TURN_LIMIT_PER_IP` | 150 | 同じ IP からのターン数の合計 | 可 |
| `AICA_DAILY_NEW_IDS_PER_IP` | 20 | 同じ IP から 1 日に使える匿名ユーザーIDの数 | 可 |
| `AICA_DAILY_TURN_LIMIT_TOTAL` | 1000 | 全体のターン数の合計 | 可 |
| `AICA_DAILY_NEW_IDS_TOTAL` | 300 | 全体で 1 日に使える匿名ユーザーIDの数 | 可 |
| `AICA_TRUST_PROXY` | なし | `1` のときだけ、IP を `X-Forwarded-For` の最後の値から取る。プロキシの後ろに置くとき(Render、Tailscale Funnel、cloudflared のトンネル)に使う。無いと全員がプロキシの IP として数えられる | 可 |

レジュメ・アーケードのゲームの AI の上限です([HLD の 4.11](architecture/hld.md#411-レジュメアーケード))。AI つきで始めるとき、ターンを 1 つ数えます。

| 環境変数 | 既定 | 上限と用途 | `.env` |
|---|---|---|---|
| `AICA_ARCADE_AI` | `1` | `0` にするとゲームの AI を止める(すべて簡易分析。保存はできる) | 可 |
| `AICA_ARCADE_DAILY_AI_PLAYS` | 3 | 匿名ユーザーIDごとに、1 日に AI つきで始められる遊びの数。超えたら AI なしで始める | 可 |
| `AICA_ARCADE_REACTIONS_PER_PLAY` | 50 | 1 回の遊びのゲームの中の反応の数。最後のまとめの 2 回は別に取っておく | 可 |
| `AICA_ARCADE_QUICK_MODEL` | `claude-haiku-5-5`(openai なら `gpt-6-luna`) | ゲームの中の反応のモデル。最後のまとめは `LINE_SIM_MODEL` | 可 |

LINE 公式アカウントで使うものです。LINE で試さないなら要りません。

| 環境変数 | 既定 | 用途 | `.env` |
|---|---|---|---|
| `LINE_CHANNEL_SECRET` | なし | チャネルシークレット。webhook の署名(`X-Line-Signature`)を確かめる。`LINE_CHANNEL_ACCESS_TOKEN` と両方あるときだけ `POST /line/webhook` を開く(無ければ 404) | 可 |
| `LINE_CHANNEL_ACCESS_TOKEN` | なし | チャネルアクセストークン(長期)。入力中の表示、返信、push、アカウント連携の linkToken の発行に使う | 可 |
| `AICA_ID_KEY` | なし(LINE の 2 つか LIFF の 2 つがあるときは必須) | LINE のユーザーID(`l-…`)を作る HMAC-SHA256 の鍵。LIFF の本人の Cookie の署名の鍵も、ここから作る。32 バイト以上(例 `python -c "import secrets; print(secrets.token_hex(32))"`)。LINE の 2 つか LIFF の 2 つがあるのに無い、短いときは起動で止まる。変えると全員の ID が変わり、それまでのデータとつながらない([ADR-0008](adr/0008-keyed-line-user-id.md)) | 可 |
| `AICA_PUBLIC_URL` | なし | 手元の Web サーバーの公開 URL(Tailscale Funnel かトンネルの https の URL)。LINE で出す LIFF 画面(LIFF の設定が無いとき)と dip のログインの画面へのリンクの前に付ける。無ければリンクを出さず、アカウント連携も始めない | 可 |
| `LINE_LIFF_ID` | なし | LIFF アプリ(LINE ミニアプリのその環境)の LIFF ID。`LINE_LIFF_CHANNEL_ID` と両方あるときだけ、LINE のボタンと操作(履歴書、音声相談、レジュメ・アーケード)を LIFF アプリで開き、`/liff/line/…` の画面と `POST /api/liff/…` を開く(無ければ 404 で、ほかは今までどおり)。そのときは `AICA_ID_KEY` も要る(無い、短いときは起動で止まる) | 可 |
| `LINE_LIFF_CHANNEL_ID` | なし | LIFF アプリを置いたチャネル(LINE ミニアプリのその環境)の ID。LIFF の ID トークンを LINE に確かめてもらうときの `client_id`(`aud`)。前の名前の `LINE_LOGIN_CHANNEL_ID` も読む(こちらが先) | 可 |

音声相談で使うものです。トークだけを試すなら要りません。

| 環境変数 | 既定 | 用途 | `.env` |
|---|---|---|---|
| `LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET` | なし(必須) | LiveKit Cloud(URL は `wss://<project>.livekit.cloud`)。Web プロセスが部屋の接続トークンを発行し、音声ワーカーが部屋に入る | 可 |
| `DEEPGRAM_API_KEY` | なし(必須) | 文字起こし | 可 |
| `VOICE_RESUME_STT_PROVIDER` | `deepgram` | 文字起こしの提供元。**本番は `deepgram` に固定する**(`render.yaml` も `deepgram`)。`openai` は開発の比較用で、声そのものが OpenAI に届く。替えるならプライバシーポリシーと音声相談の同意の文言も直す([privacy.md](design/privacy.md) の 3.2)。Deepgram には `mip_opt_out=true` を付けて送り、改善プログラムに使わせない | 不可 |
| `CARTESIA_API_KEY` | なし(必須) | 読み上げ | 可 |
| `AICA_VOICE_WORKER` | なし | コンテナで `1` のとき、Web プロセスと同じコンテナで音声ワーカーも動かす(`scripts/container_start.sh`。手元の docker 向け。Render では、音声ワーカーを別サービスに移し終えたら `0`) | 不可 |
| `AICA_DIP_PORT` | なし | コンテナでこのポートに、別サービスの音声ワーカー向けの dip の MCP サーバーを `0.0.0.0` で開く(`scripts/container_start.sh`)。`AICA_INTERNAL_KEY` が無いと開かない | 不可 |
| `AICA_INTERNAL_KEY` | なし(音声相談の設定があるときは必須) | 内部の鍵(32 文字以上)。音声ワーカーが `POST /api/voice/result` と `POST /api/voice/session` に付け、Web サーバーが照合する。音声相談の設定(`LIVEKIT_*`)があるのに無い、短いときは、Web サーバーも音声ワーカーも起動で止まる。2 つのプロセスで値が違うと結果は 401 で届かず、ワーカーのログにエラーが出る。手で掃除を走らせる `python -m line_runtime.purge` も、`POST /api/internal/purge` に付ける | 可 |
| `VOICE_RESUME_TALK_URL` | `http://127.0.0.1:8030` | 音声ワーカーが結果を返す Web サーバーの URL | 不可 |
| `VOICE_RESUME_WRITE_FILES` | なし | `1` のときだけ、相談ごとの下書き、文字起こし、求人の提案を `VOICE_RESUME_OUT_DIR` に書き出す(開発用)。本番では付けない。無くても下書きは dip の状態に入り、トークに届く([privacy.md](design/privacy.md) の 3.3) | 不可 |
| `VOICE_RESUME_OUT_DIR` | `data/voice-resume` | `VOICE_RESUME_WRITE_FILES=1` のときの、下書き、文字起こし、求人の提案の置き場 | 不可 |
| `VOICE_RESUME_MAX_SESSION_MINUTES` | `10` | 1 回の音声相談の長さの上限(分)。時間が来たら、下書きを保存していなければ締めの返答を作り、部屋を閉じる | 不可 |
| `VOICE_RESUME_MAX_WEB_SEARCHES` | `5` | 1 回の音声相談で web を調べる回数の上限(調べもの、平均時給、会社の 3 つの合計) | 不可 |
| `VOICE_RESUME_MAX_JOBS` | 4 | 音声ワーカーが同時に受ける相談の数。超えた部屋にはアドバイザーが入らない(`dev` でも) | 不可 |
| `VOICE_RESUME_JOB_MEMORY_LIMIT_MB` | 2000 | 相談 1 つのプロセスのメモリの上限(MB)。超えたらその相談を止める。0 で上限なし | 不可 |
| `VOICE_RESUME_PROVIDER` | `LINE_SIM_PROVIDER` の値(既定 `anthropic`) | 音声相談の会話、下書き、調べもののプロバイダ(`anthropic`、`openai`)。既定では `ANTHROPIC_API_KEY` が要る(無いと相談を始めるときに失敗する) | 不可 |
| `VOICE_RESUME_*`(モデル、ボイス、ターンの判定) | [LLD の 7.1](architecture/lld.md#71-音声の部品の設定) | STT、LLM、読み上げの設定 | 不可 |
