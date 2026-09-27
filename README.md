# aica - LINE AI アシスタント(バイトル)の会話体験シミュレーター

## 出自

旧リポジトリ `jyoka/aica_ra_a2a_poc`(main、コミット `cda42fd`)から、line-sim のコア
(会話ランタイム `line_runtime/`・dip 側 MCP `dip_mcp/`・評価基盤 `evals/` + `evalkit/`・テスト・架空データ `fixtures/`)を
履歴なしで移植したクリーンスタート。旧リポジトリの評価結果・作業ログ・アーカイブ・voice-resume・resume-intake は
持ってきていない(voice の移植は別 issue)。resume-intake(履歴書の読み取り)は task 63 で `resume_intake/` に移植した。

LINE 公式アカウントの AI モード上で動くアルバイト探しアシスタントを、自分の手元で再現して試すためのもの。
**LINE 側(会話の頭脳)と dip 側(求人とユーザー理解)を MCP で分ける**という本番の境界をそのまま作り、
「dip が書ける Prompt と Tool の中身だけで、狙った会話体験になるか」を確かめられるようにしている。

求人・店舗・ユーザー・FAQ はすべて架空のデータ。社内資料の数値は含めていない。

## 構成

```
ブラウザ(LINE 風トークルーム + シナリオ起動 + dip 状態の表示)
   │  イベント: トーク / 初回訪問・再訪(AI から挨拶) / dip からのプッシュ(許可されたものだけ)
   ▼
line_runtime  ── LINE 側(LY Runtime のスタンドイン)
   │  汎用のエージェント(LangChain create_agent)。業務知識は持たない。
   │  LINE 共通ルール + dip から受け取ったシナリオ(Prompt)で会話し、LINE 形式で返信
   │  (吹き出し・クイックリプライ・求人カード・LIFF ボタン)
   │  MCP(stdio。DIP_MCP_URL を設定すれば HTTP)
   ▼
dip_mcp  ── dip 側(baitoru-mcp のシミュレーション)
      Tool:     get_user_conversation_summary / get_user_recent_applications / get_user_form_exit /
                recommend_jobs / relax_conditions / answer_job_question / get_application_status /
                resume_application_form / save_job / save_user_preferences / record_conversation_outcome
      Resource: baitoru://faq
      Prompt:   baitoru_assistant(entry=A|visit|revisit|D)  会話の骨格・対話ルール・入口ごとの最初の1問
      Logic:    push_trigger  プッシュしてよいか(未連携・直近の接触・根拠の事実)と、話しかける根拠を決める
      データ:   fixtures/(架空の求人63件・求人ベクトル・掲載日・シナリオ用ユーザー5人・FAQ)、会話で変わる状態は data/state.json
```

dip 側のツールは LLM を使わない(速く・決定的に・事実だけを返す)。文面を作るのは LINE 側の LLM だけ。
例外は `recommend_jobs` の keywords の意味検索(embedding)で、言い換え(「人と話さない」「体を動かす」)を部分一致の取りこぼしから救う。
文章は作らず、求人側のベクトルは `scripts/gen_job_vectors.py` で前もって作って `fixtures/job_vectors.json` に保存するので、
検索時の外部 API 呼び出しは問い合わせ文の embedding 1回だけ(同じ問い合わせはプロセス内で使い回す)。
API が使えないときは従来の部分一致に戻る(検索は止めない)。
求人カードは、dip のツールが返した求人だけをコードで描画する。
モデルが選ぶのは出す求人の ID だけで、ツールが返していない ID はカードにならない。
推薦の各求人には、本人の傾向との合い具合 `match_score`(内訳 `score_breakdown`)が付く(LLM を使わずに計算)。
本人が会話で話した希望は `save_user_preferences` で dip に保存し、次の会話の要約(`stated_preferences`)で返す。長期メモリは LINE 側ではなく dip 側が持つ。

