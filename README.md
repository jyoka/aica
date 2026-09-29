# aica - LINE AI アシスタント(バイトル)の会話体験シミュレーター

## 出自

旧リポジトリ `jyoka/aica_ra_a2a_poc`(main、コミット `cda42fd`)から、line-sim のコア
(会話ランタイム `line_runtime/`・dip 側 MCP `dip_mcp/`・評価基盤 `evals/` + `evalkit/`・テスト・架空データ `fixtures/`)を
履歴なしで移植したクリーンスタート。旧リポジトリの評価結果・作業ログ・アーカイブは持ってきていない。
resume-intake(履歴書の読み取り)は task 63 で `resume_intake/` に、音声棚卸しアドバイザー(voice-resume)は
task 62 で `voice_resume/` に移植した(下の「音声相談」)。

LINE 公式アカウントの AI モード上で動くアルバイト探しアシスタントを、自分の手元で再現して試すためのもの。
**LINE 側(会話の頭脳)と dip 側(求人とユーザー理解)を MCP で分ける**という本番の境界をそのまま作り、
「dip が書ける Prompt と Tool の中身だけで、狙った会話体験になるか」を確かめられるようにしている。

求人・店舗・ユーザー・FAQ はすべて架空のデータ。社内資料の数値は含めていない。

## 構成

