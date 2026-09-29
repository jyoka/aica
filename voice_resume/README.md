# voice-resume - 音声で棚卸しして、求人の提案と履歴書の下書きを作る

> 旧リポジトリ `jyoka/aica_ra_a2a_poc`(コミット `cda42fd`)の `voice-resume/` を aica に移植したもの(task 62)。
> パスはリポジトリ直下に合わせてある(パッケージ `voice_resume/`・テスト `tests/voice/`・スクリプト `scripts/voice/`)。
> venv と `.env` は line_runtime と共通。旧リポジトリの `results/`(評価・E2E の記録)は持ってきていない。
> 文中の `results/*.md` の記録は旧リポジトリの `voice-resume/results/` を見る。
> aica での記録は `voice_resume/results/`(`talk-voice-20260927-*.png` はトーク →「音声で相談」→ 結果がトークに戻るまでの画面証跡。
> 音声ワーカーの代わりに面談の終わり(`propose_jobs` → `finalize_interview`)を実コードで通し、LiveKit・STT・TTS・下書きの LLM は使っていない。
> このときはまだ別タブの会話ページだった。`talk-voice-sheet-20260929-*.png` は task 92 の、同じ画面のシートで話して結果がトークに続くまでの証跡。
> LiveKit のブラウザ用ライブラリを代役にし、音声ワーカーの代わりに `POST /api/voice/result` へ結果を送った)。

キャリアアドバイザーと日本語の音声で話しながら、バイト探しの希望条件と経験を5分ほどで聞き取り、
最後に条件に合う求人を最大3件提案する。会話が終わると履歴書の下書き(構造化 JSON + Markdown)と
希望条件・提案した求人IDが `data/voice-resume/` に出る(`VOICE_RESUME_OUT_DIR` で変更可。git には入らない)。

タスク 29 の OpenAI Realtime 構成は、日本語の不自然さ・早口・ヒアリングの深掘りすぎ・
答えを待たない問題があったため、タスク 31 で「文字起こし → 文章の LLM → 日本語に強い TTS」に
分ける構成へ作り直した。

## 構成

```
ブラウザ(マイク) ──WebRTC──▶ LiveKit Cloud(room) ──▶ voice_resume/agent.py(ワーカー)
                                                        ├ STT: Deepgram nova-3(ja)+ バイト用語の keyterm
                                                        ├ LLM: OpenAI gpt-6-sol
                                                        ├ TTS: Cartesia sonic-3.5(日本語ボイス・少し遅め)
                                                        ├ ターン判定: Silero VAD + 多言語 turn detector
                                                        ├ dip: 同じリポジトリの dip_mcp(stdio。同じ venv の python で起動)
                                                        │      recommend_jobs / relax_conditions +
                                                        │      トークから入った相談なら summary の読み取り・希望の保存・結果の記録
                                                        └ 終了時: 下書き+希望+求人ID を data/voice-resume/ へ。
                                                           トーク(line_runtime)に結果(話した希望・求人・下書き)を返す
トーク(line_runtime.server)= 会話の画面(トークの上に重ねる通話中のシート。line_runtime/static/talk-core.js)の配信と、
                              LiveKit 接続トークンの発行(POST /api/voice/start。発行そのものは voice_resume/livekit_token.py)
```

task 92 で、別ポートの会話ページ(旧 `voice_resume/server.py` と `static/index.html`、既定 :8090)をやめ、
トークと同じ画面のシートにまとめた(単一コンテナでの公開に合わせ、line_runtime の配下で配る)。
音声相談はトークから始めるものだけになり、ユーザー不明の単体ページは無い。

- 下書きの JSON の形: `voice_resume/schema.py`(旧リポジトリの `docs/design/resume-draft-schema.md` と同じ。履歴書アップロード機能と共通なので変えない)
- `data/voice-resume/job-proposal-*.json` の形(タスク 35 で「本人の希望」と「検索に使った条件」を分けた):
  - `preferences`: 本人が言った希望だけ。検索のために外した・ゆるめた条件では上書きされない
  - `recommend_args`: 最後の検索に使った条件(ゆるめたものを含む)
  - `search_attempts`: 検索の経緯(使った条件と件数の並び。タスク 35 で追加)
  - `proposed_job_ids` / `jobs`: 提案した求人