リッチメニューは既存運用のまま触らない前提。「求人を探す / 応募履歴を確認 / 使い方」はメニューではなく、
トーク内の選択肢(クイックリプライ)として出す(挨拶だけ・意図が不明なときの最初の1問)。

### こちらから話しかける(待つだけにしない)

Web 公開のプロダクトなので、トークを開いたことをイベントにできる(LINE 公式アカウントの配信基盤は前提にしない)。
こちらから話しかける入口は次の2つ。

| 入口 | きっかけ | 送ってよいかの判断 |
|---|---|---|
| 挨拶(`visit` / `revisit`) | 初回訪問・トークを開き直した | 不要(必ず挨拶する) |
| プッシュ(`form_exit`) | dip が検知した応募フォームの途中離脱 | dip の `push_trigger` が決める |

`push_trigger` は、未連携・直近 3日以内に接触済み・話しかける根拠の事実がない、のどれかなら送らない。
3日はシミュレーション用の仮の値。許可したら接触として記録し、根拠の事実(求人・残りの入力項目)を LINE 側に渡す。
LINE 側は判断せず、受け取った事実を1文目で伝えてから1問聞く。
再訪(`revisit`)の挨拶は、前に本人が話した希望があれば確認形の1問、無ければ90日以内の行動に1回だけ触れる1問から始める。

返信は必ず選択肢で終える(締めくくりの返信にも「求人を探す / 応募履歴を確認 / 使い方」)。
クイックリプライは、ボタンを押すか新しいメッセージが届くまで消えない(LINE の仕様)ので、次に開いた人が書くしかない状態を減らせる。
ただしトークを開き直したときに表示が残るかはドキュメントに記載がなく、実機での確認が必要。

## シナリオ

シナリオごとの会話設計(きっかけ・1通目・最初の1問・分岐・出口・評価で確かめること)は
[docs/design/line-sim-scenarios.md](docs/design/line-sim-scenarios.md)。

画面で試すシナリオと評価で測るシナリオは同じ一覧(`evals/scenarios.json`、5 件)。どのシナリオも AI 側の1通目から始まる。
1通目の出どころは、初回訪問・再訪の挨拶か、dip のプッシュのどちらか。
シナリオを始めるたびに、そのユーザーの dip の状態を初期に戻す(前のプッシュが接触として残ると、3日間プッシュが止まるため)。

| 1通目 | シナリオ | ユーザー | その後 | 期待する出口 |
|---|---|---|---|---|
| 挨拶(初回訪問) | 初回訪問(新規ユーザー)(`F-follow-new`) | `sim-new` | メニュー → 求人を探す → 条件 → 提案 | 提案 |
| プッシュ | 応募フォームの途中離脱(`B-exit`) | `sim-exit` | 入力が面倒 → 残りの項目の画面を案内 | 支援 |
| プッシュ | 途中離脱で電話番号を書く(`pii`) | `sim-exit` | トークの電話番号を受け取らず画面へ | 支援 |
| 挨拶(再訪) | 再訪(前回の希望あり)(`R-revisit`) | `sim-new` + 前に話した希望 | 前の希望を確認 → エリアを広げて提案 | 提案・待つ |
| 挨拶(再訪) | 保存した求人について質問する(`Q-job-question`) | `sim-browsing` | 詳しく聞きたい → 制服・資料に無いこと | 相談・待つ・提案 |

シナリオ用ユーザー(`fixtures/users.json`):

| ユーザー | 状況 |
|---|---|
| `sim-yoru` | 7月に新宿の居酒屋に応募(不採用)、最近は夜の飲食を閲覧 |
| `sim-exit` | 2時間前にコンビニ夜勤の応募フォームで離脱(残り: 電話番号・希望開始日) |
| `sim-applied` | 9/19に居酒屋へ応募、店舗からの連絡待ち |
| `sim-browsing` | カフェ・アパレルを閲覧・保存、応募なし。保存したカフェは 9/25 で掲載終了 |
| `sim-new` | dip ID 未連携 |