```
ブラウザ(本番 /: LINE 風トーク1画面 / 開発者 /dev: シナリオ起動 + dip 状態 + 処理の流れ)
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
                resume_application_form / save_job / save_user_preferences / set_search_track /
                record_conversation_outcome
      Resource: baitoru://faq
      Prompt:   baitoru_assistant(entry=A|visit|revisit|D)  会話の骨格・対話ルール・入口ごとの最初の1問
      Logic:    push_trigger  プッシュしてよいか(未連携・直近の接触・根拠の事実)と、話しかける根拠を決める
      データ:   fixtures/(架空の求人103件〔アルバイト・パート63件、正社員40件〕・求人ベクトル・掲載日・シナリオ用ユーザー5人・FAQ)、会話で変わる状態は data/state.json
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
いま探している雇用区分(アルバイト・パート / 正社員 / 未定)は、本人の発言を根拠に `set_search_track` で切り替えて dip に履歴ごと残し、要約の `search_track` で返す(初回は未定。ADR-0001)。

リッチメニューは既存運用のまま触らない前提。「求人を探す / 応募履歴を確認 / 使い方」はメニューではなく、
トーク内の選択肢(クイックリプライ)として出す(挨拶だけ・意図が不明なときの最初の1問。初回訪問の挨拶では「使い方」の代わりに「まず相談する」)。

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

画面で試すシナリオと評価で測るシナリオは同じ一覧(`evals/scenarios.json`、9 件)。どのシナリオも AI 側の1通目から始まる。
1通目の出どころは、初回訪問・再訪の挨拶か、dip のプッシュのどちらか。
シナリオを始めるたびに、そのユーザーの dip の状態を初期に戻す(前のプッシュが接触として残ると、3日間プッシュが止まるため)。

| 1通目 | シナリオ | ユーザー | その後 | 期待する出口 |
|---|---|---|---|---|
| 挨拶(初回訪問) | 初回訪問(新規ユーザー)(`F-follow-new`) | `sim-new` | メニュー → 求人を探す → 条件 → 提案 | 提案 |
| プッシュ | 応募フォームの途中離脱(`B-exit`) | `sim-exit` | 入力が面倒 → 残りの項目の画面を案内 | 支援 |
| プッシュ | 途中離脱で電話番号を書く(`pii`) | `sim-exit` | トークの電話番号を受け取らず画面へ | 支援 |
| 挨拶(再訪) | 再訪(希望あり)(`R-revisit`) | `sim-new` + 前に話した希望 | 前の希望を確認 → 差分だけ答えて提案 | 提案・待つ |
| 挨拶(再訪) | 再訪(行動履歴のみ)(`R-revisit-browsing`) | `sim-browsing` | 最近の閲覧に1回だけ触れる → 条件 → 提案 | 提案・待つ |
| 挨拶(再訪) | 保存した求人について質問する(`Q-job-question`) | `sim-browsing` | 詳しく聞きたい → 制服・資料に無いこと | 相談・待つ・提案 |
| 挨拶(初回訪問) | 探し方が分からない → 相談 → 提案(`C-consult-propose`) | `sim-new` | 「何から始めれば」→ 相談モードで1問ずつ → 提案 | 提案 |
| 挨拶(初回訪問) | 相談だけで終える(`C-consult-only`) | `sim-new` | まず相談する → 1問答える → 今はいい | 相談・待つ |
| 挨拶(初回訪問) | 相談の途中で提案へ切替(`C-consult-switch`) | `sim-new` | まず相談する → 1問答える → 求人を見せて | 提案 |

シナリオ用ユーザー(`fixtures/users.json`):

| ユーザー | 状況 |
|---|---|
| `sim-yoru` | 7月に新宿の居酒屋に応募(不採用)、最近は夜の飲食を閲覧 |
| `sim-exit` | 2時間前にコンビニ夜勤の応募フォームで離脱(残り: 電話番号・希望開始日) |
| `sim-applied` | 9/19に居酒屋へ応募、店舗からの連絡待ち |
| `sim-browsing` | カフェ・アパレルを閲覧・保存、応募なし。保存したカフェは 9/25 で掲載終了 |
| `sim-new` | dip ID 未連携 |

時刻は 2026-09-23 12:00 に固定(`SIM_NOW` で変更可)。

### 相談モード(何から始めればいいか分からない人向け)

別のエージェントやグラフではなく、dip の Prompt(`baitoru_assistant.md` の「相談モード」)のステージ指針と、入口の選択肢で動く。

- 入口: 初回挨拶の選択肢「まず相談する」か、会話中の迷いのサイン(「何から始めれば」「自分に合うのが分からない」)。
- 経験・興味・不安・生活の制約を1問ずつ聞く。相談中は「3ターン以内に分岐」を緩め、ヒアリングが終わるか本人が切り上げるまで続けてよい。
- 「自分に何が合うか分からない」には、経験と興味から向いていそうな職種を理由つきで挙げる。
- 話した内容は長期メモリ(`save_user_preferences` の8項目)に保存し、そのまま求人提案の条件にする(言い直し不要)。
- 「今はいい」なら提案なしで終える。途中で「求人を見せて」と言えば、その時点までの内容ですぐ提案に切り替える。

## マルチユーザー(匿名ユーザーID)

複数の知人が同時に使っても混線しないよう、初回アクセス(`GET /`・`GET /dev`・`POST /api/talk/open`・`GET /api/me`)で
ブラウザごとに匿名ユーザーID `w-<16桁hex>` を発行し、Cookie(`aica_uid`)で保持する。認証・招待制はない(URL を知っている知人だけが使う前提)。

- 本番のトーク画面(`/`)では、匿名ID そのもの(`w-…`)がユーザーID。fixtures のシナリオ用ユーザーは使わず、空の状態から始まる。
  状態を初期に戻す操作は無い。
- 開発者画面(`/dev`)でシナリオを始めると、そのブラウザ専用のユーザー `匿名ID:シナリオユーザー`(例 `w-…:sim-new`)を作り、
  fixtures の初期状態を写して使う。会話履歴(LangGraph のチェックポイント)・長期メモリと行動履歴(dip の状態)・
  費用台帳は、すべてこのユーザーIDでスコープされる。
- Cookie を持つブラウザは自分のユーザーしか扱えない(他人のユーザーIDでの `/api/event` や `/api/dip` は 403)。
  Cookie の無い呼び出し(テスト・評価・voice-resume のバックエンド)は従来どおり `user_id` を信頼するので、
  これはアクセス制御ではなく取り違え(混線)の防止。ただし音声相談の接続トークンの発行(`POST /api/voice/{user}/start`)と
  結果の待ち(`POST /api/voice/{user}/wait`)は、実 API の費用につながるので Cookie の無い呼び出しも断る(403)。
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

./.venv/bin/python -m line_runtime.server   # 本番のトーク http://127.0.0.1:8030/ ・開発者画面 http://127.0.0.1:8030/dev
./.venv/bin/python -m pytest -q             # dip 側のオフラインテスト(MCP 往復を含む、LLM 不要)
./.venv/bin/python -m evals.run             # シナリオ評価(模擬ユーザー × 6シナリオ、実 API を使うので費用がかかる)
./.venv/bin/python -m scripts.gen_jobs      # 架空のアルバイト求人を作り直す(fixtures/jobs.json。正社員の fulltime_jobs.json は手書き。後で gen_job_vectors も回す)
./.venv/bin/python -m scripts.gen_job_vectors  # 求人ベクトルを作り直す(fixtures/job_vectors.json。求人データを変えたら)
```