- ボイス・速さ・モデル・ターン判定の待ち時間は環境変数で差し替え可(`voice_resume/config.py` 参照。
  例: `VOICE_RESUME_TTS_VOICE` / `VOICE_RESUME_TTS_SPEED=1.0`(既定 0.9 = 少し遅め)/
  `VOICE_RESUME_MIN_ENDPOINTING_DELAY`)
- STT はタスク 37 で nova-2 から nova-3 + keyterm(時給・下限・シフト・品出し・ホール・
  キッチンなどの用語登録)に変更。実会話で「合うと思う」→「アウトも」、「時給の下限」→
  「地球の加減」のような聞き間違いが多かったため、同じ言い回しの台本で候補を比較して決めた
  (比較表: `results/stt-eval-*.md`、比較の再実行: `scripts/voice/eval_stt.py`)。戻すときは
  `VOICE_RESUME_STT_MODEL=nova-2`、OpenAI にするときは `VOICE_RESUME_STT_PROVIDER=openai` と
  `VOICE_RESUME_STT_MODEL=gpt-4o-transcribe` など。用語は `VOICE_RESUME_STT_KEYTERMS`(カンマ区切り)

## ヒアリングの方針

- 全体3〜5分・質問6〜8問。1テーマの深掘りは最大1回。
- 順番: 希望条件(職種・エリア・曜日/時間帯・週の日数・時給)→ 経験 → できること → 資格・免許
  → 要約して確認 → 求人の提案。聞いた希望がそのまま `recommend_jobs` の検索条件になる。
- 0件のときは `relax_conditions` で「どの条件を外すと何件になるか」を調べて具体的に伝え、
  どれをゆるめるかを本人に一度で選んでもらう(「探してもよいですか?」を繰り返さない)。
  ゆるめた条件は `relaxed` として記録し、本人の希望(preferences)は書き換えない。
- STAR のような型・バイト探しに直結しない質問(価値観など)・答えへの解釈や評価はしない。
  自己PRは聞き出さず、下書きの段階で聞いた事実から作る。

## 誠実性の決まり(引き継ぎ)

- 声色・話し方・性格・感情・思考傾向の推論はしない。
- 本人が言ったことと会話で確かめた事実だけを書く。盛らない・作らない。
  曖昧だった点は下書きに入れず「確認したい点」(unconfirmed)に分ける。
- 本人が決めていないこと(職種など)を推測で埋めない。職種を決めていない人には職種なしで検索する。
- 「本人の希望」と「検索に使った条件」を分けて記録する(タスク 35)。見つからないときに
  ゆるめた・外した条件は検索の経緯(`search_attempts`)に残し、本人の希望としては書かない。
- 氏名・住所・電話番号・メール・生年月日は聞かない・保存しない(drafter の PII スクラブつき)。
  基本情報の欄は常に空欄で出力し、本人が記入する。
- dip への書き込みは、ユーザーID が分かる相談(トークから始めた相談)だけ。
  保存するのは本人が言った希望だけで、検索のためにゆるめた条件・推測した職種は保存しない。
  会話結果の記録もコードが propose / consult だけを書く(wait は本人が断ったときだけの記録なので、音声側からは記録しない)。
  ユーザー不明の参加者(metadata なし)は、タスク 31 の決まりのまま読み取りだけで何も書かない。
- 出来上がった下書きは本人が確認・修正してから使う(画面と Markdown にその旨を明記)。

## セットアップ & 実行

前提(環境変数の一覧はリポジトリ直下の README の「音声相談」):
- リポジトリ直下の `.env` に `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` / `OPENAI_API_KEY` /
  `DEEPGRAM_API_KEY` / `CARTESIA_API_KEY`(line_runtime と同じ `.env`)
- venv はリポジトリ直下の `.venv`(`requirements.txt` に voice-resume の依存も入っている)