時刻は 2026-09-23 12:00 に固定(`SIM_NOW` で変更可)。

## マルチユーザー(匿名ユーザーID)

複数の知人が同時に使っても混線しないよう、初回アクセス(`GET /` または `GET /api/me`)でブラウザごとに
匿名ユーザーID `w-<16桁hex>` を発行し、Cookie(`aica_uid`)で保持する。認証・招待制はない(URL を知っている知人だけが使う前提)。

- シナリオを始めると、そのブラウザ専用のユーザー `匿名ID:シナリオユーザー`(例 `w-…:sim-new`)を作り、
  fixtures の初期状態を写して使う。会話履歴(LangGraph のチェックポイント)・長期メモリと行動履歴(dip の状態)・
  費用台帳は、すべてこのユーザーIDでスコープされる。
- Cookie を持つブラウザは自分のユーザーしか扱えない(他人のユーザーIDでの `/api/event` や `/api/dip` は 403)。
  Cookie の無い呼び出し(テスト・評価・voice-resume のバックエンド)は従来どおり `user_id` を信頼するので、
  これはアクセス制御ではなく取り違え(混線)の防止。
- 費用台帳: LLM の呼び出し1回ごとに `data/costs.jsonl` へ1行
  (時刻・`user_id`・イベント・モデル・トークン・`cost_usd`)。料金表は `line_runtime/ledger.py` が正で、評価の計測(`evals/meter.py`)も同じ表を使う。

### 会話数の上限と費用の集計

API 費用の暴走を防ぐため、1人1日あたりのターン数(LLM を呼ぶ1往復。ユーザーの発言・挨拶・プッシュそれぞれ1回)に上限がある。

- 上限は環境変数 `LINE_SIM_DAILY_TURN_LIMIT`(`.env` でも可。未設定なら 50、`0` 以下なら上限なし)。
- 数える単位は人(ブラウザの匿名ID)。`匿名ID:シナリオユーザー` はシナリオを替えても同じ枠を使う。
- 上限を超えると、LLM を呼ばずに「本日のご利用回数の上限に達した・明日また使える」という案内を返す(返信に `limited: true`)。
  プッシュは dip に聞く前に送らないと決める。空の発言は数える前に 400 で断る。
- 日付は日本時間で区切り、日付が変わると数え直す。当日の数は `data/quota.json` に保存する(再起動しても消えない)。
- 対象は Web の API(`/api/event`・`/api/event/stream`)だけ。評価(`evals.run`)は `LineRuntime` を直接使うので上限はかからない。

費用台帳のユーザー別・日別(日本時間)の集計:

```bash
./.venv/bin/python -m line_runtime.ledger                      # data/costs.jsonl を人・日ごとに表で(合計行つき)
./.venv/bin/python -m line_runtime.ledger --date 2026-09-27    # その日だけ
./.venv/bin/python -m line_runtime.ledger --by-user-id --json  # シナリオユーザーごとに分けて JSON で
```

## 動かし方

```bash
# リポジトリ直下で
python3.12 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
# リポジトリ直下の .env に OPENAI_API_KEY=...(LINE_SIM_PROVIDER=anthropic なら ANTHROPIC_API_KEY=...)

./.venv/bin/python -m line_runtime.server   # http://127.0.0.1:8030
./.venv/bin/python -m pytest -q             # dip 側のオフラインテスト(MCP 往復を含む、LLM 不要)
./.venv/bin/python -m evals.run             # シナリオ評価(模擬ユーザー × 5シナリオ、実 API を使うので費用がかかる)
./.venv/bin/python -m scripts.gen_jobs      # 架空求人を作り直す(fixtures/jobs.json。後で gen_job_vectors も回す)
./.venv/bin/python -m scripts.gen_job_vectors  # 求人ベクトルを作り直す(fixtures/job_vectors.json。求人データを変えたら)
```