画面は2つ。

**本番のトーク画面 <http://127.0.0.1:8030/>**(求職者が使う。スマホ前提の1画面)
- シナリオは選ばない。開くと `POST /api/talk/open` がこのブラウザの匿名ユーザー(匿名ID そのもの)について決める:
  - 最後のやりとりから30分以内(再読み込み・タブの開き直し): 今の会話を続ける。画面は `GET /api/talk/history/{user_id}`
    (本人の Cookie でしか読めない)で吹き出し・求人カード・選択肢を描き直す。LLM は呼ばず、会話数も使わない。
  - それ以外: dip の状態(前にトークを開いて挨拶が返った記録 `talk_opens`)から初回訪問(`visit`)か再訪(`revisit`)かを決め、
    画面はそのイベントで `POST /api/event/stream` を呼び、AI 側の1通目(挨拶)で新しい会話が始まる。
    開いた記録は挨拶が返ったときに残す(挨拶が失敗・上限で返らなければ、次も初回訪問。挨拶は保存されたのに返す途中で接続が切れたときは、次に開いたとき保存された挨拶から記録を補う)。
  - 画面は表示している会話の番号(`thread`)を発言に付けて送る。別のタブで新しい会話が始まった後に古いタブから送ると、
    発言は新しい会話に入らず(LLM も会話数も使わない)、古いタブに「別の画面で新しい会話が始まりました」と出して最新の会話を描き直す。
- 下のナビは3つ: トーク / 保存した求人(`GET /api/saved/{user_id}`。保存した求人をカードで一覧)/ プロフィール(準備中)。
- dip の状態・構成図・処理の流れ・シナリオ一覧は出さない。会話数の上限とユーザーの分離は本番の画面にも効く。
- 履歴書と音声相談は会話の中から入る(task 91。ヘッダーの常設ボタンは無い):
  - AI が経歴を聞く場面(相談モードの経験の1問など)で、返信の `actions` として [ファイルを選ぶ] [話して伝える(音声・約5分)] を
    差し出す(`quick_replies` の「文字で話す」「今はいい」と並ぶ。押しても発言は送られない)。いつ出すかは dip の Prompt(「履歴書・音声の提案」)。
  - 自分から使う人は、入力欄の左の「+」から「履歴書・職務経歴書を送る」「音声で相談する」に入れる(LINE と同じ置き場)。
  - 詳しくは下の各節。
- トークの部品(吹き出し・求人カード・返信の描画・ストリームの読み取り・待ちの表示・「+」メニュー・履歴書・音声)は
  `line_runtime/static/talk-core.js`(と `talk-core.css`)にあり、本番の `/` と開発者画面の `/dev` が共通に使う。

