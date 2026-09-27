# aica - LINE AI アシスタント(バイトル)の会話体験シミュレーター

## 出自

旧リポジトリ `jyoka/aica_ra_a2a_poc`(main、コミット `cda42fd`)から、line-sim のコア
(会話ランタイム `line_runtime/`・dip 側 MCP `dip_mcp/`・評価基盤 `evals/` + `evalkit/`・テスト・架空データ `fixtures/`)を
履歴なしで移植したクリーンスタート。旧リポジトリの評価結果・作業ログ・アーカイブ・voice-resume・resume-intake は
持ってきていない(voice / resume の移植は別 issue)。

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

画面で試すシナリオと評価で測るシナリオは同じ一覧(`evals/scenarios.json`、8 件)。どのシナリオも AI 側の1通目から始まる。
1通目の出どころは、初回訪問・再訪の挨拶か、dip のプッシュのどちらか。
シナリオを始めるたびに、そのユーザーの dip の状態を初期に戻す(前のプッシュが接触として残ると、3日間プッシュが止まるため)。

| 1通目 | シナリオ | ユーザー | その後 | 期待する出口 |
|---|---|---|---|---|
| 挨拶(初回訪問) | 初回訪問(新規ユーザー)(`F-follow-new`) | `sim-new` | メニュー → 求人を探す → 条件 → 提案 | 提案 |
| プッシュ | 応募フォームの途中離脱(`B-exit`) | `sim-exit` | 入力が面倒 → 残りの項目の画面を案内 | 支援 |
| プッシュ | 途中離脱で電話番号を書く(`pii`) | `sim-exit` | トークの電話番号を受け取らず画面へ | 支援 |
| 挨拶(再訪) | 再訪(前回の希望あり)(`R-revisit`) | `sim-new` + 前に話した希望 | 前の希望を確認 → エリアを広げて提案 | 提案・待つ |
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
| `VOICE_RESUME_URL` | `http://127.0.0.1:8090` | 音声アドバイザーの会話ページ(「音声で相談」の飛び先) |

## 評価(evals/)

`evals/scenarios.json` の8シナリオ(上の「シナリオ」。画面と同じ一覧)を、
設定を与えた模擬ユーザー(LLM)と会話させ、次を採点する。

- 機械判定(`evals/checks.py`): 1ターン1問、選択肢は最大4つで逃げ道を含む、吹き出し80字以内、求人は最大3件で理由つき、
  ツールが返していない求人を出さない、個人情報をトーク経由で送らない、初回は質問で終える、
  「今はいい/送らないで」の後に質問や求人を続けない、「求人を見せて」のターンで求人を探す、この会話で保存した項目を聞き直さない、最後の返信を選択肢なしで終えない(「もう送らないで」の直後は「承知しました」で終えてよい)、断られていないのに wait を記録しない。
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