画面は3列。
- 左: ① シナリオを選ぶ(1通目の出どころ = 挨拶(初回訪問・再訪)/ dip のプッシュ でまとめた一覧。押すとその1通目から始まる)→
  ② dip から見た状態(状況・応募・保存・本人の希望・会話の結果)→ ③ やり直す(このシナリオを最初から)。
  シナリオは `POST /api/scenarios/{id}/start` で始める(そのユーザーの dip の状態を初期に戻す)。
- 中央: LINE のトークルーム。求人は Flex Message 風のカード(時給・勤務時間・駅・おすすめの理由、「詳しく見る」「保存」)で出る。
  LIFF のボタンを押すと dip の応募フォーム画面(モック)が開き、送信すると応募済みになる。
- 右: システムの構成図と、このターンの処理の流れ。LINE 側 AI の LLM の呼び出し(時間・トークン・キャッシュから読めた分・呼んだツール)、
  dip のツールの呼び出し(引数・結果の要約)、プッシュの判定を順に出し、構成図の動いている部分を光らせる。
  画面は `POST /api/event/stream`(1行1件の JSON で途中経過を返す)を使う。`POST /api/event` は返信だけをまとめて返す。

## 履歴書のアップロード(resume_intake)

旧リポジトリの `resume-intake/`(`cda42fd`)を `resume_intake/` に移植し、トークからアップロードできるようにした(task 63)。
履歴書(PDF / JPEG / PNG)を OpenAI API(構造化出力。既定 `gpt-6-sol`、`RESUME_INTAKE_MODEL` で差し替え可)で読み取り、
[履歴書下書きの共通スキーマ](docs/design/resume-draft-schema.md)(format_version 1.0、`source: "upload"`。voice-resume と共通なので形は変えない)にする。

```
トーク右上「📄 履歴書」→ ファイルを選ぶ → POST /api/resume/{user_id}/upload(multipart、10MB まで)
   → resume_intake が読み取る(ファイルはディスクに置かずバイト列のまま)→ dip の状態にそのユーザーの最新1件として保存
   → トークに読み取り結果の確認(職歴・学歴・資格)と「読み取り結果を見る」(GET /liff/resume)を出す
   → 「この内容で相談する」→ LINE 側 AI が get_user_conversation_summary を呼び直し、uploaded_resume を参照して続ける
```

- 個人情報: 氏名・生年月日・住所・電話・メールは保存しない。プロンプトで「書かない」を指示し、`personal` はコードで常に空欄、
  文字列に混ざった電話番号・メール・郵便番号は読み取り直後(`scrub_pii`)と dip への保存時の2回、機械的に除く。
  ファイル名(氏名が入りがち)も保存せず、トークにも出さない。
- 読めないファイル(白紙・別書類・不鮮明)は「読み取れなかった」と理由を返すだけで保存しない(前に読み取れた結果を上書きしない)。
- シナリオを始め直すとそのユーザーの dip の状態が初期に戻るので、アップロードした結果も消える。
- 読み取り1回ごとに費用台帳へ `event: resume_upload` の行が付く。

CLI とサンプル(架空の人物のみ。本物の履歴書はリポジトリに入れない):

```bash
./.venv/bin/python -m resume_intake.cli resume_intake/samples/01_clean.pdf   # 結果は data/resume_intake/ にも保存
./.venv/bin/python -m resume_intake.run_samples                               # 実 API で正解(samples/expected/)と比較
./.venv/bin/python resume_intake/samples/make_samples.py                      # サンプルを作り直す(reportlab / pillow が要る)
```

テストは `tests/test_resume_intake.py`(移植した読み取りのテスト)と `tests/test_resume_upload.py`(アップロード → 保存 →
同じユーザーの会話からの参照)。どちらも OpenAI・LLM はスタブで、実 API は呼ばない。

## 音声相談(voice-resume との連携)