**開発者画面 <http://127.0.0.1:8030/dev>**(評価・説明用。3列。合言葉などの制限は無い)
- 左: ① シナリオを選ぶ(1通目の出どころ = 挨拶(初回訪問・再訪)/ dip のプッシュ でまとめた一覧。押すとその1通目から始まる)→
  ② dip から見た状態(状況・応募・保存・本人の希望・会話の結果)→ ③ やり直す(このシナリオを最初から)。
  シナリオは `POST /api/scenarios/{id}/start` で始める(そのユーザーの dip の状態を初期に戻す)。
- 中央: LINE のトークルーム。求人は Flex Message 風のカード(アルバイト・パートは時給・勤務時間、正社員は年収か月給の幅・働き方・休日、駅・おすすめの理由、「詳しく見る」「保存」)で出る。
  LIFF のボタンを押すと dip の応募フォーム画面(モック)が開き、送信すると応募済みになる。
- 右: システムの構成図と、このターンの処理の流れ。LINE 側 AI の LLM の呼び出し(時間・トークン・キャッシュから読めた分・呼んだツール)、
  dip のツールの呼び出し(引数・結果の要約)、プッシュの判定を順に出し、構成図の動いている部分を光らせる。
  画面は `POST /api/event/stream`(1行1件の JSON で途中経過を返す)を使う。`POST /api/event` は返信だけをまとめて返す。

## 履歴書のアップロード(resume_intake)

旧リポジトリの `resume-intake/`(`cda42fd`)を `resume_intake/` に移植し、トークからアップロードできるようにした(task 63)。
履歴書(PDF / JPEG / PNG)を OpenAI API(構造化出力。既定 `gpt-6-sol`、`RESUME_INTAKE_MODEL` で差し替え可)で読み取り、
[履歴書下書きの共通スキーマ](docs/design/resume-draft-schema.md)(format_version 1.0、`source: "upload"`。voice-resume と共通なので形は変えない)にする。

```
AI が差し出す [ファイルを選ぶ] か、入力欄の「+」→「履歴書・職務経歴書を送る」→ ファイルを選ぶ
   → 本人側にファイルの吹き出し「履歴書(PDF)」(ファイル名は出さない)、AI 側は「入力中」
   → POST /api/resume/{user_id}/upload(multipart、10MB まで。会話数を1つ使う)
   → resume_intake が読み取る(ファイルはディスクに置かずバイト列のまま)→ dip の状態にそのユーザーの最新1件として保存
   → AI 側に読み取り結果の確認(要点だけ: 職歴・スキル・資格を最大3件ずつ、吹き出しは80字以内。`line_runtime/resume_confirm.py`)と「読み取り結果を見る」(GET /liff/resume。全文)を出す
   → 画面が POST /api/event/stream を event=resume_upload で呼ぶ(本人の1ターン。会話数を1つ使う)。LINE 側 AI には
     「[イベント: 本人が履歴書(PDF)をアップロード]」と画面に出した確認が渡り、get_user_conversation_summary を呼び直して
     uploaded_resume を見て、確認を繰り返さずに次の1問を返す
```

- アップロードは本人の1ターン。定型文を本人の発言として送ることはしない。再読み込みで描き直すときも、ファイルの吹き出し → 確認 → 次の1問 の順に出る
  (会話にはファイルの表示名と確認の吹き出しだけを保存する)。

- 個人情報: 氏名・生年月日・住所・電話・メールは保存しない。プロンプトで「書かない」を指示し、`personal` はコードで常に空欄、
  文字列に混ざった電話番号・メール・郵便番号は読み取り直後(`scrub_pii`)と dip への保存時の2回、機械的に除く。
  ファイル名(氏名が入りがち)も保存せず、トークにも出さない。
- 読めないファイル(白紙・別書類・不鮮明)は「読み取れなかった」と理由を返すだけで保存しない(前に読み取れた結果を上書きしない)。
  その場合も本人の1ターンとして resume_upload のイベントで会話を続け、AI が読み取れなかった前提で1問返す
  (会話数は読めたときと同じく2つ。再読み込みしても、ファイルの吹き出し → 案内 → 1問 のまま残る)。