```bash
# リポジトリ直下で
python3.12 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# 初回のみ: Silero VAD / turn detector のモデルをダウンロード
./.venv/bin/python -m voice_resume.agent download-files

# 1) トーク(line_runtime。音声相談のシートと接続トークンの発行もここ)
./.venv/bin/python -m line_runtime.server                  # http://127.0.0.1:8030

# 2) ワーカー起動(LiveKit Cloud に接続して待受)
./.venv/bin/python -m voice_resume.agent dev
```

→ ブラウザで <http://127.0.0.1:8030> を開く → 入力欄の「+」→「音声で相談する」(または AI が差し出す [話して伝える])
→ トークの上に通話中のシートが開く → マイク許可(イヤホン推奨)→ アドバイザーが日本語で話しかけてくる
(シートには 接続中 / 聞いています / あなたが話しています / 考えています / アドバイザーが話しています と、求人検索などの「何をしているか」が出る)
→ 会話の最後に求人の提案が音声で出る
→ 会話が締まる(または「面談を終える」ボタン)と、下書きが `data/voice-resume/` に保存され、シートが閉じて
  同じトークに「音声で話した内容のまとめ」「求人カード」「職務経歴書の下書きを見る」が AI の返信として続く。

## トークから入る(line_runtime との統合・匿名ユーザーID)

トーク画面(line_runtime)の [話して伝える] /「+」→「音声で相談する」でシートを開くと、トークのユーザーID が
接続トークン(`POST /api/voice/start`。Cookie の匿名IDのユーザーにだけ発行し、他人のユーザーIDは 403・Cookie なしは 401。有効期限5分)の
LiveKit participant metadata(`{"user_id": ...}`)でワーカーまで届く。ユーザーID は、本番の画面ならそのブラウザの匿名ユーザーID
(`w-0123456789abcdef`)、開発者画面ならそれでスコープしたシナリオユーザー(例 `w-0123456789abcdef:sim-new`。リポジトリ直下 README の
「マルチユーザー」)。旧リポジトリの `line_user_id`(LINE のユーザー)をこの ID に置き換えた。このとき:

- 会話の前に dip の要約(`get_user_conversation_summary`)を読み、前に話した希望
  (`stated_preferences`)があれば確認形で1回だけ使う(同じことを聞き直さない。LINE 側と同じ決まり)。
- 同じ要約の `uploaded_resume`(トークでアップロードした履歴書の読み取り結果)があれば、その要点を
  大事な順(資格、読み取れなかった点、できることは先頭5件、学歴は在学中か最終学歴の1行、職歴は新しい順に3件まで・
  やっていたこと等は1件あたり3つまで、職務要約、履歴書の希望)にアドバイザーの指示へ入れる。
  `prompts.RESUME_NOTE_MAX_CHARS`(600文字)に収まる行だけを入れ、入らない行は飛ばして「長いので省略した項目」に名前を書く
  (行の途中では切らない。長い1行のせいで後ろの行まで落とさない)。在学中なら働ける時間帯を確かめる1行を足す。
  決まった項目だけを読むので、氏名・連絡先は指示に入らない(dip 側も保存しない)。書いてある経験・資格は聞き直さず
  確認形で使い、読み取れなかった点は必要なら1問で確かめる。本人が会話で話した希望と食い違えば、会話で話した希望を優先する。
- 最初の挨拶では、履歴書があれば確認形で1回だけ触れる(直近の職歴。職歴が無ければ学歴など)。前に話した希望もあるときは、
  履歴書には触れるだけにして、最初の1問は希望の確認にする。職歴のある履歴書だけなら、最初の1問は「履歴書の経験に近い仕事で探すか」。
  要約が読めないときや、どちらも無いときは従来どおり職種から聞く。
- 求人検索(`recommend_jobs` / `relax_conditions`)はそのユーザーID で呼ぶ(本人の傾向・応募済み除外が効く)。
  dip の MCP ツールの引数名は `line_user_id` のまま(line_runtime と共通の dip の契約)で、値に匿名ユーザーID のスコープを渡す。
- 本人が話した希望は `save_user_preferences` で本人の言葉のまま保存する(経験・できること・資格を
  含む dip の8項目 = 相談プロファイル。同じユーザーの次のテキスト会話の要約に出る)。
