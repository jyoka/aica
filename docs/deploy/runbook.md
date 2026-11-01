# ダウンとメモリ不足の runbook

`aica` と `aica-voice` が落ちた、メモリが足りなくなったと知らせが来たときに、何を見て、どう戻すかの手順です。
担当は運営者 1 人(jyoka)です。代わりの人はいません。

- 知らせの仕組みの設定は [render.md](render.md) の 6〜8 にあります。ここは、知らせが来た後の手順です
- 症状は、[Architecture Brief の「落ちたときの影響と自動の復旧」](../architecture.md#落ちたときの影響と自動の復旧)の表の行に合わせています
- 「試していない」と書いた手順は、本番でも試しの環境でも動かしていません。Render の文書と、リポジトリのコードから書きました
- バックアップの復元は、ここには書きません(Brief の「次にやること」の 6)

## 1. 担当といつ見るか

| 知らせ | 届く先 | 運営者がいつ見るか | 今の状態 |
|---|---|---|---|
| Render の通知(「Only failure notifications」)。build かデプロイの失敗、`aica` の unhealthy、ディスクの使用の 80% 超え、Blueprint の同期の失敗、Suspend | メール。運営者が Render にログインしているアカウント | 気づいたらすぐ | 設定待ち([render.md の 6](render.md#6-通知を設定して確かめる-運営者)) |
| 外形監視の Issue「外形監視: /healthz に届かない」 | Issue をリポジトリの持ち主(運営者)に割り当て、GitHub からメール。回復すると Issue が閉じ、閉じた知らせのメールが届く | 気づいたらすぐ | 設定待ち([render.md の 8](render.md#8-外形監視を有効にして確かめる-運営者)) |
| メモリ不足の毎日の点検(`scripts.render_events`)。判定は ok、look、act | 届かない。運営者が毎日 1 回、手元の Mac で動かす | 毎日 1 回。look は控えて Metrics と Logs を見る。act はその日のうちに | 始める前([render.md の 7](render.md#7-メモリ不足とインスタンスの失敗を毎日見る-運営者)) |
| 利用者からの連絡 | 問い合わせの窓口([プライバシーポリシーの 13](../legal/privacy-policy.md#13-問い合わせの窓口)) | 毎日の点検と同じときに、窓口のメールも見る | 窓口の連絡先は `contact@example.com` のまま。届くアドレスかは未確認 |

- 対応する時間は約束していません(利用規約の第 8 条)。運営者が気づいたときに始めます
- 運営者が対応できない間(移動中、休みなど)は、止まったままになります。代わりに見る人はいません
- 3 つの知らせが設定待ちと始める前の間は、運営者がダッシュボードを開くか、利用者から連絡が来るまで気づけません
- 外形監視は、デプロイの数秒の停止では知らせません。1 回の実行で 60 秒おきに 3 回呼び、全部失敗したときだけ知らせます。
  Render が数十秒で起動し直したメモリ不足は、外形監視では見えず、毎日の点検で分かります

## 2. はじめにすること

どの知らせでも、まずここをします。

1. 公開 URL の `/healthz` を呼ぶ。`200` なら、Web プロセスは起動を終えて答えています

   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 https://<名前>.onrender.com/healthz
   ```

   - `/healthz` は起動を終えていれば `200` を返すだけです。ディスク、dip、LLM は見ません。`200` でもトークが答えないことはあります
2. [Render のダッシュボード](https://dashboard.render.com/) で、`aica` と `aica-voice` の状態を見る。「Live」か、デプロイの途中か、失敗か、Suspend か
3. それぞれの「Events」で、直近の出来事を見る。`Out of memory`、`Instance Failed`、デプロイの成功と失敗、Suspend
4. 下の「4. 症状ごとの手順」から、当たる行を選ぶ
5. 日時、気づき方、「Events」の出来事を控える。外形監視の Issue が開いていれば、そこにコメントで書く。なければ jyoka/aica に Issue を開いて書く

見る場所の注意です。

- 「Metrics」は、メモリと CPU を、プランの上限に対する割合でも見られます(Render の文書 [Service Metrics](https://render.com/docs/service-metrics))。
  急にメモリが増えて落ちたときは、山が出ないことがあります(Render の文書 [Troubleshooting Your Deploy](https://render.com/docs/troubleshooting-deploys))
- 「Logs」は、右上で時間の範囲を選び、文字で探せます(既定は直近 1 時間。Render の文書 [Logs](https://render.com/docs/logging))
- 「Metrics」と「Logs」の保持は、Render の文書ではワークスペースのプランで決まります(Hobby は 7 日、Pro は 14 日)。今のプランは未確認です。7 日のうちに見ます
- アプリのログは値を出しません(`line_runtime/log_policy.py`)。uvicorn のアクセスログには IP とパスが出ます。控えるときに写しません

## 3. 戻し方

下の症状ごとの手順から、ここの A〜D を使います。どれも **【運営者】** の作業で、ダッシュボードの操作です。

### A. 起動し直す(試していない)

1. サービスの「Manual Deploy」→「Restart service」を押す。同じコミットと設定で、インスタンスを起動し直します(Render の文書 [Deploying on Render](https://render.com/docs/deploys))
2. 「Events」で、起動し直しが終わったことを見る

- `aica` はディスクつきなので、起動し直す間は止まります。止まる時間は測っていません。進行中のターンと音声相談は切れます
- Render の文書は、落ちたインスタンスを手で起動し直さず、Render に任せるよう書いています。Render が起動し直しても戻らないとき、
  「Live」なのに答えないとき、`aica-voice` が固まったときにだけ使います

### B. 前のデプロイに戻す(試していない)

デプロイの後に落ちるようになったときに使います。

1. サービスのデプロイの一覧で、前の成功したデプロイの「Rollback」を押し、「Rollback to this deploy」を押す
   (Render の文書 [Rollbacks](https://render.com/docs/rollbacks) では「Deploys」の画面。今のダッシュボードの画面の名前は未確認)
2. 「Events」で `Deploy live` になるまで待つ
3. 戻した後、原因のコミットを main で直すか戻す PR を出す。入れたら「Manual Deploy」→「Deploy latest commit」で前に進める

Render の文書に書いてある、戻るものと戻らないものです。

- 戻る: build したもの、Docker Command、ヘルスチェックのパス、台数、**環境変数**。その後に入れ直した鍵も、そのデプロイの時の値に戻ります
- 戻らない: ディスクの中身、プラン。新しいコードが書いたデータを、前のコードが読めないことがありえます(確かめていない)
- ダッシュボードで戻すと、自動デプロイが off になります。`aica` と `aica-voice` は、もともと off です(`render.yaml` の `autoDeployTrigger`)
- 戻せるのは、Render が build したものを残している間だけです。いくつ残すかはワークスペースのプランによります(数は文書になく、未確認)
- `render.yaml` を変えた PR で落ちたとき、戻した後に Blueprint の同期で `render.yaml` の内容が入り直すかは未確認です。
  `render.yaml` を元に戻す PR を main に入れるのが確かです(マージはデプロイです。[render.md の 1](render.md#1-blueprint-からサービスを作る-運営者))

### C. `aica-voice` を Suspend する(試していない)

`aica-voice` が落ち続ける、相談の結果が届かないなど、`aica-voice` を止めたほうがよいときに使います。

1. `aica-voice` の「Settings」で「Suspend」を押す
2. 直ったら、同じ画面で再開する(ボタンの名前は未確認)。再開した後、`aica-voice` の「Logs」で LiveKit に登録されたことを見る

- 音声ワーカーを移す間(`aica` の `AICA_VOICE_WORKER=1`)は、相談はすべて `aica` の音声ワーカーが受けます。音声相談は止まりません
- 移し終えた後(`AICA_VOICE_WORKER=0`)は、音声相談が止まります。トークは動きます。利用者に何と見えるかは確かめていません
- Suspend の間の料金の扱いは未確認です

### D. 鍵を入れ直す(試していない)

1. `aica` の「Environment」で値を直し、「Save Changes」を押す。`aica` が起動し直し、止まります
2. `aica-voice` が使う鍵(`ANTHROPIC_API_KEY`、`LIVEKIT_*`、`DEEPGRAM_API_KEY`、`CARTESIA_API_KEY`、`AICA_INTERNAL_KEY`)なら、
   Blueprint の「Manual sync」で写し直し、`aica-voice` を「Manual Deploy」する([render.md の「環境変数」](render.md#環境変数))
3. `aica-voice` の「Environment」に値が入ったことを見る。空なら、`aica-voice` の「Environment」に手で入れる
   (`fromService` でダッシュボードの値が写るかは未確認)

- `AICA_ID_KEY` は変えません。変えると、全員の LINE のユーザーIDが変わり、それまでのデータとつながらなくなります([ADR-0008](../adr/0008-keyed-line-user-id.md))
- 鍵が漏れたときの差し替えの手順(どこで無効にするか)は、まだありません(Brief の「次にやること」の 8)

## 4. 症状ごとの手順

### 4.1 `aica` のコンテナ全体が落ちる(2GB を超える、Web プロセスが終わる)

- 気づき方:
  - 毎日の点検で、`aica` のメモリ不足(`Out of memory`)かインスタンスの失敗(`Instance Failed`)が数えられる(look か act)
  - 戻らないとき: 外形監視の Issue。Render の通知の unhealthy。24 時間戻らなければ Render の通知の Suspend
  - 利用者から「つながらない」「返事が来ない」
- 見る場所:
  - 「Events」: `Out of memory`(メモリ不足)か `Instance Failed`(プロセスが予期せず終わった)と、その後に戻ったか
  - 「Metrics」: その時刻の前後のメモリ。待機だけで上限近くにいる、相談のたびに山になる、時間とともに増え続ける、のどれかを控える
  - 「Logs」: 落ちる直前のログ。`WARNING: voice worker exited` が続いていないか(音声ワーカーが同じコンテナにいる間)
- 戻し方:
  - ふつうは Render が起動し直します。戻っていれば、ここではすることはありません。回数の扱いは [render.md の 7 の「act のとき」](render.md#7-メモリ不足とインスタンスの失敗を毎日見る-運営者)のとおりです
  - 音声ワーカーを移す間(`AICA_VOICE_WORKER=1`)にメモリ不足が続くなら、移し終えるのが手当てです(Brief の「次にやること」の 5)
  - 10 分たっても「Live」に戻らない: A で起動し直す。デプロイの直後から落ちるなら B で前のデプロイに戻す
  - Render が Suspend した: 「Logs」で原因を直してから、再開する(ボタンの名前は未確認)
- 確かめ方: 下の「5. 戻ったことの確かめ方」の 1〜3

### 4.2 `aica` の Web プロセスが固まる(生きているが答えない)

- 気づき方:
  - Render の通知の unhealthy(`/healthz` の失敗が続いたとき)
  - 外形監視の Issue(約 2 分より長く答えないとき)
  - 利用者から。`/healthz` は答えるのにトークが答えないときは、利用者の連絡でしか気づけません
- 見る場所:
  - 「Events」: unhealthy と、その後の起動し直し。Render は `/healthz` が 15 秒続けて失敗すると要求を止め、60 秒続けて失敗すると起動し直します
  - 「Metrics」: CPU が上限に張りついていないか、メモリ、HTTP の要求の数
  - 「Logs」: 最後に出たログと、その時刻。止まる前のエラー
- 戻し方:
  - ふつうは Render が 60 秒で起動し直します
  - `/healthz` は `200` なのにトークが答えない: Render は気づかないので、A で起動し直す
  - 起動し直しても固まるのを繰り返す: デプロイの後からなら B。そうでなければ「Logs」を控えて Issue に書く
- 確かめ方: 5 の 1〜3。「`/healthz` は 200 なのに答えない」だったときは、5 の 4 も

### 4.3 `aica` のコンテナの中の音声ワーカーが落ちる

音声ワーカーを移す間(`AICA_VOICE_WORKER=1`)だけ起きます。

- 気づき方: 知らせは来ません。利用者から「音声相談がつながらない」。毎日の点検の look で「Logs」を見たとき
- 見る場所: `aica` の「Logs」で `WARNING: voice worker exited; restarting in 10s` が繰り返し出ていないか。
  `WARNING: could not download the voice models` が出ていれば、音声ワーカーなしで起動しています
- 戻し方:
  - 落ちても、`scripts/container_start.sh` のループが 10 秒後に起動し直します。`aica-voice` が相談を受けていれば、相談は続きます
  - 繰り返し落ちる、モデルを取れない: A で `aica` を起動し直す(Web も止まる)。相談の少ない時間にします
- 確かめ方: `aica` の「Logs」に警告が続かないこと。5 の 5 で音声相談を 1 件する

### 4.4 `aica` のコンテナの中の dip の MCP サーバーが落ちる

- 気づき方: 知らせは来ません。利用者から「音声相談で、希望が保存されない」「要約が出ない」など
- 見る場所: `aica` の「Logs」で `WARNING: dip MCP server for the voice worker exited; restarting in 10s` が繰り返し出ていないか。
  `aica-voice` の「Logs」で、dip の呼び出しの失敗(15 秒で諦める)が出ていないか
- 戻し方:
  - 10 秒後にループが起動し直します。音声相談は、dip の道具なしで続きます
  - 繰り返し落ちる: `AICA_INTERNAL_KEY` が `aica` にあるか(無いと dip の口は起動しない)を「Environment」で見る。無ければ D。あれば A で `aica` を起動し直す
- 確かめ方: `aica` の「Logs」に警告が続かないこと。5 の 5

### 4.5 `aica-voice` が落ちる、固まる

- 気づき方:
  - 毎日の点検で、`aica-voice` のメモリ不足かインスタンスの失敗が数えられる
  - Render の通知の build かデプロイの失敗、Suspend。unhealthy の通知は、バックグラウンドワーカーには出ないはず(未確認)
  - 利用者から「音声相談がつながらない」。固まったときは、これでしか気づけません(Brief の「次にやること」の 10)
- 見る場所:
  - `aica-voice` の「Events」: `Out of memory`、`Instance Failed`、デプロイ
  - `aica-voice` の「Metrics」: メモリ。待機だけで約 1.4GB、相談 2 件で約 1.9GB の見込みです([render.md の「メモリの見積もり」](render.md#メモリの見積もり))
  - `aica-voice` の「Logs」: LiveKit への登録、相談の始まりと終わり、結果を `aica` へ送るときのエラー(`401` なら `AICA_INTERNAL_KEY` が `aica` とそろっていない)
- 戻し方:
  - プロセスが終われば Render が起動し直すはずです(本番では確かめていない)
  - 固まった(Logs が止まり、相談を受けない): A で `aica-voice` を起動し直す。`aica` は止まりません
  - デプロイの後から落ちる: B で前のデプロイに戻す
  - 落ち続ける、結果が届かない: C で Suspend する。移す間なら相談は `aica` が受けます
  - 鍵が空か違う: D
- 確かめ方: 5 の 1 と 3、5 の 5

### 4.6 音声相談 1 件のプロセスが上限を超える

- 気づき方: 知らせは来ません。サービスのメモリ不足ではないので、毎日の点検にも数えられません(はず。確かめていない)。利用者から「音声相談が途中で切れた」
- 見る場所: `aica-voice` の「Logs」で、その時刻の相談のプロセスがメモリの上限で止められたログ(livekit-agents の出す文言は確かめていない)。
  `aica` の音声ワーカーが受けたときは `aica` の「Logs」。「Metrics」でその時刻のメモリ
- 戻し方: その相談だけが止まり、ほかの相談と Web は動き続けます。することはありません。
  続くときは、日時と回数を Issue に書きます(上限 400MB に収まるかを測るのは Brief の「次にやること」の 11)。
  測らずに `VOICE_RESUME_JOB_MEMORY_LIMIT_MB` を上げません。2 件で `aica-voice` の 2GB を超えうるためです
- 確かめ方: 5 の 5 で、相談が最後まで進むこと

### 4.7 デプロイ、環境変数の保存で止まる

デプロイと「Save Changes」は、運営者が自分でする操作です。相談の少ない時間にします。

- 気づき方:
  - Render の通知の build かデプロイの失敗
  - 起動に約 2 分より長くかかると、外形監視の Issue が開きます。次の回で閉じます
  - `render.yaml` を変えた PR のマージ: Blueprint の同期で、そのままデプロイされます。同期の失敗は Render の通知で届きます
- 見る場所: 「Events」で `Deploy live` になったか、失敗か。「Logs」で起動の途中のエラー。Blueprint の「Syncs」で同期が成功したか
- 戻し方:
  - 15 分たっても `Deploy live` にならない: 「Logs」で原因を見る([render.md の「うまくいかないとき」](render.md#うまくいかないとき))。鍵なら 4.8 へ
  - build の失敗のときに、前のインスタンスが動き続けるかは未確認です。5 の 1 で答えるかを見ます
  - 新しいコードで落ちる: B で前のデプロイに戻す
- 確かめ方: 5 の 1〜4

### 4.8 鍵の入れ間違い、鍵の不足

- 気づき方: Render の通知のデプロイの失敗(起動が終わらないため。届くかは確かめていない)。外形監視の Issue
- 見る場所: 「Logs」で、起動のときのエラーを探す
  - `ANTHROPIC_API_KEY がありません`: `aica` の Web プロセスが起動の途中で止まる
  - `InternalKeyError` と `AICA_INTERNAL_KEY がありません` か `が短すぎます`: `LIVEKIT_*` があるのに `AICA_INTERNAL_KEY` が無いか 32 文字未満。Web プロセスも音声ワーカーも止まる
  - ほかの鍵(`OPENAI_API_KEY`、`DEEPGRAM_API_KEY`、`CARTESIA_API_KEY`、`LIVEKIT_*` の値の誤り)は、起動では止まらないはずです(確かめていない)。使ったときに「Logs」にエラーが出ます
- 戻し方:
  - D で鍵を入れ直す。Render が起動し直しても、鍵を直すまで動かないはずです(確かめていない)
  - 鍵を変えた後のデプロイで落ちたなら、B でも戻せます。B は環境変数も前の値に戻します。前の鍵を無効にしていたら、B では戻りません
- 確かめ方: 5 の 1〜4。`aica-voice` の鍵なら 5 の 5 も

### 4.9 ディスクの中身が壊れる、消える

- 気づき方:
  - Render の通知のディスクの使用の 80% 超え(満ちると書き込めなくなる)
  - 利用者から「会話が消えた」「前回のことを覚えていない」
  - 「Logs」の `WARNING: /app/data is not a mounted disk.`(ディスクがつながっていない)
- 見る場所:
  - `aica` の「Shell」で `df -h /app/data`(使用量)と `ls -la /app/data`(ファイルがあるか、日時)
  - 「Logs」で、SQLite や JSON の読み書きのエラー
  - `aica` の「Settings」のディスク(`aica-data`、2GB、`/app/data`)がつながっているか
- 戻し方:
  - 80% 超え: 音声ワーカーを移し終えた後なら、`/app/data/cache/huggingface`(約 460MB、前のモデル)を消せます([render.md の 2 つ目の PR の 7](render.md#1-つのコンテナで動かしていたサービスを分ける-運営者))。
    移す間は消しません(`aica` の音声ワーカーが使う)。足りなければディスクを広げるかを決める(この runbook では決めていない)
  - ディスクがつながっていない: 「Settings」で Mount Path を `/app/data` にする。つながっていない間に書いたデータは、起動し直すと消えます
  - 中身が壊れた、消えた: 書き込みを増やさないよう、Web を止めるかを先に決めます。スナップショットからの復元は、手順がなく、試していません(Brief の「次にやること」の 6)。B ではディスクは戻りません
- 確かめ方: 5 の 1〜4。消えた人の会話が戻ったかは、その人の匿名ユーザーIDで `--cookie w-… --expect revisit` の smoke を流す(実 API)

## 5. 戻ったことの確かめ方

1. 2 の 1 のコマンドで、`/healthz` が `200` を返す
2. 「Events」で、サービスが「Live」(デプロイなら `Deploy live`)。その後に `Out of memory` と `Instance Failed` が増えていない
3. 外形監視の Issue が開いていれば、次の回(15 分ごと)で閉じることを見る。待たないときは、GitHub の「Actions」→「healthz」→「Run workflow」で動かす
4. トークが答えることを確かめる。**実 API を使うので、運営者だけがします**
   - スマホのブラウザで公開 URL を開き、1 回書いて返事が来ること
   - または手元から smoke を流す(初回の挨拶の 1 往復。実 API を 1 回使う)

     ```bash
     ./.venv/bin/python -m scripts.deploy_smoke --url https://<名前>.onrender.com
     ```

5. 音声相談を確かめる(`aica-voice` と、音声ワーカーと dip の MCP サーバーのとき)。**実 API を使うので、運営者だけがします**。
   トークの音声相談のボタンから 1 件相談し、`aica-voice` の「Logs」に相談が出て、結果がトークに届くこと
6. 2 の 5 で控えた Issue に、原因、戻し方、戻った時刻を書いて閉じる。メモリ不足が続くなら、render.md の 7 の「act のとき」のとおりカードにする

## 決めていないこと、確かめていないこと

- A〜D の手順は、どれも試していません。ボタンの名前は、Render の文書と [render.md](render.md) からです
- 利用者に、止まっていることをどう知らせるか(画面のお知らせ、状態のページ)は決めていません
- 問い合わせの窓口の連絡先(`contact@example.com`)が届くアドレスかは未確認です
- Render がメモリ不足の後に起動し直すまでの時間、`aica` の起動し直しで止まる時間は測っていません
- `aica-voice` のプロセスが終わったときに Render が起動し直すか、`aica-voice` で unhealthy の通知が出るかは未確認です
- 前のデプロイに戻すとき、Blueprint の同期が `render.yaml` の内容を入れ直すかは未確認です