- 開発者画面でシナリオを始め直すとそのユーザーの dip の状態が初期に戻るので、アップロードした結果も消える(本番の画面には初期に戻す操作は無い)。
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

AI が差し出す [話して伝える(音声・約5分)] か、入力欄の「+」→「音声で相談する」から、音声アドバイザー(`voice_resume/`。詳しくは [voice_resume/README.md](voice_resume/README.md))と話せる
(旧リポジトリの task 40 を task 62 で移植。task 92 で別タブをやめ、トークと同じ画面のシートで開くようにした)。
音声はトークの上に重ねる通話中のシート(画面下から出る)で話し、タブはまたがない。シートにはマイクの許可・
接続中 / 聞いています / あなたが話しています / 考えています / アドバイザーが話しています の状態と「面談を終える」がある。
面談が終わるとシートが閉じ、同じトークに AI の返信として「音声で話した内容のまとめ」「求人カード」「職務経歴書の下書きを見る」が続く
(システム通知のラベルは出さない。会話に入るので再読み込みしても残り、次のテキストの会話はその続きになる)。
音声で聞いた希望・経験・できること・資格(dip の相談プロファイル8項目)・提案した求人・下書きは、
同じユーザーの dip の状態(長期メモリ)とトークに戻る。次のテキストの会話では、それを要約から読んで確認形で1回だけ触れ、聞き直さない。

ユーザーの対応付けは匿名ユーザーID(上の「マルチユーザー」)。旧リポジトリの `line_user_id`(LINE のユーザー)の代わりに、
トークで使っているユーザー(本番の画面は匿名ID `w-…`、開発者画面は `匿名ID:シナリオユーザー`(例 `w-…:sim-new`))をそのまま音声側へ渡し、
dip の読み書き・結果の返送はすべてこの ID で行う(dip の MCP ツールの引数名は `line_user_id` のまま)。
音声構成は旧リポジトリと同じ(LiveKit Cloud + Deepgram nova-3 + OpenAI gpt-6-sol + Cartesia sonic-3.5)。

```
トーク [話して伝える] / 「+」→「音声で相談する」→ 同じ画面に通話中のシートが開く
   → POST /api/voice/{user}/start(line_runtime。Cookie の匿名IDのユーザーでなければ 403、Cookie なしも 403。
     会話数を1つ使い、上限なら 429)が LiveKit の接続トークンを発行し、participant metadata に user_id を載せる
     (発行は voice_resume/livekit_token.py。LIVEKIT_* が無ければ会話数を使わずに 503)
   → ブラウザ ⇔ LiveKit Cloud ⇔ 音声ワーカー。ワーカーは会話の前に get_user_conversation_summary を読む
     (前に話した希望 stated_preferences と、トークでアップロードした履歴書 uploaded_resume の要点を指示に入れ、確認形で使う・聞き直さない)
   → 会話中: recommend_jobs をそのユーザーID で呼ぶ / 本人が言った希望を save_user_preferences で保存
   → 終わり(締めの挨拶か「面談を終える」): 結果を propose / consult で記録し、POST /api/voice/result で
     話した希望・求人・下書きをこちらへ返す → line_runtime が今の会話に AI の返信として続ける(LLM は呼ばない)
   → シートは開いている間だけ POST /api/voice/{user}/wait(この面談の部屋の結果を最長25秒待って返すのを繰り返す)で待ち、
     届いたら閉じて同じ返信(まとめ・求人カード・「職務経歴書の下書きを見る」= GET /liff/voice/draft)をトークに描く
     (「面談を終える」から2分たっても届かなければ、× で閉じてよいと案内する。あとで届いた結果は会話に入り、開き直すと出る)
```

- 求人カードは、送られてきた `job_id` を dip の求人データと照合してコードで作り直す。dip に無い求人はトークに出ない
  (LINE 側の「dip が返した求人だけをカードにする」決まりと同じ)。