- 終わると `record_conversation_outcome`(propose / consult)を記録し、話した希望・提案した求人・下書きを
  トークの `POST /api/voice/result`(`{"user_id", "jobs", "draft_markdown", "files", "preferences", "room"}`)へ送る。
  サーバー間の経路なので、共有鍵を `Authorization: Bearer <AICA_INTERNAL_KEY>` で付ける(line_runtime と同じ値。無いと 401)。
  line_runtime が今の会話に AI の返信(話した内容のまとめ・求人カード・「職務経歴書の下書きを見る」)として続け、
  開いているシートが `POST /api/voice/wait` で受け取って閉じる(常時の問い合わせはしない)。

起動手順と画面の流れはリポジトリ直下の README の「音声相談(voice-resume との連携)」を参照。
トーク側の URL は `VOICE_RESUME_TALK_URL`(既定 `http://127.0.0.1:8030`)で変えられる。

### 音声が途切れる・割り込まれるとき

Realtime 版で「音声が途切れる」問題があったため、この構成では次の対策を入れている:

- スピーカーの音をマイクが拾って「割り込み」扱いになるのが主因。ブラウザ側でエコーキャンセルを
  明示的に有効化し(`echoCancellation: true`)、エージェント側も短い物音では割り込まない
  (`MIN_INTERRUPTION_DURATION=0.8s`)+ 誤割り込みなら発話を自動再開する(`resume_false_interruption`)。
- それでも切れる場合はイヤホンを使う(スピーカー再生が原因の切り分けにもなる)。
- 考え中の「えーと」で打ち切られる問題は、多言語 turn detector と
  `MIN_ENDPOINTING_DELAY=0.8s` / `MAX_ENDPOINTING_DELAY=6s` で「まだ話し中」を待つ。

## 会話中の調べもの

- `web_research`(仕事内容・資格など)と `research_company`(気になる会社)は、聞かれたときに web で調べる(1回 約7〜10秒、$0.02 前後)。
- `wage_average`(平均時給)は、全国と東京都の値を参考データ `voice_resume/data/wage_reference.json` からすぐ答える(会話のたびには調べない)。
  それ以外の地域や職種は、その場で web で調べる。
- 参考データは年1回くらい手で更新する(答えには調査の時期を添えるので、少し古くても誤解されない):

```bash
./.venv/bin/python scripts/voice/update_wage_reference.py   # 実 API、1回 $0.05 前後
```

## テスト

台本(文字起こし)→ 会話の流れ・検索引数・下書きの部分はオフラインでテストする(実 API を呼ばない。
dip の MCP 結合テストと匿名ユーザーID のスコープのテストはローカルの stdio サーバーを起動する):

```bash
./.venv/bin/python -m pytest tests/voice/
```

## マイク無し環境での実会話 E2E

本人役の発話を TTS(gpt-4o-mini-tts)で音声合成し、本番と同じ部品
(Deepgram STT → gpt-6-sol + dip MCP → Cartesia TTS)で1会話を通す。
成果物(文字起こし・下書き・求人提案・アドバイザー音声・費用)は `voice_resume/results/` に残る:

```bash
./.venv/bin/python scripts/voice/e2e_audio_session.py
```

LiveKit の WebRTC 転送とターン判定だけはこの経路では通らないので、上のブラウザ手順(トークのシート)で確かめる。

## 費用の目安(1会話 = 5分)

単価は 2026-09 時点の各社公式 pricing。実測は旧リポジトリの `voice-resume/results/cost-*.md` を参照。

- STT(Deepgram nova-3 streaming): 正価 $0.0077/分 → 5分で **$0.04**(2026-09 時点はセール価 $0.0048/分)
- LLM(gpt-6-sol、$2/$10 per 1M tokens): 往復10回・履歴込みで 3万 in / 2千 out 程度 → **$0.08 前後**
- TTS(Cartesia sonic-3.5): アドバイザー発話 1,500〜2,500文字 → **$0.06〜0.10**(プラン換算 $0.04/1k文字。無料枠 2万文字/月あり)
- 下書き生成(gpt-6-sol): 1回 **$0.03 前後**
- LiveKit Cloud: 無料枠(5,000 接続分/月)内。超過時も数セント/会話

合計の目安: **5分の会話 1回で $0.2 前後**(Realtime 版の半分以下)。
