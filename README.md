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
   │  イベント: トーク / 配信への返信 / 友だち追加 / dip からのプッシュ(許可されたものだけ)
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
      Prompt:   baitoru_assistant(entry=A|C|follow|D|app_status|saved_closing|new_match)  会話の骨格・対話ルール・入口ごとの最初の1問
      Logic:    push_trigger  プッシュしてよいか(未連携・抑制中・直近の接触・根拠の事実)と、話しかける根拠を決める
      データ:   fixtures/(架空の求人63件・求人ベクトル・掲載日・シナリオ用ユーザー7人・FAQ)、会話で変わる状態は data/state.json
```

dip 側のツールは LLM を使わない(速く・決定的に・事実だけを返す)。文面を作るのは LINE 側の LLM だけ。
例外は `recommend_jobs` の keywords の意味検索(embedding)で、言い換え(「人と話さない」「体を動かす」)を部分一致の取りこぼしから救う。
文章は作らず、求人側のベクトルは `scripts/gen_job_vectors.py` で前もって作って `fixtures/job_vectors.json` に保存するので、
検索時の外部 API 呼び出しは問い合わせ文の embedding 1回だけ(同じ問い合わせはプロセス内で使い回す)。
API が使えないときは従来の部分一致に戻る(検索は止めない)。
求人カードは、dip が返した求人(どのツールの結果でも、プッシュの根拠の事実でも)だけをコードで描画する。
モデルが選ぶのは出す求人の ID だけで、ツールが返していない ID はカードにならない。
推薦の各求人には、本人の傾向との合い具合 `match_score`(内訳 `score_breakdown`)が付く(LLM を使わずに計算)。
本人が会話で話した希望は `save_user_preferences` で dip に保存し、次の会話の要約(`stated_preferences`)で返す。長期メモリは LINE 側ではなく dip 側が持つ。

リッチメニューは既存運用のまま触らない前提。「求人を探す / 応募履歴を確認 / 使い方」はメニューではなく、
トーク内の選択肢(クイックリプライ)として出す(挨拶だけ・意図が不明なときの最初の1問)。

### こちらから話しかける(待つだけにしない)

LINE にはユーザーがトークを開いただけで届くイベントがない(Messaging API の Webhook イベント一覧で確認)。
こちらから話しかけられるのは次の2つだけなので、両方を入口にしている。

| 入口 | きっかけ | 費用 | 送ってよいかの判断 |
|---|---|---|---|
| 友だち追加(`follow`) | 友だち追加・ブロック解除 | 応答メッセージ(通数に数えない) | 不要(必ず挨拶する) |
| プッシュ(`form_exit` / `app_status` / `saved_closing` / `new_match`) | dip が検知した行動イベント | プッシュ(通数に数える) | dip の `push_trigger` が決める |

`push_trigger` は、未連携・配信抑制中・直近 3日以内に接触済み(CRM 配信を含む)・話しかける根拠の事実がない、のどれかなら送らない。
3日はシミュレーション用の仮の値。許可したら接触として記録し、根拠の事実(求人・選考状況・掲載終了日・新着)を LINE 側に渡す。
LINE 側は判断せず、受け取った事実を1文目で伝えてから1問聞く。

返信は必ず選択肢で終える(締めくくりの返信にも「求人を探す / 応募履歴を確認 / 使い方」)。
クイックリプライは、ボタンを押すか新しいメッセージが届くまで消えない(LINE の仕様)ので、次に開いた人が書くしかない状態を減らせる。
ただしトークを開き直したときに表示が残るかはドキュメントに記載がなく、実機での確認が必要。

## シナリオ

シナリオごとの会話設計(きっかけ・1通目・最初の1問・分岐・出口・評価で確かめること)は
[docs/design/line-sim-scenarios.md](docs/design/line-sim-scenarios.md)。

画面で試すシナリオと評価で測るシナリオは同じ一覧(`evals/scenarios.json`、12 件)。どのシナリオも AI 側の1通目から始まる。
LINE で AI から先に話せるのは、友だち追加への応答・dip のプッシュ・dip の CRM 配信だけなので、1通目の出どころはこの3つのどれか。
ユーザーが配信に返信するシナリオは、dip の配信(LLM なし、`crm_broadcast`)を1通目に出し、その返信から LINE 側 AI が会話する。
シナリオを始めるたびに、そのユーザーの dip の状態を初期に戻す(前の配信・プッシュが接触として残ると、3日間プッシュが止まるため)。

| 1通目 | シナリオ | ユーザー | その後 | 期待する出口 |
|---|---|---|---|---|
| 友だち追加 | はじめて友だち追加(`F-follow-new`) | `sim-new` | メニュー → 求人を探す → 条件 → 提案 | 提案 |
| 友だち追加 | ブロック解除(`F-follow-back`) | `sim-browsing` | 最近の閲覧に1つ触れる → 未経験で大丈夫か | 相談・提案 |
| 友だち追加 | 抑制中の人のブロック解除(`suppressed`) | `sim-stopped` | 挨拶とメニューだけ(求人は勧めない) | 待つ・停止・相談 |
| プッシュ | 応募フォームの途中離脱(`B-exit`) | `sim-exit` | 入力が面倒 → 残りの項目の画面を案内 | 支援 |
| プッシュ | 途中離脱で電話番号を書く(`pii`) | `sim-exit` | トークの電話番号を受け取らず画面へ | 支援 |
| プッシュ | 傾向に合う新着求人(`P-new-match`) | `sim-yoru` | 求人カード → 保存 | 提案 |
| プッシュ | 保存した求人の掲載終了(`P-saved-closing`) | `sim-browsing` | 未経験で大丈夫か | 相談・待つ・提案 |
| プッシュ | 応募先から選考の連絡(`P-app-status`) | `sim-applied` | 面接の不安 | 相談・支援 |
| プッシュ | 保存した求人について質問する(`Q-job-question`) | `sim-browsing` | 詳しく聞きたい → 制服・資料に無いこと | 相談・待つ・提案 |
| dip の配信 | 配信への返信(休眠)(`C-dormant`) | `sim-dormant` | 「また探し始めた」→ まだ考え中 | 待つ |
| dip の配信 | 配信を断る(`stop`) | `sim-browsing` | 「今はいい」→「もう送らないで」 | 待つ・停止 |
| dip の配信 | 再訪(前回の希望あり)(`R-revisit`) | `sim-new` + 前に話した希望 | 「また探したい」→ 前の希望を確認 | 提案・待つ |

シナリオ用ユーザー(`fixtures/users.json`):

| ユーザー | 状況 |
|---|---|
| `sim-yoru` | 7月に新宿の居酒屋に応募(不採用)、最近は夜の飲食を閲覧 |
| `sim-exit` | 2時間前にコンビニ夜勤の応募フォームで離脱(残り: 電話番号・希望開始日) |
| `sim-dormant` | 7月にカフェへ応募して辞退、以後休眠 |
| `sim-applied` | 9/19に居酒屋へ応募、店舗からの連絡待ち |
| `sim-browsing` | カフェ・アパレルを閲覧・保存、応募なし。保存したカフェは 9/25 で掲載終了 |
| `sim-stopped` | 9/10に「もう送らないで」→ 10/10まで抑制中 |
| `sim-new` | dip ID 未連携 |

時刻は 2026-09-23 12:00 に固定(`SIM_NOW` で変更可)。

## 動かし方

```bash
# リポジトリ直下で
python3.12 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
# リポジトリ直下の .env に OPENAI_API_KEY=...(LINE_SIM_PROVIDER=anthropic なら ANTHROPIC_API_KEY=...)