- 「話した内容のまとめ」は、音声で本人が話した希望(dip に保存したのと同じ項目。個人情報らしい値は捨てる)から
  コードで作る(`line_runtime/voice_reply.py`。吹き出しは80字以内)。
- 会話数: 音声相談を始めるとき(接続トークンの発行)に1つ使う。結果をトークに続けるときは使わない。
- 会話結果の記録はコードが propose(求人を提案できた)/ consult(提案なし)だけを書く。
  wait は音声側からは記録しない(断っていない人を wait と記録しない決まり)。
- 画面の証跡: `voice_resume/results/talk-voice-sheet-20260929-*.png`(LiveKit のライブラリを代役にして、
  結果の受け取りからトークへの描画までを通したもの。実際の音声は使っていない)。

動かし方(2プロセス。すべてリポジトリ直下・同じ venv・同じ `.env`。会話ページは line_runtime が配る):

```bash
# リポジトリ直下の .env に(下の表の「音声相談」の必須の5つ + OPENAI_API_KEY)
#   LIVEKIT_URL=wss://<project>.livekit.cloud
#   LIVEKIT_API_KEY=...  LIVEKIT_API_SECRET=...
#   DEEPGRAM_API_KEY=...  CARTESIA_API_KEY=...  OPENAI_API_KEY=...
./.venv/bin/python -m voice_resume.agent download-files     # 初回のみ(VAD / turn detector のモデル)

# 1) トーク(line_runtime。音声相談のシートと接続トークンの発行もここ)
./.venv/bin/python -m line_runtime.server                  # http://127.0.0.1:8030
# 2) voice-resume のワーカー(LiveKit Cloud に接続して待受)
./.venv/bin/python -m voice_resume.agent dev
```

オフラインテスト(実 API なし。台本(文字起こし)駆動の会話の流れ・検索引数・下書き、dip の MCP 結合、匿名ユーザーID のスコープ)は
`./.venv/bin/python -m pytest tests/voice/`(`pytest -q` 全体にも含まれる)。実 API の E2E は `scripts/voice/e2e_audio_session.py`
(費用がかかる。[voice_resume/README.md](voice_resume/README.md))。

→ <http://127.0.0.1:8030>(本番のトーク)または <http://127.0.0.1:8030/dev> でシナリオを選ぶ → 入力欄の「+」→「音声で相談する」
→ 同じ画面のシートで面談(マイク許可。イヤホン推奨)
→ 面談が終わる(締めの挨拶か「面談を終える」)と、シートが閉じて同じトークにまとめ・求人カード・下書きのリンクが続く。
開発者画面では、左の「dip から見た状態」の「本人の希望」「会話の結果」にも反映される。

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

音声相談(voice_resume)で使うもの。キーはリポジトリ直下の `.env` に置く(`voice_resume/config.py` が読む。トークだけ試すなら不要):

| 環境変数 | 既定 | 用途 |
|---|---|---|
| `LIVEKIT_URL` | なし(必須) | LiveKit Cloud のプロジェクト URL(`wss://….livekit.cloud`)。トーク(line_runtime)とワーカーの両方が使う |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | なし(必須) | LiveKit Cloud の API キー。トーク(line_runtime)が部屋ごとの接続トークンを発行し、ワーカーが部屋に入る |
| `DEEPGRAM_API_KEY` | なし(必須) | 文字起こし(Deepgram nova-3、日本語) |
| `CARTESIA_API_KEY` | なし(必須) | 読み上げ(Cartesia sonic-3.5、日本語ボイス) |
| `OPENAI_API_KEY` | なし(必須) | 会話の LLM・履歴書の下書き・会話中の調べもの(gpt-6-sol)。トーク側と共通 |
| `VOICE_RESUME_TALK_URL` | `http://127.0.0.1:8030` | 面談の結果(求人・下書き)を返すトーク(line_runtime)の URL |
| `VOICE_RESUME_OUT_DIR` | `data/voice-resume` | 面談ごとの下書き・文字起こし・求人提案の保存先 |
| `VOICE_RESUME_*`(モデル・ボイス・ターン判定) | `voice_resume/config.py` | STT / LLM / TTS のモデル、ボイスと速さ、ターン判定の待ち時間 |