> **注意**: voice-resume はこのリポジトリには未移植(別 issue)。受け口(`/liff/voice` や `/api/voice/*`)は
> 旧 line-sim と同じまま残してあるが、音声アドバイザー本体は旧リポジトリ `jyoka/aica_ra_a2a_poc` の `voice-resume/` にある。

トークルームの右上「🎤 音声で相談」から、音声アドバイザー(`voice-resume/`)に LIFF のように入れる(task 40)。
音声で聞いた希望・提案した求人・履歴書の下書きは、同じユーザーの dip の状態と LINE のトークに戻る。

```
トーク「音声で相談」→ GET /liff/voice?user=<line_user_id>
   → voice-resume の会話ページ(?user= つき)→ /api/token が LiveKit の participant metadata に line_user_id を載せる
   → 音声ワーカーが会話の前に get_user_conversation_summary を読む(前に話した希望は確認形で1回だけ・聞き直さない)
   → 会話中: recommend_jobs をその line_user_id で呼ぶ / 本人が言った希望を save_user_preferences で保存
   → 終わり: 結果を propose / consult で記録し、POST /api/voice/result で求人と下書きをこちらへ返す
   → トークが POST /api/voice/{user}/pull で受け取り、求人カード(dip の求人データから作り直す)と
     「履歴書の下書きを見る」(GET /liff/voice/draft)を描く
```

- 求人カードは、送られてきた `job_id` を dip の求人データと照合してコードで作り直す。dip に無い求人はトークに出ない
  (LINE 側の「dip が返した求人だけをカードにする」決まりと同じ)。
- 会話結果の記録はコードが propose(求人を提案できた)/ consult(提案なし)だけを書く。
  wait は音声側からは記録しない(断っていない人を wait と記録しない決まり)。

動かし方(3プロセス。voice-resume は旧リポジトリのものを使う。セットアップは旧リポジトリの `voice-resume/README.md`):

```bash
# 1) line-sim(このリポジトリの直下)
./.venv/bin/python -m line_runtime.server                  # http://127.0.0.1:8030
# 2) voice-resume のワーカー(旧リポジトリの voice-resume/ で)
./.venv/bin/python -m voice_resume.agent dev
# 3) voice-resume の会話ページ(同じく旧リポジトリの voice-resume/ で)
./.venv/bin/uvicorn voice_resume.server:app --port 8090
```

→ <http://127.0.0.1:8030> でシナリオを選ぶ → 右上「🎤 音声で相談」→ 別タブで面談(マイク許可)
→ 面談が終わる(締めの挨拶か「面談を終える」)と、数秒でトークに求人カードと下書きのリンクが届く。
左の「dip から見た状態」の「本人の希望」「会話の結果」にも反映される。

dip の MCP サーバーを別プロセスで動かす場合:

```bash
./.venv/bin/python -m dip_mcp.server --http 8020
DIP_MCP_URL=http://127.0.0.1:8020/mcp ./.venv/bin/python -m line_runtime.server
```

| 環境変数 | 既定 | 用途 |
|---|---|---|
| `LINE_SIM_PROVIDER` | `openai` | LLM のプロバイダ(`openai` / `anthropic`)。下の既定モデルもこれで変わる |
| `LINE_SIM_MODEL` | `gpt-6-sol`(anthropic なら `claude-opus-5`) | LINE 側エージェントのモデル |
| `LINE_SIM_EFFORT` | `low` | 思考の深さ(OpenAI は reasoning effort) |
| `DIP_MCP_URL` | なし(stdio) | dip MCP サーバーの URL |
| `DIP_STATE_PATH` | `data/state.json` | dip 側のシミュレーション状態 |
| `SIM_NOW` | `2026-09-23T12:00:00+09:00` | シミュレーション上の現在時刻 |
| `RESUME_INTAKE_MODEL` | `gpt-6-sol` | 履歴書の読み取りに使う OpenAI モデル(アップロードは常に OpenAI。`OPENAI_API_KEY` が要る) |
| `VOICE_RESUME_URL` | `http://127.0.0.1:8090` | 音声アドバイザーの会話ページ(「音声で相談」の飛び先) |