./.venv/bin/python -m line_runtime.server   # http://127.0.0.1:8030
./.venv/bin/python -m pytest -q             # dip 側のオフラインテスト(MCP 往復を含む、LLM 不要)
./.venv/bin/python -m evals.run             # シナリオ評価(模擬ユーザー × 12シナリオ、実 API を使うので費用がかかる)
./.venv/bin/python -m scripts.gen_jobs      # 架空求人を作り直す(fixtures/jobs.json。後で gen_job_vectors も回す)
./.venv/bin/python -m scripts.gen_job_vectors  # 求人ベクトルを作り直す(fixtures/job_vectors.json。求人データを変えたら)
```

画面は3列。
- 左: ① シナリオを選ぶ(1通目の出どころ = 友だち追加 / dip のプッシュ / dip の配信 でまとめた一覧。押すとその1通目から始まる)→
  ② dip から見た状態(状況・抑制・応募・保存・本人の希望・会話の結果)→ ③ やり直す(このシナリオを最初から)。
  シナリオは `POST /api/scenarios/{id}/start` で始める(そのユーザーの dip の状態を初期に戻し、配信から始まるものは配信の中身を返す)。
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
  wait / stop(配信を止める)は音声側からは記録しない(断っていない人の配信を止めない決まり)。

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

`evals/scenarios.json` の12シナリオ(上の「シナリオ」。画面と同じ一覧)を、
設定を与えた模擬ユーザー(LLM)と会話させ、次を採点する。

- 機械判定(`evals/checks.py`): 1ターン1問、選択肢は最大4つで逃げ道を含む、吹き出し80字以内、求人は最大3件で理由つき、
  ツールが返していない求人を出さない、個人情報をトーク経由で送らない、初回は質問で終える(配信に「今はいい」と返されて始まった会話は除く)、応募済みの人に求人を出さない、
  「今はいい/送らないで」の後に質問を続けない、最後の返信を選択肢なしで終えない(「もう送らないで」の直後は「承知しました」で終えてよい)、断られていないのに wait/stop を記録しない。
  プッシュのシナリオは本番と同じく `push_trigger` を通してから始める。配信から始まるシナリオは、配信を1通目として会話ログに入れ、
  配信の中身(本文・求人カード)も dip から来た情報として事実性の照合に使う。
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

2026-09-24 にシナリオを 15 件から 11 件にまとめ(その後、案件単位の FAQ の `Q-job-question` を足して 12 件)、どれも AI 側の1通目から始まる形にした(ユーザーが先に書く入口 A だけで
始まるシナリオは無くなった。入口 A の Prompt は本番の入口として残している)。それより前の結果とは、
ID が消えたもの(A-yoru・A-new・B-greeting・D-applied)や始まり方が変わったもの(stop・suppressed・R-revisit)があるので、
evalkit の diff で直接比べない。

既定は各シナリオ1回。変更の前後を比べるときは `--repeat 3` などで繰り返す(1回だけだとばらつきと区別がつかない)。集計は 95% 区間つきで出す。
費用を抑えるには `--only <シナリオ>` で対象を絞る。LLM ジャッジは既定でオフ(`--judge` で有効)。結果は1回ごとに書き出すので、途中で止めてもそこまでの結果は残る。
実行エラーになった回は失敗として数えず、件数を別に出す。

結果は `evals/results/sim-*.md`(集計 + 会話ログ)と `sim-*.json`(evalkit 形式)。変更の前後を比べるには:

```bash
python3 evalkit/aggregate.py diff evals/results/sim-前.json evals/results/sim-後.json
```

差がノイズ幅(95%)を超えた指標だけ「改善/悪化」と判定する。