## 評価(evals/)

`evals/scenarios.json` の9シナリオ(上の「シナリオ」。画面と同じ一覧)を、
設定を与えた模擬ユーザー(LLM)と会話させ、次を採点する。

- 機械判定(`evals/checks.py`): 1ターン1問、選択肢は最大4つで逃げ道を含む、吹き出し80字以内、求人は最大3件で理由つき、
  ツールが返していない求人を出さない、個人情報をトーク経由で送らない、初回は質問で終える、
  「今はいい/送らないで」の後に質問や求人を続けない、「求人を見せて」のターンで求人を探す、この会話で保存した項目を聞き直さない、最後の返信を選択肢なしで終えない(「もう送らないで」の直後は「承知しました」で終えてよい)、断られていないのに wait を記録しない。
  再訪(希望あり)は、1通目で前の希望に触れ、以後その項目を聞き直さない。再訪(行動履歴のみ)は、1通目で最近の行動に触れ、
  触れるのは会話を通して1回だけ、1通目に求人カード・求人名・件数を並べず職種も1つだけ。
  プッシュのシナリオは本番と同じく `push_trigger` を通してから始める。
- 事実性(`fact_violations`、指標 `fabrications`): 返信に出てくる金額・日付・徒歩分数・件数・求人名が、この会話で
  dip から来た情報(ツールの結果・プッシュの根拠の事実・dip の Prompt)かユーザーの発言にあるかをコードで照合する。
  無ければ作り話として違反。数字は同じ単位の値とだけ照合する(徒歩の分数は walk_min、件数は total や件数のリストなど。ID や日付に含まれる数字は根拠にしない)。
  dip の Prompt は同じ単位で書かれた数字だけを根拠にし、例文の日付は根拠にしない。
  その場で作った架空の求人名そのものは文字列では見分けにくいので、ここは実在の求人の出し間違いを捕まえる。
  保存した希望(`save_user_preferences`)が本人の発言に出てこない場合も違反にする(本人が決めていない条件を記録しない)。
  相談モードのシナリオ(`consult_mode`)では、初回の選択肢に「まず相談する」がある、相談の入口で求人を出さずに1問から始める、
  本人が切り上げる前に相談を締めない、相談で聞いた項目を保存する、も見る。
- KPI: 3ターン以内に分岐(提案/相談/支援/待つ)へ到達した割合(相談モードのシナリオは規則を緩めているので測らない)、会話結果を記録した割合、期待どおりの出口の割合。
- 費用と効率(エージェント側の呼び出しだけ): `cost_usd`、`llm_calls`、`cache_hit_rate`(入力のうちプロンプトキャッシュから読めた割合。
  読めた分は 0.1 倍の料金。シナリオとユーザーIDを会話の途中で変えないのは、この割合を保つため)。
- LLM ジャッジ(`--judge` のときだけ): トーン、設計の狙いとの一致、コードで拾えない作り話。gpt-6-sol(anthropic なら Opus 5)を呼ぶので費用がかかる。
  プッシュの根拠の事実もジャッジに渡す(渡さないと本物の事実を作り話と判定する)。

2026-09-27 に CRM 機能(プッシュ配信・休眠掘り起こし・配信停止/抑制)の削除に合わせて、シナリオを 12 件から
新規ユーザー系の 5 件に再編した(jyoka/tasks#56。入口も follow / crm_reply から visit / revisit に変わった)。
続けて再訪の2振る舞いを分けて測るため、`R-revisit-browsing`(再訪・行動履歴のみ)を足して 6 件にした(jyoka/tasks#57)。
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