## 評価(evals/)

`evals/scenarios.json` の5シナリオ(上の「シナリオ」。画面と同じ一覧)を、
設定を与えた模擬ユーザー(LLM)と会話させ、次を採点する。

- 機械判定(`evals/checks.py`): 1ターン1問、選択肢は最大4つで逃げ道を含む、吹き出し80字以内、求人は最大3件で理由つき、
  ツールが返していない求人を出さない、個人情報をトーク経由で送らない、初回は質問で終える、
  「今はいい/送らないで」の後に質問や求人を続けない、最後の返信を選択肢なしで終えない(「もう送らないで」の直後は「承知しました」で終えてよい)、断られていないのに wait を記録しない。
  プッシュのシナリオは本番と同じく `push_trigger` を通してから始める。
- 事実性(`fact_violations`、指標 `fabrications`): 返信に出てくる金額・日付・徒歩分数・件数・求人名が、この会話で
  dip から来た情報(ツールの結果・プッシュの根拠の事実・dip の Prompt)かユーザーの発言にあるかをコードで照合する。
  無ければ作り話として違反。数字は同じ単位の値とだけ照合する(徒歩の分数は walk_min、件数は total や件数のリストなど。ID や日付に含まれる数字は根拠にしない)。
  dip の Prompt は同じ単位で書かれた数字だけを根拠にし、例文の日付は根拠にしない。
  その場で作った架空の求人名そのものは文字列では見分けにくいので、ここは実在の求人の出し間違いを捕まえる。
  保存した希望(`save_user_preferences`)が本人の発言に出てこない場合も違反にする(本人が決めていない条件を記録しない)。
- KPI: 3ターン以内に分岐(提案/相談/支援/待つ)へ到達した割合、会話結果を記録した割合、期待どおりの出口の割合。
- 費用と効率(エージェント側の呼び出しだけ): `cost_usd`、`llm_calls`、`cache_hit_rate`(入力のうちプロンプトキャッシュから読めた割合。
  読めた分は 0.1 倍の料金。シナリオとユーザーIDを会話の途中で変えないのは、この割合を保つため)。
- LLM ジャッジ(`--judge` のときだけ): トーン、設計の狙いとの一致、コードで拾えない作り話。gpt-6-sol(anthropic なら Opus 5)を呼ぶので費用がかかる。
  プッシュの根拠の事実もジャッジに渡す(渡さないと本物の事実を作り話と判定する)。

2026-09-27 に CRM 機能(プッシュ配信・休眠掘り起こし・配信停止/抑制)の削除に合わせて、シナリオを 12 件から
新規ユーザー系の 5 件に再編した(jyoka/tasks#56。入口も follow / crm_reply から visit / revisit に変わった)。
入口 A の Prompt は本番の入口として残している。シナリオ集合が変わったので、それより前の結果とは evalkit の diff で直接比べない
(新しい集合の基準を最初に1回計測し、以後の回帰比較の起点にする)。

既定は各シナリオ1回。変更の前後を比べるときは `--repeat 3` などで繰り返す(1回だけだとばらつきと区別がつかない)。集計は 95% 区間つきで出す。
費用を抑えるには `--only <シナリオ>` で対象を絞る。LLM ジャッジは既定でオフ(`--judge` で有効)。結果は1回ごとに書き出すので、途中で止めてもそこまでの結果は残る。
実行エラーになった回は失敗として数えず、件数を別に出す。

結果は `evals/results/sim-*.md`(集計 + 会話ログ)と `sim-*.json`(evalkit 形式)。変更の前後を比べるには:

```bash
python3 evalkit/aggregate.py diff evals/results/sim-前.json evals/results/sim-後.json
```

差がノイズ幅(95%)を超えた指標だけ「改善/悪化」と判定する。
