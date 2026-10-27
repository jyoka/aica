# アルバイト探しチャットエージェント PRD v4（aica 移行用）

| 項目 | 内容 |
|---|---|
| 版 | v4.1（2026-10-27 JST。05 実装準備度チェックの指摘42件を反映） |
| 前版 | v3（`PRD_baito_agent.md`・`harness_v3.py`・`bank.py`・`prompt_review_ja.txt`。いずれも変更せずに残す） |
| 規範（正） | **本書**、および本書が規範付録として指定する2つのデータファイル（付録A `/workspace/baito_harness/bank_v4.py`、付録B `/workspace/baito_harness/catalog_v4.py`） |
| 参照実装 | `harness_v4.py`・プロンプト4ファイル（`prompt_*_v4_ja.txt`）。本書に従う実装の一例であり、規範ではない |
| 検証 | `unit_checks_v4.py`（131件）・`sim_v4.py`（20ペルソナ。`python3 simulate.py v4` で実行）。どちらも `BAITO_HARNESS=<モジュール名>` で移植版に差し替えられる |
| 反映したレビュー | 01〜04（§15-1〜§15-5）、05 実装準備度チェック B-1〜B-12・M-1〜M-20・N-1〜N-10（§15-6） |
| 未確定事項 | §12-2（矛盾の扱い）と §16 の確認事項。いずれも既定値を定めてあり、確認の結果を待たずに実装できる |

---

## §0 この文書の読み方

### 0-1 基本原則と優先順位
- **何を聞くか・いつフェーズを切り替えるか・いつ会話を終えるかはハーネス（コード）が決める。どう言うか（口調・リアクション）だけをプロンプト（LLM）が決める。**
- LLM の出力は常に「提案」として扱い、ハーネスが検証してから状態に反映する。LLM が状態を直接書き換える経路は存在しない。
- **優先順位は「本書 ＞ 規範付録のデータ（付録A・付録B）＞ 参照実装・プロンプトファイル」である。** 参照実装・プロンプトが本書と食い違う場合は本書を正とし、参照実装をバグとして直す。本書の本文と付録データが食い違う場合は本文を正とし、付録データを直す。
- 規範付録は「本書の一部」として扱う。ファイルの場所と役割は付録A・付録B に記す。付録以外のファイル（`harness_v4.py` など）を読まなくても実装できるよう、挙動を決める規則・正規表現・定数・固定文はすべて本文に書く。
- 本文中の次の3つのブロックは機械可読であり、`unit_checks_v4.py` 節18 が参照実装と一致することを検査する。
  - `<!-- REGEX:BEGIN -->` 〜 `<!-- REGEX:END -->`：正規表現（§5-8）
  - `<!-- CONST:BEGIN -->` 〜 `<!-- CONST:END -->`：定数（§8-1）
  - `<!-- TEXT:BEGIN -->` 〜 `<!-- TEXT:END -->`：ハーネスが所有する固定文（§10-5）
- 本書の数値（ターン数・件数など）は §11-4 の再現コマンドで再生成できる。§11-5 の基準値は「本書に一致する参照実装」の実行結果であり、本書の規則を変えたときは必ず再生成する。本書の規則と矛盾する基準値は無効である（P4 の基準にも使わない）。

### 0-2 用語
| 用語 | 定義 |
|---|---|
| ターン | ユーザーの発言1通をハーネスが `turn()` で処理すること。数えるか否か、どのフェーズに数えるかは §5-3 で定める |
| フェーズ内ターン数 | `phase_turns[p]`。§5-3 の「計上先」の規則で加算する。全フェーズの合計は常に `turns` と等しい |
| スロット | 条件表の1項目（例：`days_per_week`）。定義は §6 |
| 重要度 | HARD（=3、ゆずれない条件）／MID（=2）／LOW（=1）。セグメントごとに決まる（§6-2）。カード・`slots_json` では `tier` として `hard`／`mid`／`low` と書く |
| 種別 | fact（事実。年齢・学校の許可など。「こだわりなし」を受け付けない）／pref（好み） |
| 確信度 | `high`／`low` の2値。ハーネスだけが決める（§7-2）。LLM が返した確信度は無視する |
| `from` | 値の出どころ。決まった値の一覧から付ける（§7-3） |
| セグメント | `student`／`homemaker`／`side_job`／`other`、および未判明を表す `unknown`（§3） |
| 未成年モード | 年齢が 18歳未満・未回答・未取得のときに有効になる安全モード（§10-1） |
| 空 | スロットに値が無い（`None`・空リスト・空文字）こと。確信度 `low` の値は「空」ではない |
| 不足 | 空、または確信度 `low` であること（`gap_weight` > 0。§8-3） |
| 未定 | 値が無い、または矛盾が解けないまま結果に進んだ状態。結果文で必ず明示する（§7-6・§8-7） |
| 推測 | 確信度 `low` の値。カードでは「（推測）」、結果文では「推測：…」と表示する（§7-6・§8-7） |
| （どっち？） | 同じスロットに `low` 同士の異なる値が入った状態（矛盾）。カードで候補を並べる |
| カード | review フェーズで見せる条件のまとめ（§8-6） |
| 結果 | おすすめ最大3件と注意文・推測・未定の一覧からなる最終出力（§8-7） |
| 規範付録 | 付録A（質問バンク）・付録B（職種カタログ）。本書の一部として扱うデータファイル |

---

## §1 背景
- v3 は「条件表＋質問バンク＋スコアで次の質問を選ぶ」方式を採用し、シミュレーション上は 14〜15ターンで結果まで到達した。
- しかし、4本のゼロコンテキスト・レビュー（01〜04）で次の問題が指摘され、実装準備度判定は NO-GO であった。
  - 1ターンの処理（抽出→判断→生成）の境界と、ボタン押下時の処理主体が曖昧。
  - 確信度を LLM が自己申告しており、ハーネスが検証していない。
  - LLM 出力が壊れたときの検証・再試行・代替処理が未定義で、実際に `KeyError('confidence')` でクラッシュする。
  - 18歳未満・年齢不明・中学生以下への法令上の配慮（深夜・危険作業・就労不可）が未実装。
  - HARD 条件が空のまま推薦が出ることがある。
  - プロンプト変数・カード・結果の JSON 形式が未定義で、aica へ移植できない。
- v4 初版はこれらを解消したが、05 実装準備度チェック（本書だけを読んで判定）で NO-GO となった。理由は、挙動を決めるデータ・正規表現が本書に無いこと、境界の事象（review 中の失敗、脱線後の再提示、15ターン到達、結果後の発言）の規則が無いこと、完了の定義が移植版で実行できないことである。v4.1 はこれらを本文・規範付録・機械可読ブロックで解消した（§15-6）。

---

## §2 目標と非目標

### 2-1 目標
| ID | 目標 | 判定方法 |
|---|---|---|
| G1 | 15ターン以内に結果（おすすめ最大3件）を出す | §4 指標 M-turns・M-over |
| G2 | HARD 条件は「確定」「推測として結果文に明示」「未定として結果文に明示」のいずれかになる。黙って空・推測のまま推薦しない | §4 指標 M-silent・M-silent-low |
| G3 | 18歳未満・年齢未回答では深夜・危険作業・18歳以上限定の仕事を推薦しない。中学生以下には推薦を出さない | §4 指標 M-minor |
| G4 | LLM の出力やハーネス内部の不具合があっても会話が止まらない（例外で落ちない・無限ループしない） | §4 指標 M-crash・M-loop |
| G5 | 推測した値はユーザーがカードで確認・訂正できる | §8-6 |
| G6 | おすすめは職種カタログ（付録B、許可リスト）の中からだけ選ぶ | §4 指標 M-result |

### 2-2 非目標
- 実在の求人検索・応募代行・企業名の提示。
- 扶養・税金などの金額計算や具体的な金額の提示（ルールの存在だけを固定文で伝える）。
- 法律相談。注意文は一般的なルールの目安であり、`rules_only` の固定文を添える。
- 職種カタログ（付録B、24件）以外の職種の推薦。

---

## §3 対象ユーザーとセグメント

### 3-1 セグメント
| 値 | 表示名（`SEGMENT_JA`） | 例 | 固有の HARD |
|---|---|---|---|
| `student` | 学生 | 高校生・大学生・専門学生 | `school_permission` |
| `homemaker` | 家のこと中心（主婦・主夫など） | 子育て・介護中など | `available_hours`・`sudden_leave` |
| `side_job` | 本業あり副業探し | 会社員・公務員 | `side_job_allowed`・`weekday_or_weekend` |
| `other` | フリーター・求職中・その他 | フリーター・求職中・回答しない | なし（共通のみ） |
| `unknown` | 未判明 | 会話開始時のみ | なし（共通のみ） |

- 年齢（`age_band`）はすべてのセグメントで共通の HARD である。
- 家庭に関する質問文は性別・家族構成を前提にしない中立な表現にする（例：「家族の用事や体調で急に休むことはある？」）。
- **セグメント質問 `q_segment` の文言（規範）**：質問文は「今のあなたに近いのは？」、選択肢はこの順で「学生」「家のこと中心（主婦・主夫など）」「本業あり副業探し」「フリーター・求職中・その他」の4つである（§10-5 の `Q_SEGMENT_CHOICES` でも検査する）。各選択肢の `choice_effects` は `{"__segment__": "student"}`・`homemaker`・`side_job`・`other` の順に対応する。

### 3-2 セグメントの決まり方と変更
| 入力 | 現在が `unknown` | 現在が既知（別の値） |
|---|---|---|
| `q_segment` のボタン | 即時に設定 | 即時に切り替え |
| 確認質問（`confirm_segment`）のボタン／自由入力 | 即時に設定 | 即時に切り替え |
| 自由入力から抽出 | 即時に設定（`from` は自由入力系） | **保留**（`pending_segment`）。次の提示で確認質問を出す |
| review での訂正 | 即時に設定 | 保留→確認質問（ただし §5-2 で次が review・result なら確認質問を出さない） |

- 確認質問の文言は `CONFIRM_TEXT`（「確認させてね！今のあなたに近いのはどれ？」）、選択肢は `q_segment` と同じ4択である。
- **一度判明したセグメントは `unknown` に戻さない。** LLM が `unknown` や定義外の値を返した場合は無視してログ（`invalid_segment`）に残す。
- **確認質問の計上先（05-B4・B5）**：保留がある状態で次の提示を決めるとき、ハーネスはまず §5-2 で「次のフェーズ」を求める。次のフェーズが review・result 以外なら、`phase` をその次のフェーズに進めてから確認質問を出す（確認質問への返答はそのフェーズに計上される。これにより確認質問がフェーズ上限を超えさせることはない）。次のフェーズが review・result なら確認質問は出さず、保留を消してログ `confirm_skipped` を残す（カードの【あなた】行で訂正できる）。
- **確認質問の失敗（05-B8）**：確認質問への返答から抽出が2回とも失敗した場合、または抽出の `segment` が null・定義外だった場合は、現在のセグメントを維持し、保留を消す（ログ `confirm_unparsed`、または何も残さない）。いずれもターンには数える（1回目の失敗の再試行は確認質問では行わない）。確認質問では聞き返し（clarify）を行わない。

### 3-3 切り替え時のスロットの扱い
- 新しいセグメントでも有効なスロット（共通スロット、および `coworker_age` のように複数セグメントに属するスロット）は退避しない。新しいセグメントで無効になるスロットだけを `archived[旧セグメント]` に退避し、現在のスロットからは外す（矛盾の記録も外す）。
- 元のセグメントに戻った場合、退避したスロットを**統合規則（§7-5）を同じ `from`・確信度で再実行して**復元する。ただし現在値がある（空でない）スロットは復元しない（現在値を優先）。未成年の深夜除去などのルールはこのとき改めてかかる（05-M7）。
- 未判明の間に入力された、まだ有効でないスロットは `pending` に置き、該当セグメントに切り替わった時点で統合規則を同じ `from`・確信度で再実行して適用する。
- 結果の時点で `pending` に残っている値は推薦に使わない。`pending` に置かれた値は §4 の「取りこぼし」には数えない。

---

## §4 成功指標（式と許容値）
すべての指標は `sim_v4.py` の `metrics()` が出力し、`results_v4.json` の `v4`（ペルソナ別）と `v4_agg`（集計）に保存される。ペルソナの台本は質問 ID をキーにした応答であり、台本に無い ID には決定的な既定応答（選択肢のうち `crc32(ペルソナ名＋質問ID)` で選んだもの）を返す。

### 4-1 合否指標（1件でも出たら不合格）
| ID | 名前（キー） | 式 | 許容値 |
|---|---|---|---|
| M-crash | `crash` | 例外で終了したペルソナ数 | 0 |
| M-loop | `loop` | 「同じ（質問ID・モード・ターン数）の提示が5回以上」または「1会話で60回を超える処理」のペルソナ数 | 0 |
| M-reach | `not_reached` | 結果に到達しなかったペルソナの一覧 | 空 |
| M-minor | `minor_violation` | 未成年モードで、時間帯に深夜が残る・`derived_ng` に深夜が無い・18歳以上限定／重労働／深夜専用の仕事を推薦、のいずれかの件数＋「未成年なのに未成年モードにならない」件数 | 0 |
| M-result | `result_violation` | 推薦数≠`need`、カタログ外 ID、NG に当たる仕事、`blocked` なのに推薦、の件数 | 0 |
| M-silent | `silent_empty_hard` | 結果時点で**空**の HARD のうち、結果文の未定一覧（`unknown_hard_slots`）に無いものの件数（`blocked` を除く） | 0 |
| M-silent-low | `silent_low_hard` | 結果時点で確信度 `low` の HARD のうち、結果文の推測一覧（`guessed_hard_slots`）に無いものの件数（`blocked` を除く） | 0 |
| M-drop | `dropped` | 抽出した正しい更新のうち、保存・保留・退避・ルールによる却下（ログあり）のどれにもならなかった件数 | 0 |
| M-irrel | `irrelevant` | セグメント固有の質問を、その時点のセグメントと異なるユーザーに出した件数 | 0 |
| M-waste | `wasted` | 提示時点で対象スロットの不足度（§8-3 の `gap_weight`）がすべて 0 の質問の件数 | 0 |
| M-repeat | `repeated` | 同じ質問をモード `ask` で2回以上出した件数（`reask`・`aside`・`clarify`・`retry` は除く。やり直し前の会話も除く） | 0 |
| M-spam | `spam_hard_high` | HARD が、他の質問への自由入力（`free_other`。fact スロットを除く）や、シナリオ・逆指名・アイスブレイクのボタンから `high` になった件数 | 0 |
| M-phase | `phase_turns_mismatch`・`phase_cap_violation` | フェーズ内ターン数が §5-3 の計上規則と一致しない件数、`PHASE_CAP` を超えた件数、合計が `turns` と一致しない件数（やり直し前の会話を除く） | 0 |
| M-over | `over_max_turns` | `turns` が 15 を超えたペルソナ数 | 0 |
| M-turns | `max_turns` | 全ペルソナの最大ターン数 | 15 以下 |

### 4-2 品質指標
| ID | 式 | 許容値（v4 基準値からの幅） | P4 の合格条件か |
|---|---|---|---|
| Q-turns | 通常ペルソナ8人それぞれのターン数 | 15 以下、かつ基準値（§11-5）±1 | はい |
| Q-scenario | 通常ペルソナのシナリオ（scenario_game）ターン数 | 各 3 以上 | はい |
| Q-mid | 通常ペルソナ8人の、結果時点で空の MID の合計 | 11 以下（基準値 9＋2） | はい |
| Q-review | review の巡回数 | 各 2 以下 | はい |
| Q-llm | `malformed` ペルソナ：修復・再試行・代替の件数 | 結果に到達すること（件数は記録のみ） | いいえ（記録のみ） |
| Q-pre | review 前（最初のカード時点）で空、または `low` の HARD | 記録のみ | いいえ |

P3 まで（参照実装の開発中）は §4-2 を「目安」として扱い、逸脱したら原因を調べる。P4（移植版の受け入れ）では「はい」の4項目を合格条件にする（05-M18）。

---

## §5 会話フローとターン

### 5-1 フェーズ
順番は `icebreak → hard_filter → scenario_game → reverse_pick → fill_gaps → review → result` で固定である（`ORDER`）。直接質問フェーズ（`DIRECT`）は `hard_filter` と `fill_gaps` の2つである。

| フェーズ | 目的 | 典型ターン数 | 主な質問 |
|---|---|---|---|
| icebreak | 打ち解ける・セグメントを知る | 2 | `q_money`（目的）、`q_segment`（セグメント。未判明のときだけ） |
| hard_filter | HARD を直接聞く | 1〜6 | `hf_age` を必ず最初に聞く。以降はスコア順 |
| scenario_game | 好み（MID/LOW）を「どっち派？」で聞く | 3〜4 | `sc_*`。HARD と fact は書かない |
| reverse_pick | NG（やりたくない仕事）を聞く | 1 | `rp_*`（セグメント別。other/unknown は `rp_general`） |
| fill_gaps | 残った HARD を言い換えて再質問し、空の MID を質問する | 1〜2 | `fg_*`、および `hf_*` の言い換え（`alt_text`） |
| review | カードで確認・訂正 | 1〜2 | カード＋締めの一文 |
| result | おすすめと注意文 | 0（数えない） | — |

典型ターン数は §11-5 のシミュレーション結果の範囲であり、上限ではない。上限は §5-2 と `PHASE_CAP`（§8-1）だけで決まる。

### 5-2 フェーズ遷移の判定（`next_phase`。上から順に評価し、最初に当てはまったものを採用する）
| 順 | 条件 | 遷移先 |
|---|---|---|
| 1 | `blocked`（中学生以下）である | result |
| 2 | 現在が result | result（終了） |
| 3 | `turns` ≥ 15（`MAX_TURNS`） | result（**15ターンの絶対上限**。review 中・2巡目が必要でも result に進む。05-B6） |
| 4 | 現在が review | 「2巡目が必要（§8-6）」かつ `review_rounds` < 2 かつ `show_result` を受けていない なら review、そうでなければ result |
| 5 | `turns` ≥ 13（= 15 − `REVIEW_RESERVE`）、または `show_result` を受けた | review |
| 6 | icebreak：セグメントが判明した、またはこのフェーズで3ターン使った | hard_filter（そうでなければ icebreak） |
| 7 | hard_filter：このフェーズで6ターン使った、または「まだ1度も直接聞いていない不足の HARD」が無い | scenario_game（そうでなければ hard_filter） |
| 8 | scenario_game：このフェーズで6ターン使った、または「3ターン以上 かつ `turns` ≥ 10（= 15 − 2 − 3）」 | reverse_pick（そうでなければ scenario_game） |
| 9 | reverse_pick | fill_gaps |
| 10 | fill_gaps：残りの不足（§8-3 の `remaining_gaps`）が無い | review（そうでなければ fill_gaps） |

- 「このフェーズで使ったターン」は `phase_turns[フェーズ]` であり、§5-3 の計上規則で数える。
- 判定したフェーズで出せる質問が無い場合（`pick_question` が何も返さない）は、`ORDER` の次のフェーズへ進めて再判定する。この繰り返しは最大 `len(ORDER)+2` 回で打ち切り、打ち切った場合は review に進む。したがって無限ループしない。
- 次の提示を決める直前に、セグメントの確認が保留中（`pending_segment`）であれば、§3-2 の規則で確認質問を出すか、出さずに保留を消す。
- 「不足の HARD」は値が空か確信度 `low` のものを指す。hard_filter では各 HARD を1回ずつ直接聞き、2回目（言い換え）は fill_gaps で行う。1スロットあたりの直接質問は最大2回（`MAX_ATTEMPTS`）である。
- `q_segment` はセグメントが `unknown` のときにしか出さない。自由入力で先に判明した場合は出さない。

### 5-3 ターンの定義と計上先（05-B5・B6・B7・N10）
「計上先」は、そのターンで `phase_turns` のどのキーを +1 するかを表す。**計上先は発言が届いた時点のフェーズ（`state.phase`）である。** 例外は脱線・危険話題の数える分で、これは疑似キー `aside` に計上する（フェーズ上限を消費させないため）。どの場合も `turns` と同時に +1 するので、`sum(phase_turns.values()) == turns` が常に成り立つ。

| 事象 | `turns` | 計上先 | 備考 |
|---|---|---|---|
| 会話開始（`start`）・再開（`resume`） | 数えない | — | 冪等。何回呼んでも同じ質問を返す |
| 空の発言（NFKC 後に空文字） | 数えない | — | 何も処理せず、現在の提示をそのまま返す（LLM を呼ばない。ログ `empty_input`） |
| 通常の回答（ボタン・自由入力） | +1 | 現在のフェーズ | |
| 確認質問（`confirm_segment`）への回答 | +1 | 現在のフェーズ | 確認質問を出す時点で `phase` を「次のフェーズ」に進めてある（§3-2）。上限に達したフェーズへ確認質問を計上することはない |
| 聞き返し（clarify） | +1 | 現在のフェーズ | 同じ質問に `CLARIFY_TEXT` を付けて再提示する。条件は §5-5 |
| 聞き返しの条件を満たさない clarify | +1 | 現在のフェーズ | 通常の回答として処理し、ログ `clarify_suppressed` を残す |
| 抽出失敗（LLM が2回とも使えない出力）の1回目 | 数えない | — | 同じ質問を `RETRY_TEXT` 付きで再提示する（`retry`）。質問は消費しない |
| 同じ質問での抽出失敗の2回目 | +1 | 現在のフェーズ | 回答なしとして先へ進む（ログ `extract_gave_up`） |
| 脱線（aside）・危険話題（danger）の1〜3回目 | 数えない | — | 2種類を合わせて会話全体で3回（`MAX_ASIDES`）。§5-6 |
| 脱線・危険話題の4回目以降 | +1 | `aside` | 同じ質問に戻る。ただし加算後に `turns` ≥ 15 なら result に進む |
| やり直し（restart） | 数えない | — | 状態を初期化する。`restarts` だけ引き継ぐ |
| review（カード）への返答 | +1 | review | 1巡ごとに数える。返答が読めなかった場合は §5-5 |
| 結果の出力 | 数えない | — | 直前の返答のターンに含まれる |
| 結果を出した後の発言 | 数えない | — | §5-7。例外は中学生以下判定の解除だけ |

- ターン上限は 15（`MAX_TURNS`）である。13ターン目の処理後に review へ移るので、review が2巡しても 15 を超えない。脱線の数える分で 15 に達した場合は §5-2 の順3で直ちに result に進む。
- フェーズごとの上限は `PHASE_CAP`（icebreak 3・hard_filter 6・scenario_game 6・reverse_pick 1）である。§5-2 の判定と §5-5 の聞き返し条件により、どのフェーズの `phase_turns` も上限を超えない。fill_gaps と review には `PHASE_CAP` を設けない（fill_gaps は `MAX_ATTEMPTS` と予備ターンで、review は `MAX_REVIEW_ROUNDS` で抑える）。

### 5-4 1ターンの処理（3段構成と分岐の順序）
`turn(state, user_text, bank, llm, catalog=None)` は1通の発言を処理し、`{reply, view, done, counted}` を返す。**例外は外に出さない。** ハーネス内部で例外が起きた場合は、ターン開始時に取った状態のスナップショットに戻し、ログ `internal_error` を残し、現在の提示をそのまま返す（`counted=false`。結果済みなら保存済みの結果文を返す）（05-M15）。

処理は次の順に行う。
1. 結果を出した後なら §5-7 の処理をして終える。
2. 空の発言なら、数えずに現在の提示を返す。
3. **前処理**：発言を NFKC 正規化して `DANGER_RE`（§5-8）に当たれば danger として §5-6 の処理をして終える。抽出も生成も行わない。
4. **Stage1 抽出**：発言が提示中の選択肢と完全一致（NFKC 後）すれば**ボタン**として扱う。複数選択（`multi`）の質問では、`MULTI_SEP_RE` で区切った各部分がすべて選択肢と完全一致するとき、複数ボタンとして扱う（重複は1つにまとめる）。完全一致を区切りより先に判定するので、選択肢自体に区切り文字を含んでいても誤分割しない。ボタンでなければ自由入力として `llm.extract(payload)` を呼び、`validate_extract()` で検証する（§8-4）。
5. 中学生以下の文字列による補完（§10-1）：提示中の質問の対象（`targets`）に `age_band` を含み、発言が `JHS_RE` に当たり、抽出結果に `age_band` が無い場合に限り、`age_band=under_15`（根拠は発言全体）を抽出結果に加える。
6. 意図による分岐：`restart` → やり直し、`danger` → §5-6（danger）、`aside` → §5-6（aside）。
7. ターンを +1 する（§5-3）。
8. **Stage2 判断**：提示中のモードに応じて処理する。
   - `review`：§8-6 の返答処理。
   - `confirm`：ボタンなら `choice_effects` を適用、自由入力なら抽出結果の `segment` を確認として適用する。どちらでも保留を消す。抽出失敗は §3-2。
   - それ以外：ボタンなら `choice_effects` を適用する。自由入力なら、抽出失敗は §5-3 の表、`clarify` は §5-5、それ以外は §7 の統合規則で反映する。
   - 意図が `show_result` なら `flags` に `show_result` を加える（§5-2 の順4・5）。
9. 次の提示を決める（§5-2・§8-3）。
10. **Stage3 生成**：`view`（§8-5）を渡して `llm.generate(view)` を呼び、`validate_reply()` で検証する（§8-4）。結果フェーズでは `validate_result()` で検証する（§8-7）。

| 段 | LLM 呼び出し |
|---|---|
| Stage1 | 自由入力のときだけ1回（失敗時は再試行1回） |
| Stage2 | 使わない |
| Stage3 | 1回（失敗時は再試行1回）。危険話題・空の発言・抽出失敗1回目以外の決定的な再提示、`need`=0 の結果では呼ばない |

- 1ターンあたりの LLM 呼び出しは、ボタンなら最大2回（生成と再試行）、自由入力なら最大4回である。
- 注意文（§10-5）は生成した返事の**前に**ハーネスが付ける。LLM に注意文を書かせない。

### 5-5 聞き返し・抽出失敗・review の失敗（05-B5・B8）
**聞き返し（clarify）**は、次の条件をすべて満たすときだけ行う。
- 提示中のモードが `confirm` でない。
- その質問でまだ聞き返していない（`clarified` に無い）。
- 会話全体の聞き返し回数 `clarify_used` < 2（`MAX_CLARIFY`）。
- 現在のフェーズに上限があれば `phase_turns[現在のフェーズ]` < `PHASE_CAP[現在のフェーズ]`（今回の計上後の値で判定する）、かつ `turns` < 13（予備ターンを侵さない）。

条件を満たさない clarify は、通常の回答として抽出結果をそのまま反映する（ログ `clarify_suppressed`）。

**抽出失敗の再提示（retry）**：提示中の質問の再提示は `RETRY_TEXT` ＋質問文であり、モードは `retry` になる。同じ質問 ID で再提示するのは1回だけである（`retried` に ID を記録する）。

**review への返答が読めない場合**：
| 回 | 処理 |
|---|---|
| 同じ巡回で1回目 | 数えない。`RETRY_TEXT` の後にカードと `CLOSING_JA` をそのまま再提示する（LLM を呼ばない。ログ `review_reply_unparsed`、`retry=true`） |
| 同じ巡回で2回目 | 数える。巡回数を +1 し、**何も確定（昇格）させない**。2巡目の要否は通常どおり判定する（ログ `review_reply_unparsed`、`retry=false`） |

ボタン「これでOK」（`REVIEW_OK`）は抽出を通さないので、読めない返答にはならない。

### 5-6 脱線（aside）と危険話題（danger）（05-B7・M5）
- 脱線は、提示中の質問と関係のない発言（雑談・質問返し）である。抽出段が意図 `aside` を返したときに判定する。
- 危険話題は、前処理の `DANGER_RE`、または抽出段の意図 `danger` で判定する。前処理で当たった発言からは、回答の部分も含めて何も抽出しない（ログ `danger_skip`）。
- 両者は回数枠（`MAX_ASIDES`=3、`aside_count`）を共有する（危険話題も枠を消費する。05-M5）。
- 脱線・危険話題では**状態を一切変えない**（スロット・NG・セグメント・質問の記録 `asked`・`attempts`・`topic_count`・`clarified`・`retried` を更新しない。M-repeat の対象にも数えない）。変わるのは `aside_count`、`flags`（危険話題のとき `danger_warned`）、ログ、および4回目以降のターン数だけである。
- 危険話題の返事は、`NOTICE.danger` の後に現在の提示をそのまま続けたものである（LLM を呼ばない）。**危険話題の注意文は、危険話題を検知するたびに毎回表示する**（「1回だけ」ではない。05-N9）。
- 脱線の返事は Stage3 で生成する。`view` のモードを `aside`、元のモードを `base_mode` として渡し、プロンプトは「脱線に一言だけ答えてから、`text` を原文のまま示す」（§9-2）。`review` 中の脱線では、モードは `review` のままとし、カードと締めの一文を再提示する。
- 4回目以降で `turns` が 15 に達した場合は、再提示せず result に進む。

### 5-7 結果を出した後の発言（05-B9）
結果を出した後（`phase=result` かつ結果が保存済み）の発言は、ターンに数えず、面談を再開しない。
| 発言 | 返事 |
|---|---|
| 空 | `POST_RESULT_TEXT` ＋改行＋保存済みの結果文 |
| `DANGER_RE` に当たる、または抽出の意図が `danger` | `NOTICE.danger` のみ（`danger_warned` を立てる） |
| 抽出の意図が `restart` | やり直し（状態を初期化し、最初の質問を出す） |
| `blocked`（中学生以下）で、抽出結果に `age_band` が `15_17`・`under_18`・`18_plus` のいずれかとしてある | **ブロック解除**：ターンを +1（計上先 result）し、`age_band` をその値・確信度 `high`・`from="free:post_result"` で上書きし、保存済みの結果と未解決を消し、`phase` を hard_filter に戻して面談を続ける（ログ `unblocked`） |
| それ以外 | `POST_RESULT_TEXT` ＋改行＋保存済みの結果文（ログ `post_result_message`） |

結果フェーズの抽出ペイロードは、`question` を「（おすすめを表示したあと）」、`choices` を空、`targets` を `blocked` なら `["age_band"]`、そうでなければ空とする。

### 5-8 規範の正規表現
以下は規範である。各行は `名前 = パターン`（Python `re` の構文。照合前に入力を NFKC 正規化し、前後の空白を除く）。`search`（部分一致）で使う。

<!-- REGEX:BEGIN -->
```text
# 名前 = パターン（Python re の構文。照合の前に発言を NFKC 正規化する）
DANGER_RE = 闇バイト|ホワイト案件|受け子|出し子|運び屋|叩き|荷物を?受け取るだけ|受け取るだけ|口座.*(売|貸|渡)|名義.*(貸|売)|即日.*高(収入|額)|高(収入|額).*即日|1日で?\s*\d+\s*万|日給\s*\d+\s*万|SNS.*(高額|高収入)|簡単.*高収入
MONEY_RE = \d+(\.\d+)?\s*万|１０３万|103万|106万|130万|150万|160万
JHS_RE = 中学|中[1-3１-３](?!\d)
NOPREF_RE = こだわりなし|どっちでもいい|どちらでもいい|なんでもいい|何でもいい|気にしない
DECLINE_RE = ひみつ|秘密|答えたくない|言いたくない|内緒|スキップ
PII_RE = [\w.+-]+@[\w-]+\.[\w.]+|0\d{1,4}-?\d{1,4}-?\d{3,4}|\d{3}-?\d{4}|〒
MULTI_SEP_RE = [、,，/／]\s*
```
<!-- REGEX:END -->


| 名前 | 用途 |
|---|---|
| `DANGER_RE` | 前処理の危険語判定（§5-4 の3）。結果後の発言にも使う（§5-7） |
| `MONEY_RE` | 生成文・結果の推薦理由に金額（「〇万」と扶養・社会保険の金額ライン）が入っていないかの検査（§8-4・§8-7）。時給の表示名（「1,200円以上」など）には当たらない |
| `JHS_RE` | 年齢の正規化（§6-3）と、年齢を聞いている質問での中学生以下の補完（§10-1） |
| `NOPREF_RE` | 「こだわりなし」の判定（§6-3・§7-5） |
| `DECLINE_RE` | 年齢・fact スロットの「答えない」の判定（§6-3） |
| `PII_RE` | `notes` に残す原文から、メールアドレス・電話番号・郵便番号を「＊」に置き換える（§10-6） |
| `MULTI_SEP_RE` | 複数選択の質問で、複数ボタンの区切りを判定する（§5-4 の4） |

- ハーネスが所有する固定文（§10-5 の TEXT ブロック、`SLOTS` の表示名、質問バンクの文言・選択肢）は、`DANGER_RE`・`MONEY_RE` に当たってはならない（`unit_checks_v4.py` 節19 で検査する）。
- `DANGER_RE` の誤検知（例：「口座に振り込んで渡す」）は許容する。誤検知しても注意文を出して同じ質問に戻るだけで、状態は変わらない（§5-6）。

---

## §6 条件表（スロット）

### 6-1 スロット一覧と正規値
「複数」は値がリストになるスロットである。値は「正規値：表示名」で示す。正規値以外は保存しない。表示名は §6-1 の表が正であり、カード・結果文の表示に使う。

| スロット | 表示名 | 種別 | 複数 | 正規値：表示名 |
|---|---|---|---|---|
| `age_band` | 年齢 | fact | | `under_15`：中学生以下／`15_17`：15〜17歳／`under_18`：18歳未満／`18_plus`：18歳以上／`declined`：ひみつ（未回答） |
| `days_per_week` | 週の日数 | pref | | `1-2`：週1〜2日／`3-4`：週3〜4日／`5+`：週5日以上 |
| `time_slots` | 時間帯 | pref | ○ | `morning`：朝／`daytime`：昼／`evening`：夕方〜夜／`late_night`：深夜 |
| `commute` | 通勤 | pref | | `walk_bike`：徒歩・自転車／`train_30`：電車30分まで／`train_60`：1時間くらいまで |
| `hourly_wage_min` | 時給の下限 | pref | | `1000`：1,000円台でOK／`1200`：1,200円以上／`1500`：1,500円以上（整数） |
| `shift_length` | 1回の勤務時間 | pref | | `short`：4時間未満／`mid`：4〜6時間／`long`：6時間以上 |
| `customer_facing` | 接客 | pref | | `like`：好き／`ok`：ふつう（できる）／`avoid`：できれば避けたい |
| `physical` | 体力面 | pref | | `heavy_ok`：体力仕事もOK／`standing_ok`：立ち仕事OK／`light`：軽めがいい／`seated`：座り仕事がいい |
| `purpose` | 目的 | pref | ○ | 18値。§6-6 の表 |
| `pace` | 忙しさ | pref | | `busy_ok`：忙しくてもOK／`calm`：落ち着いた職場 |
| `teaching_style` | 教わり方 | pref | | `structured`：マニュアルできっちり／`autonomous`：自分で考えたい |
| `social` | 職場の付き合い | pref | | `friendly`：仲良くしたい／`dry`：あっさりがいい |
| `environment_noise` | にぎやかさ | pref | | `lively`：にぎやか／`quiet`：静か |
| `wage_priority` | 時給と働きやすさ | pref | | `near_first`：近さ優先／`wage_first`：時給優先／`comfort_first`：ラクさ優先 |
| `recognition` | うれしい評価 | pref | | `customers`：お客さんから／`manager`：店長から |
| `coworker_age` | 一緒に働く人 | pref | | `same_age`：同年代／`mixed_age`：大人も／`similar_life`：同じような立場／`any`：気にしない |
| `school_permission` | 学校の許可 | fact | | `ok`：OK／`needs_notice`：届け出すればOK／`banned`：禁止／`unknown`：わからない |
| `exam_flex` | テスト期間 | pref | | `full_off`：休みたい／`reduced`：減らせればOK／`no_change`：気にしない |
| `long_vacation` | 長期休み | pref | | `heavy`：がっつり／`normal`：いつも通り |
| `start_time_pref` | 学校のあと | pref | | `right_after_school`：放課後すぐ／`after_club`：部活のあと |
| `available_hours` | 働ける時間 | fact | | `9-14`：9〜14時くらい／`10-16`：10〜16時くらい／`evening`：夕方以降／`varies`：日によってバラバラ |
| `sudden_leave` | 急なお休み | fact | | `frequent`：けっこうある／`sometimes`：たまにある／`rare`：ほぼない |
| `fuyou` | 扶養 | fact | | `within`：扶養内で／`beyond_ok`：気にしない／`unknown_needs_help`：わからない |
| `remote` | 在宅 | pref | | `yes`：在宅がいい／`no`：外がいい／`either`：どちらでも |
| `shift_flex` | シフトの決め方 | pref | | `fixed`：固定がいい／`flexible`：自分で選びたい |
| `leave_support` | 休むときの安心 | pref | | `instant`：すぐ休める／`cover`：代わりがいる |
| `side_job_allowed` | 副業の可否 | fact | | `ok`：OK／`needs_notice`：届け出が必要／`banned`：禁止／`unknown`：わからない・グレー／`civil_servant`：公務員 |
| `weekday_or_weekend` | 平日・週末 | pref | | `weekday_evening`：平日の夜／`weekend`：土日／`either`：どちらでも |
| `energy_left` | 本業後の体力 | pref | | `high`：けっこう残ってる／`some`：少しだけ／`none`：ほぼゼロ |
| `avoid_coworkers` | 会社の人に会う | pref | | `no`：気にしない／`yes`：絶対イヤ |
| `work_style` | 働き方 | pref | | `steady`：安定（毎週）／`spot`：スポット |
| `weekend_length` | 週末の長さ | pref | | `full_day`：1日／`half_day`：半日 |

- 学校の許可と副業の可否の「届け出が必要」の正規値は、どちらも `needs_notice` である（v4 初版にあった `notify` という表記は誤り。05-N1）。
- `こだわりなし` は pref スロットで有効な値として保存できる特別値であり、表示名も「こだわりなし」である。
- v3 の `atmosphere`（雰囲気）は、教わり方（`teaching_style`）・付き合い（`social`）・一緒に働く人（`coworker_age`）に分割した。v3 の時給系は、下限（`hourly_wage_min`、整数）と優先度（`wage_priority`）に分割した。v3 の `age_under_18`（真偽値）は廃止した（旧名で来た値は §8-4 で `age_band` に読み替える）。

### 6-2 重要度（セグメント別）
共通スロット（全セグメント）：

| 重要度 | スロット |
|---|---|
| HARD | `age_band`・`days_per_week`・`time_slots`・`commute` |
| MID | `hourly_wage_min`・`shift_length`・`customer_facing`・`physical`・`purpose` |
| LOW | `pace`・`teaching_style`・`social`・`environment_noise`・`wage_priority`・`recognition` |

セグメント固有スロット：

| セグメント | HARD | MID | LOW |
|---|---|---|---|
| student | `school_permission` | `exam_flex` | `long_vacation`・`coworker_age`・`start_time_pref` |
| homemaker | `available_hours`・`sudden_leave` | `fuyou`・`remote` | `shift_flex`・`leave_support`・`coworker_age` |
| side_job | `side_job_allowed`・`weekday_or_weekend` | `energy_left` | `avoid_coworkers`・`work_style`・`weekend_length` |
| other | — | — | `coworker_age` |
| unknown | — | — | — |

「有効なスロット」（`active_slots`）は、共通スロットと現在のセグメントの固有スロットの和である。

### 6-3 正規化（`normalize_value`。05-M9）
入力値は、正規値・表示名・自由な文字列・真偽値・リストのいずれでもよい。次の順に判定し、最初に決まった値を採用する。どれにも当てはまらなければ「正規化できない」（`None`）とする。

**複数スロット（`time_slots`・`purpose`）**：
1. 値がリストならその要素を、文字列なら NFKC 後に `[、,，/／・と&\s]+` で分割した各部分を、それぞれ単一値として下の手順で正規化する。
2. どれかの部分が `NOPREF_RE` に当たれば、pref スロットなら全体を `こだわりなし`、fact スロットなら `None` とする。
3. 正規化できた値を重複なく元の順に並べたリストを返す。1つも無ければ `None`。

**単一スロット**：値がリストなら先頭の要素だけを使う（空リストは `None`）。その上で次の表を上から適用する（「s」は NFKC 正規化して前後の空白を除いた文字列）。

| 順 | 対象 | 条件 | 結果 |
|---|---|---|---|
| 1 | すべて | 値が真偽値 | `age_band` なら 真→`under_18`、偽→`18_plus`。ほかは `None` |
| 2 | すべて | 値が正規値と一致（NFKC 前・後のどちらか） | その正規値 |
| 3 | すべて | s が表示名（NFKC 後）と一致 | その正規値 |
| 4 | `age_band` | `JHS_RE` に当たる | `under_15` |
| 5 | `age_band` | `DECLINE_RE` または `NOPREF_RE` に当たる | `declined` |
| 6 | `age_band` | `高[1-3]` または「高校」を含む | `15_17` |
| 7 | `age_band` | 「大学」「大1〜4」「専門」「短大」「社会人」「会社員」のいずれかを含む | `18_plus` |
| 8 | `age_band` | 1〜3桁の数字（後ろに「歳」「才」があってもよい）を含む | 最初の数が 15 未満→`under_15`、18 未満→`15_17`、それ以外→`18_plus` |
| 9 | `age_band` | 「未成年」「18歳未満」を含む | `under_18` |
| 10 | `age_band` | 「成人」「大人」「18歳以上」を含む | `18_plus`（ここまでで決まらなければ `None`） |
| 11 | pref | `NOPREF_RE` に当たる | `こだわりなし` |
| 12 | fact で正規値に `unknown` がある | `DECLINE_RE` に当たる | `unknown` |
| 13 | `shift_length` | 「〇時間」の数がある | 4 未満→`short`、6 未満→`mid`、それ以外→`long` |
| 14 | `shift_length` | 「短」「サクッと」を含む／「長」「ガッツリ」を含む | `short`／`long` |
| 15 | `hourly_wage_min` | 数字（桁区切りの「,」可）を含む | 最初の数を w とする。w < 20 なら w×100、20 ≤ w < 100 はそのまま。w < 1200→`1000`、w < 1500→`1200`、それ以外→`1500` |
| 16 | `days_per_week` | 数字を含む | 最初の1桁 d が 2 以下→`1-2`、4 以下→`3-4`、それ以外→`5+` |
| 17 | `time_slots` | 「深夜」／「朝」「午前」／「昼」「日中」／「夕」「夜」「放課後」を含む（この順に判定） | `late_night`／`morning`／`daytime`／`evening` |
| 18 | `commute` | 「徒歩」「自転車」「近所」を含む | `walk_bike` |
| 19 | `commute` | 「1時間」を含む | `train_60` |
| 20 | `commute` | 「〇分」の数 m がある | m ≤ 15 かつ「電車」を含まない→`walk_bike`、m ≤ 30→`train_30`、それ以外→`train_60` |
| 21 | `physical` | 「座」を含む／「軽」を含む | `seated`／`light` |
| 22 | `customer_facing` | 「苦手」「避け」「イヤ」「嫌」のいずれかを含む／「好き」を含む | `avoid`／`like` |

- fact スロットの `こだわりなし` は §7-5 の順3で扱う（年齢は `declined`、ほかは却下）。
- 正規化できない値は保存しない。原文は `PII_RE` で伏せて30文字に切り詰めて `notes[スロット]` に残し、カードで「未定（メモ：…）」と表示する（ログ `note_unnormalizable`）。

### 6-4 スロット間の整合チェック（カードの ⚠️ 行）
| 条件 | 表示文 |
|---|---|
| 本業後の体力が「ほぼゼロ」かつ週5日以上 | 本業後の体力は「ほぼゼロ」なのに週5日以上になってるよ。どっちを優先する？ |
| side_job で「平日の夜」なのに時間帯（リスト）に夕方〜夜が無い | 「平日の夜」希望だけど、時間帯に夜が入ってないよ。 |
| 働ける時間が日中（9〜14時・10〜16時）なのに時間帯に夕方〜夜がある | 働ける時間は日中なのに、時間帯に夕方〜夜が入ってるよ。 |
| NG に「立ち仕事」があるのに体力面が立ち仕事OK以上 | NGに「立ち仕事」があるけど、体力面は「立ち仕事OK」になってるよ。 |
| 未成年モードで週5日以上かつ6時間以上 | `NOTICE.minor_hours` の固定文（§10-5） |
| 扶養内希望かつ週5日以上かつ6時間以上 | 扶養内希望なら、週5日×長時間は多めかも（金額のラインは家族・勤務先で確認してね）。 |

- 主婦・主夫の `available_hours` が入り、`time_slots` が空なら、`time_slots` を導出して埋める（`from=derived`。確信度は `available_hours` と同じ。9〜14時→朝・昼、10〜16時→昼、夕方以降→夕方〜夜。日によってバラバラは導出しない）。

### 6-5 NG
- `user_ng`：ユーザーが言った NG（文字列）。ボタンの `__ng__` 効果、または抽出の `ng_updates` から追加する。抽出の文字列は NFKC 正規化済みであり、重複は NFKC 後の完全一致で判定して追加しない（05-N8）。抽出の `ng_removals` で削除できる。
- `derived_ng`：ルールが決める NG。未成年モードでは `DERIVED_NG_MINOR`（「深夜シフト」「重量物・危険な作業」）で固定であり、未成年モードでなければ空である。**新しい状態（`new_state`）の時点で、年齢未取得＝未成年モードとして `DERIVED_NG_MINOR` を入れておく**（05-N4）。年齢が変わるたびに再計算し（`recompute_derived`）、ユーザーは削除できない。
- 未成年モードで取り除いた時間帯の深夜・体力面の重労働は、後で18歳以上と確定しても自動では戻さない（05-N4。ユーザーが改めて答えれば入る）。
- 職種との照合（`ng_hits`）は付録B-3 で定める。

### 6-6 目的（`purpose`）の値（05-M10）
目的は推薦のスコアに使わない（カードの【好み】に表示するだけ）。そのため、意味が近い値（`money`・`earn`・`saving` など）を統合せず、質問ごとの選択肢をそのまま正規値にしている。

| 正規値 | 表示名 | 出どころの質問 |
|---|---|---|
| `hobby` | 推し活・趣味 | `q_money` |
| `saving` | 貯金 | `q_money` |
| `living_cost` | 生活費・家計の足し | `q_money`・`sc_hm_money` |
| `fun` | 旅行・遊び | `q_money` |
| `career` | 履歴書に書ける経験 | `sc_st_resume` |
| `easygoing` | ゆるく | `sc_st_resume` |
| `perk_meal` | まかない | `sc_st_perk` |
| `perk_discount` | 社割 | `sc_st_perk` |
| `pocket_money` | おこづかい | `sc_hm_money` |
| `use_experience` | 経験を活かす | `sc_hm_exp` |
| `try_new` | 違うことをしたい | `sc_hm_exp` |
| `no_brain` | 頭を使わない | `sc_sj_brain` |
| `skill_up` | 本業にプラス | `sc_sj_brain` |
| `earn` | 稼ぐ | `sc_sj_goal` |
| `refresh` | 気分転換 | `sc_sj_goal`・`fg_purpose` |
| `money` | お金 | `fg_purpose` |
| `skill` | 経験・スキル | `fg_purpose` |
| `people` | 人とのつながり | `fg_purpose` |

---

## §7 質問バンクと統合規則

### 7-1 質問バンク（付録A）
- 質問バンクは規範付録A（`/workspace/baito_harness/bank_v4.py` の `BANK`）である。49問の文言・選択肢・効果は付録A のファイルが正であり、本文には転記しない。質問の形式（スキーマ）と規則は付録A-2 で定める。
- `choice_effects` の値は必ず §6-1 の正規値である。ボタンが押されたときは、ハーネスがこの効果をそのまま適用する（LLM を通さない）。
- 複数のボタンが同時に押された場合（§5-4 の4）、複数スロットの値は全ボタン分の和集合にしてから1回で適用し、それ以外の効果はボタンごとに順に適用する。
- `__ng__` は `user_ng` に追加し、`__segment__` は §3-2 の規則でセグメントを設定する。

### 7-2 確信度（ハーネスが決める。`confidence_for`）
| 出どころ（`src_kind`） | 条件 | 確信度 |
|---|---|---|
| `choice_direct` | DIRECT フェーズ（hard_filter・fill_gaps）の質問、または確認質問のボタン | high |
| `choice` | icebreak・scenario_game・reverse_pick の質問のボタン | low |
| `free_current` | 自由入力で、提示中の質問の対象スロット、かつ DIRECT フェーズ | high |
| `free_current`・`free_other` | 自由入力で、fact スロット、かつ `evidence` の検査に合格 | high |
| `review` | review での訂正 | high |
| 上記以外 | — | low |
| 例外 | 1回の抽出で同じ単一スロットに異なる値が複数ある | 必ず low（ログ `ambiguous_extract`） |

- **`evidence` の検査（05-M12・N2）**：抽出結果の `evidence` が文字列で、NFKC 後に空でなく、かつ NFKC 後の発言の部分文字列であるとき合格とする。年齢だけを特別扱いすることはしない。
- LLM が出力した `confidence` は読まない。プロンプトでも書かせない。
- 確認質問中の自由入力では、`segment` 以外のスロットは「対象外」（`free_other`）として扱う。

### 7-3 `from` の値一覧
| 値 | 意味 |
|---|---|
| `choice:<質問ID>` | ボタンの `choice_effects` による（確認質問のボタンは `choice:q_segment`） |
| `free:<質問ID>` | その質問の対象スロットへの自由入力 |
| `free_other:<質問ID>` | 別の質問の最中に出た自由入力（例：日数を聞いたら時間帯も言った）。確認質問中なら `free_other:confirm_segment` |
| `free:post_result` | 結果後の年齢訂正（§5-7） |
| `review` | review でのユーザーの訂正 |
| `review_accepted` | review で異議が無く `low` から `high` に上がった値（§8-6） |
| `derived` | 他のスロットからの導出（§6-4） |

`pending`・`archived` に置かれた値は元の `from` と確信度をそのまま保持し、適用時もそれを使う。

### 7-4 年齢とブロック（`update_block`。05-B10・B11）
- `blocked="under_15"` になるのは、`age_band` が `under_15` かつ確信度が `high` のときだけである。確信度 `low` の `under_15`（推測）ではブロックせず、未成年モードのまま `hf_age` を聞く。
- 結果を作る直前（`finalize`）に、`age_band` が `under_15` であれば確信度にかかわらずブロックする（安全側。§16-2）。
- `age_band` が更新されるたびにブロックを再判定する。訂正で `under_15` 以外になればブロックを解除する（review での訂正、結果後の訂正（§5-7）を含む）。
- 再判定は、統合規則の後、review の昇格の後、`finalize` で行う。

### 7-5 統合規則（`apply_update`）
上から順に適用する。

| 順 | 条件 | 処理 | 戻り値 |
|---|---|---|---|
| 1 | 定義外のスロット | 捨ててログ（`discarded_unknown_slot`） | discarded |
| 2 | 現在のセグメントで無効なスロット | `pending` に置く（同じスロットは上書き） | pending |
| 3 | fact スロットに `こだわりなし` | 年齢なら `declined` に読み替えて続行。ほかは却下してログ（`rejected_by_rule`、`nopref_on_fact`） | rejected |
| 4 | 未成年モードで時間帯に深夜を含む | 深夜を除いてログと注意文 `late_night_removed`。残りが空なら却下 | rejected（空のとき） |
| 5 | 未成年モードで体力面が `heavy_ok` | `standing_ok` に置き換えてログ | — |
| 6 | 現在値が空 | 保存 | set |
| 7 | `purpose` | 既存値に追加する（`こだわりなし` は追加しない）。どちらかが high なら high。`from` は新しい値が high なら新しい値のもの | set |
| 8 | 現在値が high、新しい値が low | 値が違えば却下してログ（`low_over_high`）。同じなら何もしない | rejected／noop |
| 9 | 両方 low、値が違う、単一スロット | 矛盾。`conflicts[スロット]` に**出てきた値をすべて**（重複なし・出現順）記録し、新しい値を仮置きする（05-N7）。カードで「A / B / C（どっち？）」と表示する | conflict |
| 10 | 両方 low、複数スロット | 和集合（既存の順に新しい値を後ろへ追加） | set |
| 11 | 上記以外（high での上書き、low → high など） | 上書きする。新しい値が high なら矛盾の記録を消す | set |

保存後の副作用（戻り値が set・conflict のとき）：
- `age_band`：`derived_ng` を再計算し（§6-5）、未成年モードなら時間帯の深夜と体力面の重労働を取り除く。ブロックを再判定する（§7-4）。値が `declined` なら注意文 `age_declined`、未成年（`under_15`・`15_17`・`under_18`）なら `minor_rules`。
- `school_permission`：`banned`／`unknown` ならフラグと注意文（`school_banned`／`school_unknown`）。
- `side_job_allowed`：`banned`／`unknown`／`civil_servant` ならフラグと注意文（`side_banned`／`side_unknown`／`civil_servant`）。
- `fuyou`：値が入った時点でフラグと注意文 `fuyou`（金額は書かない）。
- `available_hours`：§6-4 の導出。
- 注意文は会話全体で各1回だけ表示する（`notices_sent`）。危険話題の注意文だけは例外で、毎回表示する（§5-6）。

### 7-6 HARD の保証（コードで強制する範囲）
v3 の「HARD は会話の終わりに必ず埋まる」は、ユーザーが答えない場合に守れない。v4 では保証を次のように定義し直し、すべてコードで強制する。
1. **聞く**：有効な HARD は hard_filter で1回ずつ直接聞く（年齢は必ず最初）。答えが無い・推測（low）のままなら、fill_gaps で言い換えて1回だけ聞き直す（直接質問は1スロット2回まで）。
2. **見せる**：review のカードには有効な HARD を全部出す。空は「未定（ここだけ教えて！）」、推測は「（推測）」と表示する。1巡目の後に空・推測の HARD が残っていれば、2巡目のカードで `focus` として再提示する。
3. **勝手に確定しない**：HARD の推測（low）が high になるのは、ボタン・直接質問への回答・evidence 付きの fact の発言・review での訂正・review の「全部OK」（`review_ack=all`）のときだけである。シナリオ・逆指名・他の質問中の自由入力（fact を除く）から HARD が high になることはない（M-spam）。
4. **黙って捨てない**：結果の時点で空の HARD は、結果文の「未定：…」に必ず並べる（M-silent）。**結果の時点で推測（low）のままの HARD は、結果文の「推測：…」に必ず並べる**（M-silent-low。05-B11）。推薦の絞り込みでは、空の HARD は「その条件では除外しない」、推測の HARD は「その値で絞り込む」として扱う。ただし年齢が空・未回答（`declined`）のときは未成年として扱う（§10-1）。

---

## §8 ハーネス仕様

### 8-1 定数（規範）
以下は規範である（Python のリテラルで書く）。

<!-- CONST:BEGIN -->
```text
# 名前 = Python のリテラル
VERSION = 'v4'
MAX_TURNS = 15
REVIEW_RESERVE = 2
MAX_REVIEW_ROUNDS = 2
HF_MAX = 6
SC_MAX = 6
SC_MIN = 3
PHASE_CAP = {'icebreak': 3, 'hard_filter': 6, 'scenario_game': 6, 'reverse_pick': 1}
MAX_ATTEMPTS = 2
TOPIC_CAP = 2
MAX_CLARIFY = 2
MAX_ASIDES = 3
LLM_RETRIES = 1
SEGMENT_SCORE = 1.5
HARD = 3
MID = 2
LOW = 1
DERIVED_NG_MINOR = ['深夜シフト', '重量物・危険な作業']
ORDER = ['icebreak', 'hard_filter', 'scenario_game', 'reverse_pick', 'fill_gaps', 'review', 'result']
```
<!-- CONST:END -->

| 定数 | 意味 |
|---|---|
| `MAX_TURNS` | ターン上限（§5-3） |
| `REVIEW_RESERVE` | `turns` ≥ 15−2=13 で review に移る |
| `MAX_REVIEW_ROUNDS` | review の最大巡回数 |
| `HF_MAX`・`SC_MAX`・`SC_MIN` | hard_filter の最大、scenario_game の最大・最小ターン数 |
| `PHASE_CAP` | フェーズごとの `phase_turns` の上限（§5-3） |
| `MAX_ATTEMPTS` | 1スロットあたりの直接質問回数（初回＋言い換え1回） |
| `TOPIC_CAP` | 同じスロットを狙う質問（全フェーズ）の回数。達したスロットはシナリオの対象から外す |
| `MAX_CLARIFY` | 会話全体の聞き返し回数（1質問につき1回） |
| `MAX_ASIDES` | 数えない脱線・危険話題の回数（合算） |
| `LLM_RETRIES` | 抽出・生成・結果それぞれの再試行回数 |
| `SEGMENT_SCORE` | `q_segment` のスコア（セグメント未判明のとき） |
| `HARD`・`MID`・`LOW` | 重要度の数値 |
| `DERIVED_NG_MINOR` | 未成年モードのルール NG（§6-5） |

### 8-2 公開 API
移植版が備えるべき関数・定数は付録C で定める（`unit_checks_v4.py` 節17 が存在を検査する）。主なものは次のとおりである。
| 関数 | 説明 |
|---|---|
| `new_state()` | 空の状態を作る（`derived_ng` は未成年モードの値で始まる） |
| `start(state, bank)` | 最初の質問の `view` を返す。ターンに数えない。冪等 |
| `resume(state, bank)` | 保存状態からの再開。`start` と同じで、`view["resumed"]=True` を付ける |
| `turn(state, user_text, bank, llm, catalog=None)` | 1通を処理して `{reply, view, done, counted}` を返す。`catalog` 省略時は付録B を使う |
| `State.to_dict()`／`State.from_dict(d)` | JSON で保存・復元する（§8-8） |
| `render_prompt(stage, variables)` | プロンプトファイルを結合して `{{変数}}` を埋める。埋まらない変数があれば `KeyError` |
| `llm.extract(payload)`／`llm.generate(view)` | aica 側が実装する LLM 呼び出し。文字列または dict を返す。例外を投げてもよい（失敗1回として扱う） |

### 8-3 質問の選び方（`pick_question`）
1. hard_filter で、`hf_age` が未提示かつ年齢が不足（空または low）なら、必ず `hf_age` を出す。
2. 候補は、判定したフェーズの質問で、セグメントが合うもの（`segments` に `all` または現在のセグメントを含む）である。fill_gaps では hard_filter の質問も候補にする。
3. 提示済みの質問は再び出さない。例外として fill_gaps では、DIRECT フェーズの提示済みの質問を、対象スロット（有効なもの）の直接質問回数がすべて `MAX_ATTEMPTS` 未満なら言い換え（`reask`、`alt_text` を使う）として候補にする。
4. 不足度 `gap_weight` は、空=1.0、low=0.5、high=0.0 とする。
5. 対象スロットは、質問の `targets` のうち有効なものである。DIRECT フェーズでは直接質問回数が `MAX_ATTEMPTS` に達したスロットを、scenario_game では `topic_count` が `TOPIC_CAP` に達したスロットを除く。
6. スコア＝Σ（対象スロットの重要度 × 不足度）。スコアが正で、`segments` に `all` を含まない質問なら +0.5 する。reverse_pick の質問はスコア 1.0 とする。
7. `q_segment` は、セグメントが `unknown` かつ未提示のときだけ候補にし、スコアは `SEGMENT_SCORE` とする。
8. 表示できる選択肢（`visible_choices`）が2つ未満の質問は出さない。
9. スコアが最大のものを選ぶ。**同点なら付録A の並び順で先のものを選ぶ**（比較は「より大きい」のみ）。スコア 0 の質問は出さない。
- 提示したときに、質問 ID を `asked` に加え、有効な対象スロットの `topic_count` を +1 し、質問のフェーズが DIRECT（icebreak・hard_filter）なら不足の対象スロットの `attempts` を +1 する。提示とは `ask`・`reask` で出すことであり、聞き返し・再提示（retry）・脱線後の再提示・確認質問は提示に数えない（05-M4）。
- `remaining_gaps`（fill_gaps を続ける条件）＝有効なスロットのうち、「HARD で不足度＞0」または「MID で空」であり、直接質問回数が `MAX_ATTEMPTS` 未満のもの。

### 8-4 LLM 出力の検証・再試行・代替
LLM 呼び出しが例外を投げた場合も、使えない出力として扱う。各段とも「1回目→検証→失敗なら再試行1回→検証→失敗なら代替」の順である。ログは、修復があれば `<段>_repaired`、再試行で成功すれば `<段>_retry_ok`、代替になれば `<段>_fallback` である（段は `extract`・`generate`・`result`）。

**抽出（`validate_extract`）**：使えない（`None`）のは、JSON として読めない（前後のコードフェンスは除去する。文字列でも dict でもない）、または JSON が dict でない場合だけである（`not_json`）。それ以外は次のように修復して使う。
| 入力 | 処理 |
|---|---|
| `slot_updates` が null・欠落・型違い | 空リスト |
| `slot_updates` が `{スロット: 値}` の dict | `[{"slot","value"}]` のリストに直す |
| `slot_updates` が11件以上 | 先頭10件だけ使う（`slot_updates_truncated` を記録） |
| 要素が dict でない、`slot` が文字列でない | その要素を捨てる |
| `slot` が `age_under_18`（v3 の旧名） | `age_band` として扱う（値は §6-3 の真偽値の規則） |
| `slot` が `segment` | トップレベルの `segment` が無ければその値として扱う |
| 定義外のスロット | 捨てる |
| 値が §6-3 で正規化できない | `value=null`・`raw=元の値` として残す（§6-3 の `notes`） |
| `ng_updates`・`ng_removals` | 文字列なら1件のリストにし、NFKC 後に空でない文字列だけを残す。6件以上なら先頭5件（`*_truncated` を記録） |
| `segment` | 正規値か、次の同義語なら正規値にする：学生・大学生・高校生・専門学生→`student`、主婦・主夫→`homemaker`、会社員・社会人・副業→`side_job`、フリーター・求職中・その他→`other`。それ以外（`unknown` を含む）は null |
| `intent` | `INTENTS`（`answer`・`clarify`・`show_result`・`restart`・`aside`・`danger`）以外は `answer`。`"clarify": true` があれば `clarify` |
| `review_ack` | `all`・`partial`・`none` 以外は null |
| `confidence` | 読まない |

**生成（`validate_reply`）**：出力が dict なら `reply`、JSON でない文字列（`{` で始まらない）ならその文字列を返事とする。次のいずれかに当たれば失敗である。
| 条件 | 失敗名 |
|---|---|
| 返事が無い・空 | `no_reply` |
| 600字を超える | `too_long` |
| `MONEY_RE` に当たる | `money_amount` |
| モード `review`：`card_text` をそのまま含まない、または `CLOSING_JA` の出現がちょうど1回でない | `card_altered` |
| モード `ask`・`reask`・`clarify`・`retry`・`confirm`・`aside`：NFKC 後の `view.text` を NFKC 後の返事が含まない | `question_missing` |
| 同上：返事の文字数 − `view.text` の文字数 > 120 | `too_much_extra` |

2回失敗したときの代替は、`review` なら `card_text`＋空行＋`CLOSING_JA`、それ以外は `view.text` である。注意文（`notices`）は代替の場合も前に付ける。

**結果（`validate_result`）**：§8-7。

- API のタイムアウトや指数バックオフは aica の LLM クライアント層の責務とする。ハーネスは例外を「失敗1回」として数えるだけである。

### 8-5 `view` と抽出ペイロード（プロンプト変数）
`view(state, bank)` は生成段の入力であり、プロンプトの変数をすべて含む。`render_prompt()` はファイル中の `{{変数}}` をここから埋め、足りない変数があれば `KeyError` で止める（`unit_checks_v4.py` の `prompt_*_vars_complete` で検査する）。

| キー | 型 | 内容 | 使うプロンプト |
|---|---|---|---|
| `phase` | 文字列 | 現在のフェーズ | main・extract |
| `segment` | 文字列 | `<正規値>（<SEGMENT_JA>）` の形式。例：`student（学生）`、`unknown（未判明）`（05-M2） | main |
| `mode` | 文字列 | `ask`／`reask`／`clarify`／`retry`／`confirm`／`aside`／`review`／`result` | （ハーネス内部） |
| `base_mode` | 文字列 | `aside` のときだけ。元のモード | （ハーネス内部） |
| `qid` | 文字列 | 質問 ID（`confirm_segment`・`review`・`result` を含む） | （ハーネス内部） |
| `choices` | 配列 | 表示するボタン（未成年モードで隠すものは除外済み） | （UI がボタンとして出す） |
| `notices` | 配列 | このターンで新しく出す注意文（文面） | （ハーネスが返事の前に付ける） |
| `text` | 文字列 | 提示する文。`ask`／`aside`：質問文、`reask`：`alt_text`、`clarify`：`CLARIFY_TEXT`＋「（」＋質問文＋「）」、`retry`：`RETRY_TEXT`＋質問文、`confirm`：`CONFIRM_TEXT`、`review`：カード本文 | （検証で使う） |
| `next_question` | JSON 文字列 | `{"id","mode","text","choices"}` | main |
| `slots_json` | JSON 文字列 | 下記 | main |
| `card_json` | dict | カード（§8-6）。review のときだけ | review |
| `card_text` | 文字列 | カード本文。review のときだけ | review |
| `closing` | 文字列 | 締めの一文 `CLOSING_JA`。review のときだけ | review |
| `result_json` | JSON 文字列 | 結果材料（§8-7）。result のときだけ | result |
| `aside` | 文字列 | 脱線・危険話題の再提示のときだけ `aside`／`danger` | （ハーネス内部） |

- 抽出の入力ペイロードでの `segment` は、正規値だけ（例：`student`）である。`view` の `segment` とは形式が異なる（05-M2）。
- 脱線（`aside=True`）の `text` は元の質問文（`clarify`・`retry` の前置きを付けない）である。

`slots_json` の形式（`tier` は `hard`／`mid`／`low`）：
```json
{"slots":[{"slot":"days_per_week","label_ja":"週の日数","tier":"hard","value_ja":"週3〜4日","confidence":"high","from":"choice:hf_days"}],
 "missing_hard":["通勤","学校の許可"],"ng":["深夜シフト","重量物・危険な作業"],"minor_mode":true,"turn":4,"max_turns":15}
```

抽出ペイロード（`extract_payload`。05-B8）：
| キー | 通常の質問 | review | 確認質問 | 結果後 |
|---|---|---|---|---|
| `question` | 質問文（`reask` は `alt_text`） | カード本文 | `CONFIRM_TEXT` | 「（おすすめを表示したあと）」 |
| `choices` | 表示中の選択肢 | `["これでOK"]` | `q_segment` の4択 | `[]` |
| `targets` | 質問の `targets` | 2巡目は `focus`、1巡目はカードの全項目のスロット | `["segment"]` | `blocked` なら `["age_band"]`、そうでなければ `[]` |

共通キーは `phase`・`segment`（正規値）・`user_text`・`allowed_slots`（有効なスロット → 正規値の文字列のリスト）である。
```json
{"phase":"hard_filter","segment":"student","user_text":"週3くらい","question":"週に何日くらい入れそう？",
 "choices":["週1〜2日","週3〜4日","週5日以上"],"targets":["days_per_week"],
 "allowed_slots":{"days_per_week":["1-2","3-4","5+"],"…":["…"]}}
```
抽出の出力形式（LLM → ハーネス）：
```json
{"slot_updates":[{"slot":"days_per_week","value":"週3〜4日","evidence":"週3くらい"}],
 "ng_updates":["夜勤"],"ng_removals":[],"segment":null,"intent":"answer","review_ack":null}
```
生成の出力形式：`{"reply": "..."}`。結果の出力形式は §8-7。

### 8-6 review（カード）
**カード JSON（`build_card`）**
```json
{"round":1,"segment":"student","segment_ja":"学生",
 "hard":[{"slot":"time_slots","label_ja":"時間帯","tier":"hard","priority":3,"value":["evening"],"value_ja":"夕方〜夜",
          "inferred":true,"missing":false,"conflict_values":null,"conflict":null,"note":null}],
 "prefs":[{"slot":"pace","label_ja":"忙しさ","tier":"low","priority":1,"value":"busy_ok","value_ja":"忙しくてもOK",
           "inferred":true,"missing":false,"conflict_values":["calm","busy_ok"],"conflict":["落ち着いた職場","忙しくてもOK"],"note":null}],
 "ng_user":[],"ng_rule":[],"warnings":[],"focus":[]}
```
- 項目の `tier` は `hard`／`mid`／`low` である（05-N3）。
- `hard` は有効な HARD 全部（`active_slots` の順）、`prefs` は有効な MID 全部の後に値のある LOW である。
- 年齢が空または `declined` の項目には `safety_note`（「安全のため18歳未満と同じ条件で探すよ」）を付ける。
- `focus` は2巡目以降（`review_rounds` ≥ 1 のとき作るカード）だけ入り、「矛盾がある項目」と「空または推測の HARD」のスロットの一覧である。

**カード本文（`render_card`）**
各項目の行は次の規則で作る。
| 状態 | 行 |
|---|---|
| 矛盾あり | `・<表示名>：<候補1> / <候補2> …（どっち？）` |
| 空 | `・<表示名>：未定` ＋（メモがあれば `（メモ：…）`）＋（HARD なら `（ここだけ教えて！）`、それ以外は `（よければ教えてね）`）＋（`safety_note` があれば `※<safety_note>`） |
| 値あり | `・<表示名>：<値の表示名>` ＋（推測なら `（推測）`）＋（`safety_note` があれば `※<safety_note>`） |

1巡目（`focus` が空）の並び：
```
【あなた】学生
【ゆずれない条件】
・年齢：18歳以上
・時間帯：夕方〜夜（推測）
・通勤：未定（ここだけ教えて！）
【好み】
・時給の下限：未定（よければ教えてね）
・忙しさ：落ち着いた職場 / 忙しくてもOK（どっち？）
【NG】深夜シフト（ルール）
⚠️ （§6-4 の整合チェックの文）
```
- 2巡目は先頭を「ここだけもう一回！」とし、`focus` の項目（`hard`→`prefs` の順）、【NG】行、⚠️ 行だけを出す。
- 【NG】行は `user_ng` の後に `derived_ng` を「（ルール）」付きで並べ、「、」で区切る。無ければ「なし」。複数値は「・」で区切る。

**締めの一文** は `CLOSING_JA`（§10-5）である。持ち主はハーネスであり、カード本文（`card_text`）には含めず `view["closing"]` として渡し、返事の中にちょうど1回だけ入れる。ボタンは `REVIEW_OK`（「これでOK」）1つである。

**`review_ack` の値（05-M20）**
| 値 | 意味 | 例 |
|---|---|---|
| `all` | カード全体を了承した | 「これでOK」「全部合ってる」 |
| `partial` | 一部を直した（残りは何も言っていない） | 「通勤だけ電車30分に直して」 |
| `none` | カードについて何も言っていない・否定した | 質問だけ、「全然ちがう」 |
| null | 判定できない | — |

**返答の反映（`apply_review`）**
1. `review_rounds` を +1 する。ボタン「これでOK」なら `ack=all`、そうでなければ抽出の `review_ack` を使う。
2. 自由入力の `slot_updates` は確信度 high・`from=review` で統合する（正規化できない値は `notes`）。`ng_updates` を追加し、`ng_removals` を `user_ng` から削除する。抽出の `segment` は `review_explicit` として §3-2 の規則で扱う（既知の別の値なら確認に回す）。
3. 昇格：カードに表示した項目のうち、訂正されず、値があり、確信度 `low` で、矛盾が無いものを high・`from=review_accepted` に上げる。**ただし HARD は `ack=all` のときだけ上げる**。返答が2回読めなかった場合（§5-5）は何も上げない。
4. ブロックを再判定する（§7-4）。
5. 2巡目が必要な条件：「元が high の HARD が訂正で別の値に変わった」「矛盾が残っている」「セグメントの確認が保留中」のいずれか、または1巡目の終了時に不足の HARD が残っている。
6. 意図が `show_result` なら2巡目を出さない。2巡目は最大1回であり、`turns` が15に達していれば出さない（§5-2）。

**結果前の確定（`finalize`）**
1. 矛盾が残ったスロットは値を消し、記録されていた候補（全部）を `unresolved` に移す（ログ `conflict_unresolved`。§12-2 の暫定方針）。
2. `derived_ng` を再計算する。
3. ブロックを最終判定する（`under_15` なら確信度にかかわらずブロック。§7-4）。

### 8-7 結果
**候補の絞り込みと点数（`eligible_jobs`。05-B3）**
- 職種カタログ（付録B、24件）が**許可リスト**である。ここに無い職種は推薦しない。
- カタログの並び順で各職種を調べ、次の除外条件のどれかに当たれば除く。
  1. 未成年モードで、`min_age` ≥ 18、`phys` が `heavy`、`night_only` が真のいずれか。
  2. `user_ng`＋`derived_ng` に当たる（`ng_hits`。付録B-3）。
  3. `time_slots` が空でないリストのとき、使える時間帯（未成年モードでは深夜を除く）が空でなく、職種の `times` と1つも重ならない。
  4. `customer_facing` が `avoid` で、`cf` が `high`。
  5. `physical` が `seated` で、`phys` が `standing`・`heavy`。
  6. `physical` が `light` で、`phys` が `heavy`。
- 残った職種に次の点数を足し、根拠のスロットを `why_slots` に順に記録する。

| 条件 | 加点 | `why_slots` |
|---|---|---|
| `customer_facing` が `like`・`ok`、かつ `cf` が `high`・`mid` | +1 | `customer_facing` |
| `customer_facing` が `avoid`、かつ `cf` が `none`・`low` | +1 | `customer_facing` |
| `physical` が `seated`、かつ `phys` が `seated` | +1 | `physical` |
| `environment_noise` に値があり、`noise` と一致 | +0.5 | `environment_noise` |
| `remote` が `yes`、かつ `remote` が真 | +1.5 | `remote` |
| `work_style` が `spot`、かつ `spot` が真 | +0.5 | `work_style` |
| `avoid_coworkers` が `yes`、かつ `remote` が真 | +0.5 | `avoid_coworkers` |
| `time_slots` が空でないリストで、`times` と重なる（深夜を含めて判定） | +0.5 | `time_slots` |

- 点数の降順に並べる。同点はカタログの並び順（安定ソート）とする。通勤・時給・勤務時間・日数・目的は点数にも除外にも使わない（カタログに該当データが無いため。§12-1）。
- 上位6件を候補（`candidates`）にし、選ぶ数 `need` は min(3, 候補数) とする。`blocked` なら候補は空、`need`=0 である。

**結果材料（`build_result_payload`）**
```json
{"card":{"…":"…"},"candidates":[{"job_id":"conv","name":"コンビニスタッフ","score":0.5,"why_slots":["time_slots"]}],
 "need":3,"unknown_hard":["通勤","学校の許可"],"unresolved":{"忙しさ":["落ち着いた職場","忙しくてもOK"]},
 "notices":["（注意文）"],"blocked":null,"conditional":false}
```
- `card` は `focus` を空にしたカードである。
- `unknown_hard`：結果時点で空の有効な HARD の表示名（`unknown_hard_slots` にスロット名）。
- `guessed_hard`：結果時点で確信度 `low` の有効な HARD を `<表示名>＝<値の表示名>` の形で並べたもの（`guessed_hard_slots` にスロット名）。
- `unresolved`：`finalize` で矛盾のまま残ったスロットの表示名 → 候補の表示名のリスト。
- `blocked`：null または `"under_15"`（05-N9）。
- `conditional`：`flags` に `school_banned`・`side_banned`・`civil_servant` のいずれかがある。
- `notices`（順番は固定。05-M11）：
  1. `blocked` なら `NOTICE.under_15`。
  2. `flags` にあるものを `school_banned`・`side_banned`・`civil_servant`・`school_unknown`・`side_unknown`・`fuyou` の順に。`flags` に `danger_warned` があれば `NOTICE.danger`。
  3. `blocked` でなく、セグメントが `student` なら `NOTICE.student_fuyou`。
  4. `blocked` でなく、セグメントが `side_job` なら `NOTICE.side_tax`。
  5. `blocked` でなく、未成年モードなら、年齢が `declined` なら `NOTICE.age_declined`、そうでなければ `NOTICE.minor_rules`。
  6. 1〜5で1件でも入っていれば、最後に `NOTICE.rules_only`。
  - `late_night_removed`・`minor_hours` は会話中に出しているので結果では繰り返さない。

`result_json`（生成段に渡す）は上記のうち `card`・`candidates`・`need`・`unknown_hard`・`unresolved`・`notices`・`blocked`・`conditional` である。

**LLM の出力と検証（`validate_result`）**
```json
{"reply":"条件に合いそうなのを選んだよ！","recommendations":[{"job_id":"conv","reason":"夕方からの時間帯に入りやすいから"}]}
```
- 失敗条件：JSON でない（`not_json`）、`recommendations` がリストでない・件数が `need` と違う（`wrong_count`）、要素が dict でない・`job_id` が候補に無い・重複（`not_in_allowlist`）、`reason` が文字列でない・空・`MONEY_RE` に当たる（`bad_reason`）、`reply` が文字列でない・`MONEY_RE` に当たる（`bad_reply`）。
- 2回失敗したときの代替（`fallback_result`）：`reply` は「条件から、合いそうなバイトを選んだよ！」、推薦は候補の上位 `need` 件で、理由は `fallback_reason`（`why_slots` の先頭2つの表示名を「・」でつないだもの X について「X の条件に合いそうだから」。`why_slots` が空なら「条件の条件に合いそうだから」）。
- `need`=0（`blocked`、または候補0件）のときは LLM を呼ばず、代替（推薦0件）を使う（05-M13）。

**事後チェックと補充（`post_check_result`。05-M13）**
1. 推薦を順に調べ、重複・カタログ外・`job_safe` でないもの（未成年モードで `min_age` ≥ 18・`heavy`・`night_only`、または NG に当たる）を除く（ログ `result_postcheck_drop`）。
2. 残りが `need` 件に満たなければ、候補を順に調べ、未使用かつ `job_safe` なものを `fallback_reason` の理由で補充する（ログ `result_refill`）。
3. 補充しても足りなければ、その件数のまま出す（結果文の5番の文が付く）。

**結果文（`compose_result_text`。順番は固定）**
1. `notices` を順に。`blocked` の場合はここで終える。
2. `reply`（ひとこと）。
3. `conditional` なら「（ルールの確認がとれたら、の参考だよ）」。
4. 推薦ごとに「`<番号>. <職種名>：<理由>`」。
5. 推薦が0件なら「今の条件だと合うものが見つからなかった…条件を少しゆるめてもう一回探してみよう。」、1〜2件なら「今の条件だと合うのはここまで。時間帯や通勤の条件を少し広げると、候補が増えるよ。」。
6. `guessed_hard` があれば「推測：」＋「、」区切りの一覧＋`GUESS_TAIL`（05-B11）。
7. `unknown_hard` があれば「未定：」＋「、」区切りの一覧＋「（決まったらもっと絞り込めるよ）」。
8. `unresolved` の項目ごとに「未定：<表示名>（<候補> / <候補> のどちらか。決まったら教えてね）」。

- このターンで新しく出た注意文のうち、`notices` に含まれないもの（`late_night_removed` など）は結果文の先頭に付ける。ただし `blocked` のときの `minor_rules` は付けない。
- 結果は1回だけ作り、`state.result`（`text`・`recommendations`・`payload`）に保存する。以後の発言は §5-7 で扱う。

### 8-8 状態の保存と再開
- `State.to_dict()` は JSON にできる dict を返す。先頭が `_` の項目（そのターン限りの一時値：新しい注意文・2巡目要否・計上先）は保存しない。項目の一覧は付録D-1。
- `State.from_dict()` は未知のキーを無視し、`hourly_wage_min` を整数に戻す。v4 の状態は `version="v4"` を持つ。**`version` キーが無い保存状態は v3 のものとみなす**（05-N5）。aica の振り分けは `d.get("version", "v3")` で行う。
- 保存は毎ターンの `turn()` の後に行う。途中離脱して再開した場合は `resume()` で同じ質問を出し直す（ターンは増えない）。
- 個人情報：`notes` には伏せ字処理（`scrub_note`）後の文字列だけを保存する。ログ（`state.log`）には発言の原文を入れない。ただし中学生以下の補完（§5-4 の5）の `evidence` には発言を使うが、これはスロットに保存せず確信度判定にだけ使う。

---

## §9 プロンプト仕様

### 9-1 ファイルと組み立て
| 段（`prompt_stage`） | 結合するファイル（この順） | 変数 | 出力 |
|---|---|---|---|
| `extract`（Stage 1） | `prompt_extract_v4_ja.txt` | `phase`・`segment`・`question`・`choices`・`targets`・`allowed_slots`・`user_text` | §8-5 の抽出形式 |
| `say`（Stage 3：ask・reask・clarify・retry・confirm・aside） | `prompt_main_v4_ja.txt` | `phase`・`segment`・`slots_json`・`next_question` | `{"reply"}` |
| `review` | `prompt_main_v4_ja.txt`＋`prompt_review_v4_ja.txt` | 上記＋`card_text`・`closing` | `{"reply"}` |
| `result` | `prompt_main_v4_ja.txt`＋`prompt_result_v4_ja.txt` | 上記＋`result_json` | §8-7 の出力形式 |

- 4ファイルは `/workspace/baito_harness/` にある。ファイルを結合するときは空行1つ（`\n\n`）で区切る。
- **常時オン**は `prompt_main_v4_ja.txt`（人格・口調・禁止事項）だけである。review・result は**フェーズ別**の追加ファイルを後ろに足す。
- **呼び出しの責任（05-M3）**：`llm.extract(payload)` の実装は `render_prompt("extract", payload)` を、`llm.generate(view)` の実装は `render_prompt(prompt_stage(view), view)` を呼ぶ（aica の LLM 層の責務）。`prompt_stage` は `view.mode` が `review` なら `review`、`result` なら `result`、それ以外は `say` を返す。
- result 段の変数は `phase`・`segment`・`slots_json`・`next_question`（`mode=result`、`text` は空）・`result_json` であり、`card_text`・`closing` は使わない。
- 組み立ては `render_prompt(stage, variables)` で行う。文字列でない値は JSON にしてから埋め、足りない変数があれば `KeyError` で止める。
- 生成段の呼び出しでは、組み立てたプロンプトを system に、ユーザーの直近の発言を user メッセージとして渡す（脱線への1文の返事に使う）。会話履歴を渡すかどうかは aica の実装に任せるが、質問・カード・結果の中身は必ず変数から取る。

### 9-2 口調と生成の規則
- タメ口寄りでフレンドリーにする（「〜だよ」「〜してね」）。敬語にしない。説教しない。絵文字は1つまで。
- 本書はである調、ユーザーに見せる文はタメ口寄り、という使い分けである。
- リアクションは1文・40字までとし、質問文は `next_question.text` をそのまま使う。
- 40字はプロンプトでの指示であり、検証（§8-4）の上限120字は生成の揺れを許す余裕である。差は意図したものである（05-N6）。
- 脱線（`mode=aside`）では、脱線に1文だけ答えてから `text` を原文のまま示す。

### 9-3 抽出プロンプトの規則（`prompt_extract_v4_ja.txt`。05-M16）
抽出プロンプトは次の10項目を含む（参照実装のファイルの番号と一致する）。
1. 発言に書いてあることだけを入れ、推測で埋めない。わからなければ `slot_updates` は空でよい。
2. `confidence` は書かない（書いても無視され、確信度はハーネスが決める。§7-2）。
3. **ユーザー本人の**年齢・学年が出たら `age_band` に入れ、`evidence` に「16歳」「高1」などを原文のまま入れる。家族・子ども・知人の年齢や学年（「子どもが中学生」「中学の先生」）は入れない（§10-1）。
4. intent の判定基準：「どっちでもない」「どういうこと？」→`clarify`、「早く結果見せて」→`show_result`、「最初から」→`restart`、質問と関係ない雑談・逆質問→`aside`、「SNSで高額」「受け取るだけ」など闇バイトが疑われる話→`danger`。
5. 「どっちでもいい」「こだわりなし」は、そのスロットの `value` に「こだわりなし」と入れる。年齢・学校の許可・副業の可否には入れない。
6. 学生／主婦・主夫／会社員などの身分がはっきり言われたときだけ `segment` を入れる。言われていなければ null。
7. review では、ユーザーが直した項目だけを `slot_updates` に入れる。`review_ack` は §8-6 の表に従う。
8. 金額・法律の判断はしない。
9. `evidence` は発言の一部を言い換えずに抜き出す。発言に無い文字列の更新は確信度の判定に使われない（§7-2）。
10. `value` は `allowed_slots` に書かれた値（キーか表示名）を逐語で使うのが望ましい。合うものが無いときはユーザーの言葉をそのまま入れる。

出力形式は §8-5 の抽出の出力形式であり、それ以外のキーは出さない。規範は上の意味内容であり、文言の調整は許す。

### 9-4 intent（抽出段が判定する）
| intent | 意味 | ハーネスの処理 |
|---|---|---|
| `answer` | 質問への回答（既定） | 統合して次へ |
| `clarify` | 「どれも違う」「うーん」など、選べない | 聞き返し（§5-5 の条件を満たすときだけ） |
| `show_result` | 「早く結果を見せて」 | 回答を反映したうえで review に直行。review 中・review 前を問わず、以後2巡目を出さない（§16-2） |
| `restart` | 「最初からやり直したい」 | 状態を初期化（`restarts` だけ残す）、ターンを0に戻す |
| `aside` | 質問と関係ない雑談・逆質問 | §5-6 |
| `danger` | 闇バイトが疑われる話 | §5-6（注意文は毎回） |

### 9-5 聞き返しと再提示の決まり文句（ハーネス所有）
- 聞き返し（clarify）：`CLARIFY_TEXT`＋「（」＋元の質問文＋「）」。
- 再提示（retry）：`RETRY_TEXT`＋元の質問文。review の再提示は `RETRY_TEXT`＋改行＋カード本文＋空行＋`CLOSING_JA`。
- 確認質問（confirm）：`CONFIRM_TEXT`。
- 言い換え（reask）：質問の `alt_text`。
- 結果後：`POST_RESULT_TEXT`。

---

## §10 安全・法令まわり

### 10-1 年齢・未成年モード・中学生以下（05-B10）
- 年齢は hard_filter の最初に必ず聞く（§8-3 の1）。選択肢は「中学生以下」「15〜17歳」「18歳以上」「ひみつ」である。
- **未成年モード**（`minor_mode`）は、年齢が空、`under_15`・`15_17`・`under_18`、または `declined` のときに有効である（18歳以上の値が入るまでは未成年として扱う。値の確信度は問わない）。
- 未成年モードでは次を行う。
  - 選択肢のうち `choice_flags` に `late_night`・`heavy` があるものを表示しない。抽出に渡す正規値（`allowed_slots`）はスロット定義のまま渡し、統合時に除く。
  - 統合時に深夜・重労働を除く（§7-5。ログ `rejected_by_rule`、注意文 `late_night_removed`）。年齢が未成年に訂正されたときも、既存の値から同様に除く（`recompute_derived`）。
  - ルール NG（`derived_ng`）を `DERIVED_NG_MINOR` にする。
  - 結果から「`min_age` ≥ 18」「重労働」「深夜専用」の職種を除く（事後チェックでも再確認する）。
  - 週5日以上・1回6時間以上の希望には、カードの ⚠️ 行で `NOTICE.minor_hours` を出す（§6-4）。シフト時間の自動補正はしない（見送り。§15）。
- 年齢の値が入ったとき：`declined` なら注意文 `age_declined`、未成年の値なら `minor_rules` を出す（各1回）。空のまま結果に至った場合は、結果の注意文に `minor_rules` を入れる（§8-7）。
- **中学生以下の判定の範囲**：
  1. 抽出の `age_band` 更新（ユーザー本人の年齢。§9-3 の3）で `under_15` になった場合は、どのフェーズでも有効である。確信度は §7-2 に従う。
  2. 発言そのものに `JHS_RE` が当たった場合の補完（§5-4 の5）は、**提示中の質問の `targets` に `age_band` があり、確認質問でないとき**だけ行う。年齢を聞いていない場面の「中学の先生」「子どもが中学生」は年齢として扱わない。
  3. 「大学2年」などは `JHS_RE` に当たらない（否定の先読み）。
- **ブロック**：`under_15` が確信度 high なら直ちに `blocked="under_15"` とし、結果に進む（§5-2）。確信度 low の `under_15` は未成年モードのまま会話を続け、`hf_age` で確かめる。結果前の確定（`finalize`）の時点で low の `under_15` が残っていれば、安全のためブロックする（§7-4）。
- ブロック中の発言は §5-7 で扱う。年齢を訂正すれば解除する（ログ `unblocked`）。

### 10-2 学校・会社のルール（禁止とわからないの区別）
| 値 | 扱い |
|---|---|
| 学校：`ok` | そのまま |
| 学校：`notify`（届け出・許可が必要） | 許可されうる扱いとし、条件付きにはしない。カードの値の表示名「届け出・許可が必要」で本人に示す（`needs_notice`。05-M9） |
| 学校：`banned` | 注意文 `school_banned`。結果は条件付き（`conditional`） |
| 学校：`unknown` | 注意文 `school_unknown`。結果は通常どおり |
| 副業：`ok` | そのまま。副業セグメントには結果で `side_tax` を出す |
| 副業：`notify` | 学校の `notify` と同じ扱い |
| 副業：`banned` | 注意文 `side_banned`。結果は条件付き |
| 副業：`unknown` | 注意文 `side_unknown` |
| 副業：`civil_servant` | 注意文 `civil_servant`。結果は条件付き |

注意文は値が入った時点で出し（各1回）、`flags` に記録して結果でも再掲する（§8-7）。

### 10-3 扶養
- 扶養は主婦・主夫セグメントで「扶養内で／気にしない／わからない」を聞くだけである。**金額（103万・130万など）は一切出さない**。生成・結果の出力に金額（`MONEY_RE`）があれば検証で落とす。ハーネス所有の文（`unit_checks_v4.py` 節19）は `MONEY_RE` に当たらない。
- 扶養の値が入れば注意文 `fuyou` を出す。
- 学生には扶養スロットを作らず、結果で注意文 `student_fuyou` を出すだけとする（見送り。§15）。

### 10-4 闇バイト
- 発言に `DANGER_RE` が当たるか、抽出が `danger` を返したら、§5-6 の危険話題として扱う。注意文 `danger`（警察相談専用電話 #9110 を含む）は**当たるたびに**返事の先頭に付ける（`notices_sent` の1回制限の対象外）。
- 誤検知（例：「日給1万円くらいの仕事」）は受け入れる。注意文は話題を広げずに同じ質問に戻るだけなので、実害は小さいと判断した（§16-2）。
- 推薦は付録B の**許可リスト**からしか出さない。LLM が作った職種や求人・会社名は検証・事後チェックで落とす。

### 10-5 決まり文句と注意文（規範・ハーネス所有）
ユーザーに見せる固定の文は次のとおりであり、規範である。値は `=` の後ろの1行全体（引用符なし）である。`Q_SEGMENT_CHOICES` は JSON の配列である。

<!-- TEXT:BEGIN -->
```text
# 名前 = 文（= の後ろの1行全体）
CLOSING_JA = 推測のところ、ちがってたらまとめて教えてね！「どっち？」はどっちか選んでね。「未定」はわかる範囲で教えてね。ぜんぶ合ってたら「これでOK」だけで大丈夫🙆
REVIEW_OK = これでOK
CLARIFY_TEXT = お、どのへんがちがう？ひとことで教えて！
RETRY_TEXT = ごめん、うまく受け取れなかった！もう一回だけ教えて。
CONFIRM_TEXT = 確認させてね！今のあなたに近いのはどれ？
POST_RESULT_TEXT = おすすめはここまでだよ。条件を変えてもう一回探すなら「最初から」と送ってね。
GUESS_TAIL = （確認できていない推測の条件だよ。ちがっていたら「最初から」でやり直せるよ）
NOTICE.under_15 = 中学生のうちは、法律で原則アルバイトができないんだ（子役など特別な許可がある場合をのぞく）。高校生になったらまた一緒に探そう！それまでは、おうちの人や学校の先生に相談してみてね。
NOTICE.minor_rules = 18歳未満は、22時〜翌5時の深夜と、重い物を運ぶような危険な作業はNGなんだ。1日8時間・週40時間までがルールだよ。その条件で探すね。
NOTICE.age_declined = 年齢を教えてもらえなかったから、安全のため18歳未満と同じ条件（深夜・重い作業なし）で探すね。あとで変えられるよ。
NOTICE.late_night_removed = 18歳未満（または年齢未回答）だと深夜（22時〜翌5時）は働けないから、深夜はのぞいておくね。
NOTICE.school_banned = 学校のルールで禁止なら、まずは先生やおうちの人に相談してみてね。ここでは「もしOKになったら」の参考として続けるね。
NOTICE.school_unknown = 学校のバイトのルールは、生徒手帳や先生に一度確認しておくと安心だよ。
NOTICE.side_banned = 会社で副業が禁止なら、無理に始めずに就業規則を確認してね。ここでは「もしOKになったら」の参考として続けるね。
NOTICE.side_unknown = 副業OKかわからないときは、就業規則や人事に一度確認してね。
NOTICE.civil_servant = 公務員は副業に法律上の制限があるよ。始める前に職場のルールを必ず確認してね。ここでは参考として続けるね。
NOTICE.fuyou = 扶養のラインは家族の勤め先や制度で変わるから、具体的な金額は家族・勤務先・税務署などで確認してね。
NOTICE.danger = ⚠️「SNSで高額」「受け取るだけ」「すぐ稼げる」みたいな話は、闇バイト（犯罪）の可能性があるよ。絶対に応募しないでね。もう連絡しちゃった・脅されてるときは、警察相談専用電話 #9110 に相談してね。
NOTICE.minor_hours = 18歳未満は1日8時間・週40時間までだから、日数か時間を少し調整するかも。
NOTICE.student_fuyou = 親の扶養に入っている学生は、稼ぎすぎると親の税金や保険に影響することがあるよ。くわしくはおうちの人に確認してね。
NOTICE.side_tax = 副業の収入は、確定申告が必要になることや、本業と労働時間が合算されることがあるよ。くわしくは勤務先や税務署で確認してね。
NOTICE.rules_only = ※ここで伝えているのは一般的なルールの目安で、法律相談ではないよ。
Q_SEGMENT_CHOICES = ["学生", "家のこと中心（主婦・主夫など）", "本業あり副業探し", "フリーター・求職中・その他"]
```
<!-- TEXT:END -->

| キー | 出す条件 |
|---|---|
| `under_15` | 中学生以下でブロック（結果） |
| `minor_rules` | 年齢が未成年の値になった／年齢が空のまま結果 |
| `age_declined` | 年齢「ひみつ」 |
| `late_night_removed` | 未成年モードで深夜を除いた |
| `school_banned`・`school_unknown` | 学校のルールが禁止・不明 |
| `side_banned`・`side_unknown`・`civil_servant` | 副業が禁止・不明・公務員 |
| `fuyou` | 扶養の値が入った |
| `danger` | 危険話題（毎回） |
| `minor_hours` | 未成年で日数・時間が多い（カードの ⚠️ 行） |
| `student_fuyou`・`side_tax` | 学生・副業の結果 |
| `rules_only` | 結果に上記の注意文が1つでもある |

- 会話中の注意文は1種類につき1回だけ出す（`notices_sent`。`danger` を除く）。結果では関係する注意文を §8-7 の順でまとめて出す。
- 注意文は LLM に書かせず、ハーネスが返事の前に付ける。注意文どうし、注意文と返事は改行1つで区切る（空行は入れない）。法律の断定や個別の法律相談はしない（「一般的なルールの目安」に留める）。

### 10-6 個人情報
- `notes` に保存する文字列は、`PII_RE`（メールアドレス・電話番号・郵便番号らしき文字列・〒）に当たる部分を「＊」に置き換え、先頭30字までにする（`scrub_note`）。
- ログ（`state.log`）には発言の原文を残さない。

---

## §11 テスト

### 11-1 単体チェック（`unit_checks_v4.py`、131件）
| 節 | 内容 |
|---|---|
| 1 | バンクの整合：すべての `choice_effects` が正規値（または NG・セグメント）である。シナリオ等が HARD・fact に書かない |
| 2 | ボタンの決定性：同じボタンで同じ状態になり、LLM を呼ばない |
| 3 | 検証の境界値：null・欠落・型違い・定義外スロット・JSON でない出力 |
| 4 | 年齢の解析：「中2」→ `under_15`、「大学2年」→ 中学生扱いしない、など |
| 5 | 未成年ルール：年齢不明は未成年扱い、年齢訂正で `derived_ng` 再計算、深夜の除去、選択肢の非表示 |
| 6 | 中学生以下の停止、判定の範囲（§10-1） |
| 7 | 聞き返し・再提示は質問を消費しない（回数・ターン） |
| 8 | 確信度はハーネスが決める（LLM の `confidence` を無視） |
| 9 | 保存・復元の往復（`to_dict`→JSON→`from_dict`） |
| 10 | セグメント：既知→既知の自由入力は確認質問、`unknown` に戻らない、退避と復元 |
| 11 | HARD の保証：空の HARD はカードで未定、結果で未定一覧。HARD の推測は「全部OK」でのみ昇格。矛盾は全候補つきで未定 |
| 12 | review の返事に締めの一文がちょうど1回 |
| 13 | 結果：許可リスト・件数・金額 |
| 14 | 脱線・危険話題は数えない、やり直しで初期化 |
| 15 | 「こだわりなし」：pref では有効、fact では却下 |
| 16 | プロンプト：各段のすべての `{{変数}}` がハーネスから供給される |
| 17 | 付録C の関数・定数がすべて存在する |
| 18 | 本書の規範ブロック（REGEX・CONST・TEXT）と `Q_SEGMENT_CHOICES` が実装と一致する |
| 19 | `MONEY_RE`・`DANGER_RE` がハーネス所有の文・バンク・カタログに当たらない。「1,200円以上」は金額でない。付録A・B の件数 |
| 20 | ターンの計上（§5-3）：`phase_turns` の合計＝`turns`、計上先、上限 |
| 21 | 脱線・危険話題の再提示（§5-6） |
| 22 | review・確認質問での抽出失敗（§5-5） |
| 23 | 結果の後の発言（§5-7） |
| 24 | 中学生以下の判定の範囲、low の `under_15`（§10-1） |
| 25 | 結果時点の low の HARD（§7-6） |
| 26 | 事後チェックの補充、カードの tier、抽出の切り詰め、複数選択の区切り |
| 27 | 内部例外のロールバック（§5-4） |
| 28 | 「早く結果を見せて」（§9-4） |

### 11-2 ペルソナ（`sim_v4.py`、20人）
- 通常8人：`hs16`（高校生16歳）・`univ`（大学生）・`homemaker`・`office`（副業）・`nopref`（こだわりなし多め）・`unclear`（あいまい回答）・`late_seg`（セグメントが後から判明）・`no_seg`（セグメントを答えない）。
- 敵対的12人：`age_skip`（年齢ひみつ）・`hm17`（17歳の主婦）・`sj17`（17歳の副業・深夜希望）・`jhs`（中学生）・`freeter`（その他）・`seg_flip`（途中でセグメント反転）・`dropout`（途中離脱→保存→再開、やり直し）・`spam_high`（LLM が何でも high・他スロットを乱発）・`malformed`（JSON 欠損・型違い・壊れた出力）・`danger`（闇バイト的な入力）・`early_exit`（早く結果を見せて）・`age_fix`（年齢を後から訂正）。
- ペルソナは提示中の質問 ID（`qid`）をキーに応答する。台本に無い `qid` には、選択肢のうち `crc32(ペルソナ名＋qid) mod 選択肢数` 番目を選ぶ（決定的）。
- モック LLM は v3 と v4 で同じ壊し方をする。

### 11-3 合格条件
- §4-1 の合否指標がすべて許容値どおり（`v4_agg` が全項目 0、`max_turns` ≤ 15）。
- `unit_checks_v4.py` が 131/131。
- v3 の資産が壊れていない：`python3 simulate.py v3` が終了コード0で、`results.json` が変わらない。`unit_checks_v3.py` が終了コード0。

### 11-4 再現コマンドと移植版の検査（05-B12）
```bash
cd /workspace/baito_harness
python3 unit_checks_v4.py                                   # 131/131 passed
python3 simulate.py v4                                      # results_v4.json と表を出力
BAITO_HARNESS=aica_port python3 unit_checks_v4.py           # 移植版を同じ単体チェックで検査
BAITO_HARNESS=aica_port python3 simulate.py v4              # 移植版を同じペルソナで検査
python3 simulate.py v3                                      # v3 の再現（results.json は変わらない）
python3 unit_checks_v3.py
```
- `BAITO_HARNESS` は Python のモジュール名である（既定は `harness_v4`）。移植版は付録C の名前をすべて持つ Python モジュール（aica の本体が別言語なら、その本体を呼ぶ薄い Python ラッパー）として用意する。`unit_checks_v4.py` と `sim_v4.py` はこの変数でモジュールを差し替える。
- 単体チェックの節18 は、本書の規範ブロックと移植版の値を比べる。食い違ったら**移植版（または参照実装）を直す**（§0-1）。

### 11-5 v3 と v4 の比較（基準値）
次の表は §4-2 の基準値である。`python3 simulate.py v4` の出力（`results_v4.json`）から作り、規則を変えたら作り直す（§0-1）。v3 の列は `python3 simulate.py v3` の結果である。

| ペルソナ | v3 ターン | v3 シナリオ | v3 の問題 | v4 ターン | v4 配分（ib/hf/sc/rp/fg/rv） | v4 の結果時点の状態 |
|---|---|---|---|---|---|---|
| hs16 | 14 | 6 | — | 13 | 2/4/4/1/1/1 | 未成年モード。修復2・再試行成功0・代替0 |
| univ | 14 | 5 | — | 13 | 2/5/3/1/1/1 | なし |
| homemaker | 14 | 5 | — | 14 | 2/5/3/1/2/1 | 修復1・再試行成功0・代替0 |
| office | 14 | 4 | — | 15 | 2/6/3/1/1/2 | 両候補つきの未定：接客。MID 空：接客。修復1・再試行成功0・代替0 |
| nopref | 14 | 5 | — | 13 | 2/5/3/1/1/1 | 修復7・再試行成功0・代替0 |
| unclear | 15（打ち切り） | 2 | 同じ質問の繰り返し。MID 空：勤務時間・接客・体力 | 14 | 2/6/3/1/1/1 | MID 空：時給の下限・1回の勤務時間・接客・体力面・本業後の体力。review 前に空の HARD：時間帯・通勤・平日・週末。聞き返し2回。言い換え再質問：hf_weekday。修復1・再試行成功0・代替0 |
| late_seg | 14 | 5 | MID 空：体力 | 14 | 2/6/3/1/1/1 | MID 空：時給の下限・体力面。修復2・再試行成功0・代替0 |
| no_seg | 15（打ち切り） | 6 | MID 空：テスト期間 | 14 | 2/4/4/1/1/2 | MID 空：テスト期間。review 前に空の HARD：学校の許可。修復1・再試行成功0・代替0 |
| age_skip | 14 | — | 未成年なのに深夜 NG なし | 13 | 2/5/3/1/1/1 | 未成年モード |
| hm17 | 14 | — | 未成年なのに深夜 NG なし | 14 | 2/5/3/1/2/1 | MID 空：時給の下限。未成年モード。言い換え再質問：hf_days。修復1・再試行成功0・代替0 |
| sj17 | 14 | — | 未成年なのに深夜 NG なし | 15 | 2/5/3/1/2/2 | HARD の未定：時間帯（明示）。両候補つきの未定：体力面。MID 空：時給の下限・体力面。review 前に空の HARD：時間帯。未成年モード。言い換え再質問：hf_time。修復3・再試行成功0・代替0 |
| jhs | 14 | — | 中学生なのに推薦 | 3 | 2/1/0/0/0/0 | 停止（under_15、推薦0件） |
| freeter | 14 | — | — | 13 | 2/4/4/1/1/1 | なし |
| seg_flip | 14 | — | — | 15 | 2/5/3/1/2/2 | HARD の未定：通勤（明示）。MID 空：時給の下限・1回の勤務時間・本業後の体力。review 前に空の HARD：通勤 |
| dropout | 14 | — | — | 14 | 2/5/3/1/2/1 | やり直し1回 |
| spam_high | 12 | — | — | 15 | 2/6/3/1/1/2 | HARD の未定：通勤・週の日数・副業の可否（明示）。両候補つきの未定：副業の可否・週の日数・通勤・体力面。MID 空：時給の下限・体力面。review 前に low の HARD：週の日数・通勤・副業の可否 |
| malformed | 3（停止） | — | `KeyError('confidence')` で異常終了 | 15 | 2/5/3/1/2/2 | 両候補つきの未定：接客。MID 空：接客。修復12・再試行成功8・代替3 |
| danger | 14 | — | 闇バイトの注意なし | 13 | 2/5/3/1/1/1 | 未成年モード。数えない脱線・危険話題2回。危険話題の注意文あり |
| early_exit | 14 | — | 早期終了せず14ターンまで続いた | 6 | 2/3/0/0/0/1 | HARD の未定：通勤・週の日数・時間帯・平日・週末（明示）。MID 空：時給の下限・1回の勤務時間・接客・体力面・本業後の体力。review 前に空の HARD：週の日数・時間帯・通勤・平日・週末 |
| age_fix | 14 | — | — | 15 | 2/5/3/1/2/2 | 修復1・再試行成功0・代替0 |

- v4 の集計（`v4_agg`）：異常終了 0・ループ 0・未到達 0・未成年違反 0・結果違反 0・黙った空の HARD 0・取りこぼし 0・無関係な質問 0・無駄な質問 0・繰り返し 0・HARD の乱発 high 0・黙った low の HARD 0・計上の不一致 0・フェーズ上限違反 0・15ターン超過 0・最大ターン 15
- 「全部OK」以外で HARD の推測が確定した件数（`review_accepted` の HARD）は全ペルソナで0である。
- 結果時点で low のまま残った HARD（`guessed_hard`）は全ペルソナで0件である（spam_high の low の HARD は review で矛盾となり、未定として明示された）。

---

## §12 既知の課題と暫定事項

### 12-1 既知の課題（今回は直さない）
1. 実 LLM での検証がまだである。モック LLM による結果は §11-5 のとおりだが、実 LLM での抽出精度・質問文の逐語率は §13 P3 で測る。
2. 職種カタログ（24件）は参考用であり、勤務時間・地域・時給のデータを持たない。そのため時給・通勤・勤務時間・日数・目的は絞り込みに使わず、未成年の労働時間上限はシフトの自動補正をせず注意文で知らせる。
3. `unclear` 型（あいまい回答が続く）では MID が空のまま結果に進むことがある（結果では空の HARD だけを未定として明示する）。
4. 18歳以上の高校生（学校のルールによる制限）は、学校の許可の質問で扱い、年齢帯は分けていない。
5. セグメントは排他的な1値であり、「学生かつ子育て中」のような組み合わせは `other`＋自由入力で扱う。
6. `DANGER_RE` は誤検知がありうる（§10-4）。

### 12-2 解消しない矛盾の扱い【暫定・ユーザー確認待ち】
- review を2巡しても矛盾が解けないスロット、または2巡目を出さずに結果に進んだスロットは、**値を消して「未定」とし、記録したすべての候補を結果文に並べる**（「未定：接客（好き / ふつう のどちらか。決まったら教えてね）」）。
- 推薦の絞り込みでは、そのスロットを「空」として扱う（どの値でも除外しない）。
- この扱いは**暫定**であり、ユーザー（aica 側の責任者）の確認を待っている。代案は「最後に聞いた値を採用する」「確信度の高い方を採用する」である。確認の結果が変わっても、変更点は `finalize()` と `compose_result_text()` の2か所に閉じる。
- 合否指標（§4-1）はこの決定に依存しない。M-silent は「未定を黙って捨てない」ことだけを検査する。

---

## §13 aica への移行と完了の定義

### 13-1 進め方と完了の定義
aica は既存のプロンプトとハーネスを持っている。aica は本書と付録A・B に従って実装する（`harness_v4.py` をそのまま組み込んでもよい）。各段階は「完了の定義」に通るまで次へ進まない。完了の定義はすべて §11-4 のコマンドで、`BAITO_HARNESS=<移植モジュール>` を付けて実行できる。今回の完了範囲は P0〜P4 であり、§12-1 の既知課題は別チケットとする。

| 段階 | 内容 | 完了の定義（`unit_checks_v4.py` の節は全件合格） |
|---|---|---|
| P0 土台 | 状態（付録D）、スロットと正規値（§6）、バンク（付録A）、統合規則（§7-5）、確信度（§7-2）、未成年モード・中学生の停止（§10-1）、規範ブロック | 節1・2・4・5・6・8・9・15・17・18・19・24 |
| P1 ターンとフロー | 3段構成の `turn()`（§5-4）、ターンの計上（§5-3）、フェーズ遷移（§5-2）、質問の選び方（§8-3）、セグメント（§3）、脱線・失敗・内部例外 | 節3・7・10・14・20・21・22・27・28 が合格し、`simulate.py v4` の `v4_agg` で crash・loop・not_reached・irrelevant・wasted・repeated・phase_turns_mismatch・phase_cap_violation・over_max_turns が0 |
| P2 review と結果 | カード（§8-6）、締めの一文、昇格、2巡目、`finalize`、結果（§8-7）、事後チェックと補充、注意文（§10-5）、結果後（§5-7） | 節11・12・13・23・25・26 が合格し、`v4_agg` の全項目が0、`max_turns` ≤ 15 |
| P3 プロンプトと実 LLM | プロンプト4ファイル（§9）、`render_prompt`、検証・再試行・代替（§8-4） | 節16 が合格し、下の P3 の測定がすべて基準を満たす |
| P4 受け入れと本番導入 | 移植版を同じペルソナで検査し、影運用を行う | 下の P4 の条件を満たす |

**P3 の測定（05-M17）**：実 LLM で、`sim_v4.py` の20ペルソナを各3回（計60会話）流す。ペルソナの応答は §11-2 のとおり `qid` をキーにし、質問の順がずれても台本に無い `qid` には既定の規則で答える。
| 指標 | 式 | 基準 |
|---|---|---|
| 生成合格率 | 生成段（`generate` と `result`）の1回目の呼び出しのうち、検証に合格した数 ÷ 生成段の1回目の呼び出し数 | 95%以上 |
| 代替率 | ログ `generate_fallback`・`result_fallback` の数 ÷ 生成段の1回目の呼び出し数 | 2%以下 |
| 抽出合格率 | 抽出段の1回目の呼び出しのうち、`validate_extract` が使える出力（`None` でない）を返した数 ÷ 抽出段の1回目の呼び出し数（ボタン扱いで呼ばなかったものは含めない） | 98%以上 |
| 合否指標 | §4-1 を60会話で集計 | すべて0 |

**P4 の条件（05-M18・M-19）**
1. `BAITO_HARNESS=<移植モジュール> python3 simulate.py v4` で §4-1 がすべて0であり、§4-2 のうち「P4 の合否に使う」とした指標が §11-5 の基準値の範囲内である。
2. **影運用**：v4 を本番の会話と並走させる。v3 の実会話を v4 に流すと質問がずれるため、**v3 の会話を再生はしない**。代わりに、新規会話の一部（aica が選ぶ。上限は全体の10%）を v4 で処理し、ユーザーには v4 の返事を見せない（v3 の返事を見せる）。v4 側では各ターンのユーザー発言を v3 の質問への回答として `turn()` に入れ、結果時点の状態を記録する。件数は200会話以上とし、未成年違反・結果違反（§4-1 の判定関数を記録に適用する。判定は自動、確認は aica の担当者）が0であること。
3. **本番切り替え**は、§16 の確認事項のうち §16-1 の法務確認（注意文）と §12-2 の確認が済んだ後に行う。影運用はその前に行ってよい。確認が済むまでの既定値は本書の記載どおりとする。

### 13-2 v3 → v4 の入出力の対応
| v3（`harness_v3.step`） | v4（`harness_v4.turn`） |
|---|---|
| `step(state, model_output, bank)`：モデルの JSON を受け取って次の質問を返す。最初は `step(state, None)` | `start(state, bank)` で最初の質問を出し、以後は `turn(state, user_text, bank, llm)`。モデル呼び出しはハーネスの中で行う |
| モデル出力 `{reply, choices, slot_updates[{slot,value,confidence,from}], ng_updates, segment}` | 抽出 `{slot_updates[{slot,value,evidence}], ng_updates, ng_removals, segment, intent, review_ack}` と、生成 `{reply}` に分離。`choices`・`confidence`・`from` はモデルに出させない（出しても無視） |
| `age_under_18`（真偽値） | `age_band`（5値）。抽出で旧名が来たら `age_band` に読み替える |
| `atmosphere` | `teaching_style`・`social`・`coworker_age` に分割 |
| `hourly_wage_min`（文字列と数値が混在） | `hourly_wage_min`（整数）と `wage_priority` |
| `set_value_labels(bank)` の呼び出し | 不要（表示名は `SLOTS` に内蔵） |
| カードの `text` | `view["card_text"]`（締めの一文を含まない）と `view["closing"]` |
| `prompt_review_ja.txt` を常時または review 時に連結 | §9-1 の段ごとの結合（常時オンは main だけ） |
| 結果はプロンプト任せ | 結果材料 `result_json` → 出力 JSON → 検証 → 事後チェック → `compose_result_text` |

### 13-3 切り替えと切り戻し
- 機能フラグ `BAITO_HARNESS_VERSION=v3|v4` を aica に設ける。会話の開始時にフラグを読み、`state.version` に保存する。**会話の途中で版は変えない**（保存済みの v3 の会話は v3 で最後まで進める）。
- 保存状態に `version` キーが無ければ v3 の会話とみなす（05-N5）。
- 切り戻しはフラグを `v3` に戻すだけである。以後の**新しい会話**から v3 になる。v4 で始まった会話は v4 のまま終える（v4 の状態を v3 に変換しない）。
- シミュレーターと単体チェックの差し替え口は環境変数 `BAITO_HARNESS`（§11-4）である。

---

## §14 ファイル一式
次のディレクトリ一式（`/workspace/baito_harness/`）をそのまま aica に渡す。規範は本書と付録A・B のファイルであり、それ以外の v4 のファイルは参照実装・検査用である（§0-1）。

| ファイル | 版 | 位置づけ | 内容 |
|---|---|---|---|
| `PRD_baito_agent_v4.md` | v4 | 規範 | 本書 |
| `bank_v4.py` | v4 | 規範（付録A） | 質問バンク49問（`BANK`）。`CATALOG` は `catalog_v4.py` の再公開 |
| `catalog_v4.py` | v4 | 規範（付録B） | 職種カタログ24件（`CATALOG`） |
| `harness_v4.py` | v4 | 参照実装 | ハーネス |
| `prompt_main_v4_ja.txt` | v4 | 参照実装 | 常時オンの会話プロンプト |
| `prompt_extract_v4_ja.txt` | v4 | 参照実装（必須項目は §9-3） | 抽出プロンプト |
| `prompt_review_v4_ja.txt` | v4 | 参照実装 | review 追加プロンプト |
| `prompt_result_v4_ja.txt` | v4 | 参照実装 | 結果追加プロンプト |
| `sim_v4.py` | v4 | 検査 | 20ペルソナとモック LLM、指標計算 |
| `unit_checks_v4.py` | v4 | 検査 | 単体チェック131件 |
| `results_v4.json`、`transcripts/v4_*.txt` | v4 | 基準値 | シミュレーション結果と会話ログ（20件） |
| `simulate.py` | 共通 | 検査 | 入口（`v3`／`v4`） |
| `PRD_baito_agent.md`、`harness_v3.py`、`bank.py`、`prompt_review_ja.txt`、`unit_checks_v3.py`、`results.json` | v3 | 旧版 | 変更していない。比較と切り戻し用 |
| `harness.py`、`harness_v2*.py`、`*.diff`、`summary*.txt`、`unit_checks.py` | v1〜v3 | 記録 | 経緯の記録 |

---

## §15 変更履歴（v3 → v4）
5つのレビュー（01 理解度チェック、02 設計批評、03 コード監査、04 実装準備チェック、05 実装準備度チェック）の全指摘 151件について、対応を記す。「対応」は直したもの、「一部見送り」は一部を直し残りを理由つきで見送ったものである。全面的な見送りは無い。15-2〜15-5 の「節」の列は v4.1 の節番号に読み替えてある。

### 15-1 集計
| レビュー | 件数 | 対応 | 一部見送り | 見送り |
|---|---|---|---|---|
| 01（1〜30） | 30 | 30 | 0 | 0 |
| 02（#1〜#15＋補足） | 16 | 13 | 3（#3・#8・#15） | 0 |
| 03（A1〜A13・B1〜B8・C1〜C6・D1〜D12） | 39 | 39 | 0 | 0 |
| 04（B1〜B9・M1〜M11・m1〜m4） | 24 | 24 | 0 | 0 |
| 05（B-1〜B-12・M-1〜M-20・N-1〜N-10） | 42 | 42 | 0 | 0 |
| 合計 | 151 | 148 | 3 | 0 |

注：04-B1（指数バックオフ）・04-B6（`cited_slots`）・05-B11・05-M6・05-M11・05-M20 は、提案と違う形で対応した（各行の備考を参照）。

### 15-2 01 理解度チェック
| ID | 指摘の要旨 | v4 での対応 | 節 |
|---|---|---|---|
| 01-1 | 1回の出力で抽出と次の質問を同時に作れない | 1ターンを抽出→判断→生成の3段に分けた | §5-4 |
| 01-2 | 「プロンプトはどう言うかだけ」が実態と違う | 選択肢→値の対応・遷移・終了・確信度をハーネスに移した。LLM は抽出と言い回しだけ | §0-1・§5-4・§7 |
| 01-3 | モデルが choice_effects を知る経路がない | ボタンはハーネスが直接適用し LLM を呼ばない。自由入力には `allowed_slots`（正規値）を渡す | §5-4・§8-5 |
| 01-4 | 直接質問→high をハーネスが強制していない | `confidence_for` でハーネスが決める | §7-2 |
| 01-5 | `from:"review"` がモデル任せ | `from` はハーネスが付ける。値一覧を定義 | §7-3 |
| 01-6 | icebreak の `phase_turns≥3` が効かない | 終了条件を「セグメント判明 または 3ターン」に再定義し理由を記述 | §5-2 |
| 01-7 | セグメント質問の必須出題と自由入力の判明がぶつかる | `q_segment` はセグメント未判明のときだけ出す | §3-2・§8-3 |
| 01-8 | 標準配分の足し算が合わない | 配分は実測の典型値とし、上限は遷移判定だけで決まると明記 | §5-1 |
| 01-9 | 遷移表の行順とコードが逆 | 評価順の表を新設し、コードと一致させた | §5-2 |
| 01-10 | attempts≥2 の除外が未記載 | `remaining_gaps` の定義に明記 | §8-3 |
| 01-11 | HARD が必ず埋まる保証がない | 保証を「聞く・見せる・勝手に確定しない・黙って捨てない」に再定義しコードで強制 | §7-6 |
| 01-12 | 「low の HARD=0」が自動昇格で満たされる | HARD の推測は「全部OK」のときだけ昇格。review 前の値を Q-pre で記録 | §8-6・§4-2 |
| 01-13 | 聞き返しと「next_question だけ聞く」が両立しない | 聞き返しをハーネスのモード（clarify）にし、質問文もハーネスが渡す | §9-4・§9-5 |
| 01-14 | スキップ＝こだわりなし（high）と確信度の衝突 | 確信度はフェーズと出どころで決まる。fact の「こだわりなし」は却下 | §6-3・§7-2・§7-5 |
| 01-15 | 「同じ話題は2回まで」の対象 | 直接質問は `MAX_ATTEMPTS`、全フェーズの話題は `TOPIC_CAP` で数える | §8-1・§8-3 |
| 01-16 | purpose と atmosphere が多観点 | atmosphere を3スロットに分割、purpose は複数値 | §6-1 |
| 01-17 | hourly_wage_min の型の混在 | 下限（整数）と優先度に分割 | §6-1 |
| 01-18 | `set_value_labels` の呼び出し規約 | 廃止（表示名を `SLOTS` に内蔵） | §6-1 |
| 01-19 | 画面に出ていない値まで昇格する | カードに表示した値だけを昇格 | §8-6 |
| 01-20 | 「HARD が変更された」の範囲 | 「元が high の HARD が別の値に変わった」と定義 | §8-6 |
| 01-21 | segment に確信度がない | 既知→既知の自由入力は確認質問で決める | §3-2 |
| 01-22 | 年齢を student と unknown にしか聞かない | 全セグメント共通の HARD とし最初に聞く | §8-3・§10-1 |
| 01-23 | 未成年の深夜 NG を外せない・矛盾が残る | `derived_ng` を年齢から毎回再計算。深夜の選択肢は表示しない | §6-5・§10-1 |
| 01-24 | 締めの文言が2つある | `CLOSING_JA` 1つをハーネスが所有し、返事にちょうど1回 | §8-6 |
| 01-25 | 結果のカードの作り方がない | 結果材料・出力・検証・事後チェック・結果文を定義 | §8-7 |
| 01-26 | review プロンプトの入れ方が2通り | review・result のときだけ連結すると固定 | §9-1 |
| 01-27 | 入口と例外時の挙動がない | `start`／`resume`（冪等）と、LLM 例外時の扱いを定義 | §8-2・付録C・§8-4 |
| 01-28 | `wasted` の定義があいまい | 「全対象の不足度が0」と式で定義 | §4-1 |
| 01-29 | pending の扱い | 推薦に使わず、取りこぼしに数えないと明記 | §3-3 |
| 01-30 | 数値の出典が検証できない | 数値はすべて再現コマンドで再生成できるものに限定 | §0-1・§11-4 |

### 15-3 02 設計批評
| ID | 指摘の要旨 | v4 での対応 | 節 |
|---|---|---|---|
| 02-#1 | 未成年保護が1つの値に依存しすり抜ける | `age_band` 5値、全セグメントで最初に聞く、未回答・ひみつは未成年扱い、事後チェック | §10-1 |
| 02-#2 | 未成年と深夜OKが共存し、禁止がプロンプト頼み | 深夜・重労働の選択肢を非表示、統合時に除去、結果の事後チェック | §7-5・§10-1 |
| 02-#3 | 年少者の法令が深夜以外ない | 中学生以下の停止、8時間・40時間の注意、重労働の除外、未成年に引越しを出さない。**一部見送り**：①シフト時間の自動補正（カタログに勤務時間のデータが無い）②「18歳以上の高校生」の年齢帯（学校の許可の質問で扱う。年齢の質問を増やすとターン予算を超える） | §10-1・§12-1 |
| 02-#4 | 確信度・値の対応が無検証 | ハーネスが確信度を決め、出力を検証・正規化 | §7-2・§8-4 |
| 02-#5 | review の自動昇格で HARD の推測が確定 | HARD は「全部OK」のときだけ昇格 | §8-6 |
| 02-#6 | あいまいな回答で HARD が空のまま推薦 | 言い換え再質問、2巡目の focus、結果で未定を明示 | §7-6 |
| 02-#7 | 学校で禁止・副業 NG でも通常どおり推薦 | 「禁止」と「わからない」を区別、公務員を追加、条件付きの結果文と注意文 | §10-2 |
| 02-#8 | 3択に当てはまらない人がいる・子ども前提の文言 | `other`（フリーター・求職中・その他）を追加、文言を中立化。**一部見送り**：属性フラグの組み合わせ型（質問数とターン予算が増えるため。`other`＋自由入力で代替） | §3・§12-1 |
| 02-#9 | セグメントの無確認上書きで情報が消える | 確認質問、セグメント固有スロットの退避と復元 | §3-2・§3-3 |
| 02-#10 | スロット間の矛盾を検知しない | 6つの整合チェックをカードの⚠️行で表示 | §6-4 |
| 02-#11 | 同じスロットに互換性のない値が混在 | 正規値の列挙と正規化 | §6-1・§6-3 |
| 02-#12 | 聞き返しが設計と衝突、脱線が未定義 | intent（clarify・aside・danger 等）とハーネスの処理を定義 | §9-4 |
| 02-#13 | 途中離脱・早期終了・再開がない | 保存・`resume`・「早く結果」・やり直しを定義 | §5-3・§8-2・付録C・§8-8 |
| 02-#14 | 闇バイト・個人情報の扱いがない | 危険語チェックと注意文（#9110）、許可リスト、伏せ字 | §10-4・§10-6 |
| 02-#15 | 扶養が主婦だけ・使い道から推測 | 扶養は直接質問だけで設定、シナリオから除外、注意文を固定化、副業向けに `side_tax`。**一部見送り**：学生の扶養スロット（ターン予算のため。結果で `student_fuyou` の注意文を出す） | §7-2・§10-3 |
| 02-補足 | 敵対的ペルソナ不足・計測の定義 | 提案の10種をすべて含む敵対的ペルソナ12人を追加。合否指標を式で定義し、review 前の値は Q-pre として別に記録 | §4・§11-2 |

### 15-4 03 コード監査
| ID | 指摘の要旨 | v4 での対応 | 節 |
|---|---|---|---|
| 03-A1 | `__ng__`／`__segment__` をハーネスが適用しない | ハーネスがボタンの効果としてすべて適用 | §7-1 |
| 03-A2 | 確信度をハーネスが付けない | `confidence_for` | §7-2 |
| 03-A3 | 主婦・副業では未成年の深夜 NG が発動しない | 年齢を全セグメント共通にし、`derived_ng` をセグメントに関係なく計算 | §10-1 |
| 03-A4 | 未成年の深夜OKがそのまま確定 | 選択肢の非表示と統合時の除去 | §7-5 |
| 03-A5 | HARD の「こだわりなし」が充足扱い | fact は却下、年齢は `declined`（未成年扱い） | §7-5 |
| 03-A6 | HARD の充足がコードで保証されない | HARD の保証を再定義しコードで強制 | §7-6 |
| 03-A7 | 聞き返しの経路がない | clarify モード | §9-4 |
| 03-A8 | 話題の上限が直接質問しか数えない | `TOPIC_CAP` | §8-3 |
| 03-A9 | 締め文言が2つ・二重に出る | `CLOSING_JA` の単一所有と検証 | §8-6 |
| 03-A10 | 結果でカードを渡さず検証もしない | 結果材料にカードを含め、検証と事後チェック | §8-7 |
| 03-A11 | `from:"review"` を付けない | `apply_review` が付ける | §7-3 |
| 03-A12 | reverse_pick のスキップ条件 | 各セグメントに1問（other・unknown は共通問） | §5-2・§7-2 |
| 03-A13 | icebreak の条件が到達しない | 再定義 | §5-2 |
| 03-B1 | `open_gaps` の除外が未記載 | 明記 | §8-3 |
| 03-B2 | `step(state, None)` の規約と非冪等性 | `start`／`resume` を冪等にし、ターンに数えない | §8-2・付録C |
| 03-B3 | `set_value_labels` の呼び出しが必要 | 廃止 | §6-1 |
| 03-B4 | シナリオ終了条件の記述ずれ | 条件を再定義しコードと一致 | §5-2 |
| 03-B5 | review 質問の形が未記載 | `view` とカード JSON を定義 | §8-5・§8-6 |
| 03-B6 | pending に不正なスロット、不正な segment を受け入れる | 定義外スロットは破棄、定義外・unknown の segment は無視してログ | §3-2・§7-5 |
| 03-B7 | 旧セグメントのスロットが見えないまま昇格 | 退避（`archived`）してカードにも昇格にも使わない | §3-3・§8-6 |
| 03-B8 | モデルの `choices` を無視している | 選択肢はバンクが正。モデルには出させない | §8-5・§13-2 |
| 03-C1 | `card_text` がコードに無い | `view["card_text"]` を追加 | §8-5 |
| 03-C2 | 標準配分の合計が合わない | 実測の典型値に置き換え | §5-1 |
| 03-C3 | FIX3 の例（年齢）が成り立たない | v3 の例を削除し、v4 の処理例に置き換え | §5-5 |
| 03-C4 | 判定順序が PRD とコードで逆 | 評価順の表で統一 | §5-2 |
| 03-C5 | です・ます調とタメ口の不一致 | ユーザー向けはタメ口寄りに統一 | §9-2 |
| 03-C6 | 指標がコンソールに出ない | 全指標を表と JSON に出力 | §4・§11-4 |
| 03-D1 | 1回の呼び出しでリアクションと次の質問を作れない | 3段構成 | §5-4 |
| 03-D2 | LLM 出力の検証が無くクラッシュする | 検証・修復・再試行・代替。`malformed` ペルソナで確認 | §8-4・§11-5 |
| 03-D3 | スロットの多義化による偽の矛盾 | スロットの分割 | §6-1 |
| 03-D4 | 値の列挙が質問ごとにばらばら | 正規値と正規化、バンクの検査 | §6-1・§6-3・§11-1 |
| 03-D5 | 複数選択を表現できない | `multi` と和集合 | §6-1・§7-1 |
| 03-D6 | 年齢訂正で深夜 NG を外せない | `recompute_derived` | §6-5・§7-5 |
| 03-D7 | `{{next_question}}`／`{{slots_json}}` の形式が未定義 | 形式を定義し、変数の完全性を検査 | §8-5・§11-1 |
| 03-D8 | State を JSON にできない・保存方法が未定義 | `to_dict`／`from_dict`、毎ターン保存 | §8-8 |
| 03-D9 | 既存実装からの移行手順がない | 入出力の対応・機能フラグ・切り戻し | §13 |
| 03-D10 | おすすめの根拠データが無い | 職種カタログ24件（許可リスト） | §8-7 |
| 03-D11 | `VALUE_JA` がグローバルな可変状態 | 表示名を不変の `SLOTS` に内蔵 | §6-1 |
| 03-D12 | 「おこづかい」→扶養内の推測が強すぎる | シナリオから扶養を外し、直接質問だけで設定 | §7-2・§10-3 |

### 15-5 04 実装準備チェック
| ID | 指摘の要旨 | v4 での対応 | 節 |
|---|---|---|---|
| 04-B1 | LLM の不正出力・エラー時の扱いがない | 検証・再試行1回・代替を段ごとに定義。**提案との違い**：指数バックオフとタイムアウトは aica の LLM クライアント層の責務とし、ハーネスは例外を失敗1回として数える | §8-4 |
| 04-B2 | 処理順序とターンの定義があいまい | 3段構成とターンの定義表 | §5-3・§5-4 |
| 04-B3 | choice_effects を適用するのは誰か | ハーネス | §7-1 |
| 04-B4 | 確信度を最終的に決めるのは誰か | ハーネス（モデルの申告は無視） | §7-2 |
| 04-B5 | プロンプト変数の形式が未定義 | `view`・抽出ペイロード・`slots_json` を定義、`render_prompt` で欠落を検出 | §8-5・§9-1 |
| 04-B6 | 結果の出力スキーマがない | 結果材料と出力 JSON、検証と結果文。**提案との違い**：`cited_slots` はモデルに出させず、ハーネスが `why_slots` を持つ | §8-7 |
| 04-B7 | 指標の定義と aica 実装をテストにつなぐ方法 | 指標を式で定義、`BAITO_HARNESS` で aica の移植版を差し替えて検査 | §4・§11-4・§13-3 |
| 04-B8 | §12-2 が未決定なのに基準が前提にしている | 「未定（両候補）」を暫定で実装し、合否指標をこの決定から独立させた | §12-2 |
| 04-B9 | 聞き返しと「next_question だけ」の衝突 | clarify モードで質問文もハーネスが渡す | §9-4・§9-5 |
| 04-M1 | フェーズを飛ばすときの細部 | `n` はフェーズごとのターン数（v4.1 で `phase_turns` として §5-3 に定義）。質問が無ければ同じターン内で次のフェーズへ進む（フェーズ数で打ち切り） | §5-2 |
| 04-M2 | 同点時の選び方 | バンクの並び順で決定的に選ぶ | §8-3 |
| 04-M3 | マージ規則の抜け | 統合規則の表 | §7-5 |
| 04-M4 | value の型と「こだわりなし」 | 正規値の列挙、「こだわりなし」は pref だけ有効 | §6-1・§6-3 |
| 04-M5 | 「原文どおり」と口調の矛盾 | 口調はタメ口寄りに統一。v4.1 で、規範は §9 の規則とし、プロンプトファイルは参照実装と改めた（05-B4） | §9-1・§9-2 |
| 04-M6 | review プロンプトを常時入れるか | review・result のときだけ | §9-1 |
| 04-M7 | 2巡目のカードは全項目か | 問題の項目（`focus`）と NG・⚠️ 行だけ | §8-6 |
| 04-M8 | ラベルが無い値の扱い | 正規化できない値は捨てずにメモとして保持し、カードに「未定（メモ：…）」で表示 | §6-3・§8-6 |
| 04-M9 | 「コードで強制」の範囲 | 金額は出力の検証で落とす（`MONEY_RE`）、未成年に深夜の選択肢を出さないことを P0 に入れた | §8-4・§10・§13-1 |
| 04-M10 | NG 文字列の正規化 | 自動 NG は固定文字列、重複は NFKC 後の完全一致で判定 | §6-5 |
| 04-M11 | 作業順序と完了の定義 | P0〜P4 の完了の定義とテスト | §13-1 |
| 04-m1 | `from` の値の一覧 | 一覧を定義 | §7-3 |
| 04-m2 | 繰り返しの定義 | 言い換えの再質問は数えないと定義 | §4-1 |
| 04-m3 | segment を unknown に戻せるか | 戻さないと明記 | §3-2 |
| 04-m4 | ファイルを渡す経路 | ディレクトリ一式を同梱すると明記 | §14 |

### 15-6 05 実装準備度チェック（v4.1）
| ID | 指摘の要旨 | v4.1 での対応 | 節 |
|---|---|---|---|
| 05-B-1 | 質問バンクの中身が PRD に無い | 49問を本文に転記せず、`bank_v4.py` を規範の付録A とした（パス・役割・スキーマ・フェーズ別件数・並び順＝同点判定の正式な順序）。`q_segment` の文言は TEXT ブロックの `Q_SEGMENT_CHOICES` に本文で記載 | 付録A・§3-2・§10-5 |
| 05-B-2 | 職種カタログのスキーマ・中身・点数の重みが無い | `catalog_v4.py` を規範の付録B とし、スキーマ・値域を記載。除外条件・加点表・`ng_hits` を本文に記載 | 付録B・§8-7・§6-5 |
| 05-B-3 | 正規表現が定義されていない | 7本の正規表現を REGEX ブロックで本文に記載（実装と一致を節18で検査）。`MONEY_RE` は「数字＋万」と扶養ラインの数値（103万など）だけを対象とし、「1,200円以上」などの時給の表示名・選択肢には当たらない（節19で検査） | §5-8・§11-1 |
| 05-B-4 | 何を正とするかが矛盾、基準値が規則と合わない | 本書（＋付録A・B）が規範、`harness_v4.py`・プロンプトは参照実装、と全節で統一。規範の値は REGEX・CONST・TEXT ブロックにまとめ、節18で照合。基準値は本書の規則で実行した結果に作り直した（unclear の hf の内訳も §5-3 の計上規則と一致） | §0-1・§8-1・§11-5 |
| 05-B-5 | フェーズ内ターン数の数え方が無い | 計上先の表を新設（数えた返答はそのときの `state.phase` に計上、確認質問は次のフェーズ、数えた脱線は `aside`）。遷移判定は計上後。やり直しで初期化 | §5-3 |
| 05-B-6 | 15ターン上限の強制規則が無い | 遷移判定の3番目に「`turns` ≥ 15 なら finalize→result」。聞き返しは `turns` < 13 のときだけ（review 中は行わない）。上限到達時は HARD の推測を昇格させない | §5-2・§5-3・§5-5 |
| 05-B-7 | 脱線後の再提示のモードが無い | `mode=aside`（`base_mode` に元のモード）を追加。再提示は M-repeat・TOPIC_CAP・MAX_ATTEMPTS に数えない。危険話題も同じ（注意文は毎回） | §5-6・§8-5 |
| 05-B-8 | review・確認質問中の抽出入力と失敗時の扱いが無い | 抽出ペイロードを提示の種類ごとに表で定義。review の1回目の失敗は数えずに再提示、2回目は「訂正なし・昇格なし」で巡回を消費。確認質問の失敗・null は保留を破棄（`confirm_unparsed`） | §5-5・§8-5・§8-6 |
| 05-B-9 | 結果後の発言と「年齢訂正で解除」の矛盾 | 結果後の表を新設：restart は初期化、blocked 中の年齢訂正で解除して再開、それ以外は `POST_RESULT_TEXT`（数えない） | §5-7 |
| 05-B-10 | 「中学」の判定範囲が不明 | 抽出の `age_band`（本人の年齢）はどこでも有効。発言の原文への `JHS_RE` は年齢の質問への回答のときだけ。low の `under_15` は finalize でブロック | §5-4・§10-1・§7-4 |
| 05-B-11 | 結果時点の low の HARD の扱い | low の HARD は絞り込みに使い、結果文に「推測：…」＋`GUESS_TAIL` で明示。M-silent-low を新設（明示しなければ不合格）。**提案との違い**：M-silent の「空」は値が無いものだけを指すとしたうえで、low には別指標を設けた | §7-6・§8-7・§4-1 |
| 05-B-12 | 完了定義が移植版では実行できない | `unit_checks_v4.py` も `BAITO_HARNESS` で差し替え可能にした。移植版が持つ名前を付録C に列挙し節17で検査。P0〜P4 の完了定義を節番号とコマンドで記載 | §11-4・§13-1・付録C |
| 05-M-1 | `notify` と `needs_notice` の食い違い | 正規値は `needs_notice`。§10-2 の表を正規値の意味で書き直した | §6-1・§10-2 |
| 05-M-2 | `segment` の形式の食い違い | main は `student（学生）`／`unknown（未判明）`、抽出は正規値 | §8-5 |
| 05-M-3 | 誰が `render_prompt` を呼ぶか | aica の LLM 層が `prompt_stage(view)` で段を決めて呼ぶ。result 段の変数を列挙 | §9-1 |
| 05-M-4 | TOPIC_CAP の「超えた／達した」、数え方 | 「達した」に統一。提示（ask・reask）ごとに数え、聞き返し・retry・脱線・確認は数えない | §8-1・§8-3 |
| 05-M-5 | 脱線と危険話題の回数枠、危険語つきの回答 | 合算で `MAX_ASIDES`。危険語に当たった発言は抽出しない（`danger_skip`） | §5-6 |
| 05-M-6 | 「早く結果」を review 前に受けた場合 | **提案との違い**：review 前に受けた場合も2巡目を出さない（早く終えたいという意思を優先）。基準値の early_exit は rv=1 | §9-4・§16-2 |
| 05-M-7 | pending・archived の復元時の規則、共通スロットの退避 | 復元は `apply_update` を同じ `from` で再実行。新しいセグメントでも有効なスロットは退避しない | §3-3 |
| 05-M-8 | 「こだわりなし」の定義 | pref 共通の特別値として定義（不足度は確信度どおり、複数値では単独値、絞り込みでは空と同じ、表示は「こだわりなし」） | §6-1・§6-3 |
| 05-M-9 | 数値の自由入力の割り当て | 正規化の表（22行）を本文に記載 | §6-3 |
| 05-M-10 | 目的の表示名と重なる値 | 全値の表示名と出どころの質問を表に記載。推薦に使わないため統合しない理由を記載 | §6-6 |
| 05-M-11 | 結果の注意文の集合と順序 | 順序を固定して記載。late_night_removed・minor_hours は再掲しない。**提案との違い**：danger は警告済みなら結果でも再掲する。区切りは改行1つ | §8-7・§10-5 |
| 05-M-12 | 抽出の「使えない出力」の基準 | JSON として読めない／dict でない場合だけ失敗。上限超えは切り詰めてログ。evidence が無ければ fact の high 判定に使わない | §8-4・§7-2 |
| 05-M-13 | 事後チェック後の補充、need=0 | 候補の次点から `fallback_reason` で補充（`result_refill`）。need=0 なら LLM を呼ばない | §8-7 |
| 05-M-14 | State とログのイベント一覧 | 付録D に記載 | 付録D |
| 05-M-15 | 内部例外の扱い | スナップショットに戻し、現在の提示を `counted=false` で返し、`internal_error` を記録 | §5-4 |
| 05-M-16 | 抽出プロンプトの必須指示 | 10項目を本文に記載 | §9-3 |
| 05-M-17 | P3 の合格率の式 | 3つの率の分子・分母を定義。ペルソナは qid をキーに応答 | §13-1・§11-2 |
| 05-M-18 | 影運用の方法、§4-2 の扱い | 影運用の手順（並走・件数・判定）を定義。§4-2 に「P4 の合否に使う」列を設けた | §13-1・§4-2 |
| 05-M-19 | 本番導入が確認事項を待つか | 本番切り替えは法務確認（§16-1）と §12-2 の確認の後。影運用は先に行ってよい。既定値は本書の記載 | §13-1 |
| 05-M-20 | `review_ack` の値 | **提案との違い**：`all`・`partial`・`none`・null の4値とした。HARD の昇格は `all` のときだけなので、`partial` と `none` を区別しても安全側は変わらない | §8-6 |
| 05-N-1 | 参照の誤り | 全節の参照を v4.1 の節番号で見直した | 全体 |
| 05-N-2 | 「同じスロットに複数の値→low」の優先、evidence の照合 | 最優先と明記。evidence は NFKC 後の部分一致 | §7-2 |
| 05-N-3 | カードの `tier:"pref"` | `hard`／`mid`／`low` に統一。`safety_note` の表示位置を記載 | §8-6 |
| 05-N-4 | 初期の derived_ng、除いた深夜の扱い | `new_state` で `DERIVED_NG_MINOR` を入れる。除いた値は18歳以上と確定しても戻さない | §6-5 |
| 05-N-5 | version の無い保存状態 | v3 とみなす | §8-8・§13-3 |
| 05-N-6 | 40字と120字の違い | 意図的であると明記 | §9-2 |
| 05-N-7 | 矛盾の3つ目の値 | 出てきた値をすべて記録する | §7-5 |
| 05-N-8 | 04-M1 の `n` と NG の重複判定が本文に無い | `phase_turns` を §5-3 に、NFKC 後の完全一致を §6-5 に記載 | §5-3・§6-5 |
| 05-N-9 | `blocked` の型、blocked のとき LLM を呼ばない | null／`"under_15"`。need=0 のため結果の LLM を呼ばない | §8-7 |
| 05-N-10 | 複数ボタンの区切り、区切りを含む選択肢、空入力 | 完全一致を先に判定してから `MULTI_SEP_RE` で分割。複数選択の選択肢に区切り文字が無いことを節19で検査。空入力は数えない（`empty_input`）。区切り文字は「、」「,」「，」「/」「／」 | §5-4・§5-3 |

---

## §16 確認事項と設計判断

### 16-1 ユーザーに確認したい事項（既定値つき）
いずれも本書の記載が既定値であり、確認が済むまで既定値で実装・検査する。確認の結果で変える場合は本書を先に直す（§0-1）。本番への切り替えは、1 と §12-2 の確認が済んでからにする（§13-1）。
1. **注意文の文面（法務の確認が必要）**：§10-5 の TEXT ブロックにある `NOTICE.*` 15種の文面。
2. **ターン予算の配分**：上限15ターンは維持したが、年齢の必須化・言い換え再質問・review の2巡目に使うため、シナリオは v3 の 4〜6 ターンから 3〜4 ターンに減った。
3. **年齢不明の扱い**：年齢が空・未回答（`declined`）なら未成年として扱い、18歳以上限定の仕事も推薦しない。
4. **解消しない矛盾（§12-2、暫定）**：「未定」とし、両方の候補を結果文に並べる。
5. **質問文の逐語要求**：生成文は質問文をそのまま含む必要があり、満たさなければ質問文だけに戻す。実 LLM での代替の発生率を P3 で測る。
6. **許可リスト**：推薦は職種カタログ（付録B）24件の中からだけ選ぶ。
7. **乱発された high の扱い**：LLM が他の質問中に HARD を言い当てても low にし、矛盾すれば「未定」に落とす（`spam_high` では HARD 3件が未定になる）。
8. **17歳の主婦・主夫**：主婦・主夫セグメントのまま未成年ルールを適用する。
9. **HARD の昇格**：HARD の推測は「これでOK」（`review_ack`=`all`）のときだけ確定する。
10. **時間帯の導出**：働ける時間から導出した時間帯は、元の回答と同じ確信度にする（low にすると主婦の会話でターンが増える）。
11. **学生の扶養**：スロットは作らず、結果の注意文だけで知らせる（02-#15 の一部見送り。理由は §15-3）。
12. **P3 の合格基準**：生成の検証通過率（再試行前）95%以上、代替 2%以下、抽出の JSON 成功率 98%以上（式は §13-1）。

### 16-2 v4.1 で決めた設計判断（05 の提案と違うもの、または 05 が判断を求めたもの）
変える場合は本書と単体チェックを同時に直す。
| # | 判断 | 理由 | 関係する 05 | 節 |
|---|---|---|---|---|
| 1 | 「早く結果」（`show_result`）は、review の前に受けた場合も2巡目を出さない | 早く終えたいという意思を優先する。HARD の推測は昇格させないので安全側は変わらない | M-6 | §9-4・§8-6 |
| 2 | 危険話題の注意文は検知のたびに毎回出し、脱線と回数枠（`MAX_ASIDES`）を共有する | 闇バイトの注意は省略しない。枠を共有して会話が伸び続けるのを防ぐ | B-7・M-5 | §5-6 |
| 3 | `DANGER_RE` の誤検知（例：「日給1万円くらいの仕事」）は受け入れる | 見逃しより誤検知のほうが害が小さい。誤検知でも状態は変わらず、注意文と再提示だけである | B-3 | §5-8・§10-4 |
| 4 | low の `under_15` は会話中にはブロックせず、`finalize` でブロックする | 他の質問中の推測で会話を止めない。結果は必ず止める | B-10 | §7-4・§10-1 |
| 5 | 確認質問への回答は「次のフェーズ」に計上する | 確認質問を出す時点で `phase` を進めているため。上限に達したフェーズへ数えない | B-5 | §3-2・§5-3 |
| 6 | 4回目以降の脱線は `phase_turns["aside"]` に計上する | どのフェーズの遷移条件にも影響させない | B-5 | §5-3 |
| 7 | `review_ack` は `all`・`partial`・`none`・null の4値 | 昇格は `all` のときだけ。区別はログと分析に使う | M-20 | §8-6 |
| 8 | 結果時点の low の HARD は絞り込みに使い、「推測：」として明示する | 空とみなすと推薦が広がりすぎる。明示しないことは M-silent-low で不合格にする | B-11 | §8-7・§4-1 |
| 9 | 結果では、警告済みなら危険話題の注意文を再掲する。深夜除外・未成年の時間の注意は再掲しない | 危険話題は結果を読む時点でも重要。後2者は会話中に伝え済み | M-11 | §8-7・§10-5 |

---

## 付録A 質問バンク（規範）
- **パス**：`/workspace/baito_harness/bank_v4.py`（本書と同じディレクトリの `bank_v4.py`）。公開名 `BANK`（リスト）。
- **役割**：本書の規範の一部である。49問の本文（質問文・選択肢・効果）は本書に転記せず、このファイルを正本とする。ファイルを変える場合は、本書の版を上げ、単体チェック（節1・節19）と §11-5 の基準値を作り直す。
- **並び順**：`BANK` のリストの順序が正式な順序であり、`pick_question` の同点判定に使う（§8-3）。
- **フェーズ別件数**：icebreak 2・hard_filter 9・scenario_game 25・reverse_pick 4・fill_gaps 9、合計49。
- **1問のスキーマ**：
| キー | 型 | 意味 |
|---|---|---|
| `id` | 文字列 | 質問 ID（一意。`q_segment`・`hf_age` などは本書から参照する） |
| `phase` | 文字列 | `ORDER` の review・result 以外の5フェーズのどれか |
| `segments` | 文字列のリスト | `all` または `SEGMENTS` の値。出題できるセグメント |
| `targets` | 文字列のリスト | この質問で埋めるスロット（`segment` を含みうる） |
| `text` | 文字列 | 質問文（1回目） |
| `alt_text` | 文字列 | 言い換え（`reask` のとき。無ければ `text` と同じ） |
| `choices` | 文字列のリスト | ボタンの表示名（`choice_effects` のキーの順） |
| `choice_effects` | 辞書 | 選択肢 → `{スロット: 正規値, "__ng__": [NG…], "__segment__": セグメント}`。ボタンのときハーネスが直接適用する（§5-4） |
| `choice_flags` | 辞書 | 選択肢 → `["late_night"]`・`["heavy"]` など。未成年モードでは該当する選択肢を表示しない（§10-1） |
| `multi` | 真偽 | 複数の選択肢を同時に押せるか（§5-4） |
- **制約**（単体チェック節1で検査）：`choice_effects` の値は §6-1 の正規値であること。scenario_game・reverse_pick・icebreak の質問は HARD スロットと fact スロットに書き込まないこと。複数選択の選択肢は `MULTI_SEP_RE` に当たらないこと（節19）。ハーネスが出す文と全質問の文・選択肢は `MONEY_RE`・`DANGER_RE` に当たらないこと（節19）。
- `q_segment` の選択肢の文言は本書 §10-5 の TEXT ブロック（`Q_SEGMENT_CHOICES`）にも記載し、一致を節18で検査する。

## 付録B 職種カタログ（規範）
- **パス**：`/workspace/baito_harness/catalog_v4.py`。公開名 `CATALOG`（24件のリスト）。
- **役割**：推薦の許可リストであり、本書の規範の一部である。結果に出してよいのはこの24件だけである（§8-7）。件数・属性を変える場合は本書の版を上げ、基準値を作り直す。
- **1件のスキーマ**：
| キー | 型・値域 | 意味 |
|---|---|---|
| `id` | 文字列 | 職種 ID（`job_id`） |
| `name` | 文字列 | 表示名 |
| `cf` | `high`／`mid`／`low`／`none` | 接客の度合い |
| `phys` | `seated`／`light`／`standing`／`heavy` | 体の負担 |
| `times` | `morning`・`daytime`・`evening`・`late_night` の集合 | 働ける時間帯 |
| `keys` | 文字列のリスト | NG 照合用の語 |
| `min_age` | 15 または 18 | 18 なら未成年モード（年齢が空・未回答を含む）では出さない |
| `remote` | 真偽 | 在宅可 |
| `noise` | `quiet`／`normal`／`lively` | 職場のにぎやかさ |
| `night_only` | 真偽 | 主に22〜5時の仕事 |
| `spot` | 真偽 | 単発で働ける |
- **NG 照合（`ng_hits`）**：NG の各文字列 `n` を NFKC 正規化した `n2` について、次のどれかに当たればその職種を除外する。
  1. `n2` が「深夜シフト」と完全一致し、`night_only` が真。
  2. `n2` が「重量物」を含み、`phys` が `heavy`。
  3. `keys` のどれか `k` について、`n2` が `k` を含むか、`k` が `n2` を含む。
  4. `n2` が `name` に含まれる。
- 除外条件と加点表は §8-7 に記載する。未成年モードでは `min_age`=18・`phys`=`heavy`・`night_only` の職種を出さない（§10-1・`job_safe`）。

## 付録C 移植版が持つべき名前（単体チェック節17）
移植版のモジュールは次の名前を同じ意味で公開する。引数と戻り値は参照実装 `harness_v4.py` と同じとする。`BAITO_HARNESS=<モジュール名>` で `unit_checks_v4.py` と `simulate.py` の両方を移植版に差し替えて実行できる（§11-4）。
- **関数・クラス（40）**：`State` `new_state` `start` `resume` `turn` `advance` `next_phase` `pick_question` `visible_choices` `match_button` `extract_payload` `view` `validate_extract` `validate_reply` `validate_result` `apply_update` `set_segment` `normalize_value` `confidence_for` `build_card` `render_card` `apply_review` `finalize` `build_result_payload` `eligible_jobs` `compose_result_text` `fallback_result` `post_check_result` `ng_hits` `job_safe` `update_block` `qmap` `minor_mode` `active_slots` `hard_slots` `gap_weight` `is_filled` `recompute_derived` `render_prompt` `prompt_stage` `slots_json`
- **定数（21）**：`VERSION` `SLOTS` `SLOT_PRIORITY` `SEGMENTS` `SEGMENT_JA` `NOTICE` `NOPREF` `DERIVED_NG_MINOR` `FACT_SLOTS` `ORDER` `HARD` `MID` `LOW` `PHASE_CAP` `DANGER_RE` `MONEY_RE` `JHS_RE` `NOPREF_RE` `DECLINE_RE` `PII_RE` `MULTI_SEP_RE`
- 規範の値（REGEX・CONST・TEXT ブロック）は本書の §5-8・§8-1・§10-5 にあり、移植版との一致も節18で検査する。

## 付録D 状態（State）とログのイベント
### D-1 State のフィールド
保存形式は `to_dict()` の JSON であり、参照実装と移植版で共有できる。`version` が無い保存状態は v3 とみなす（§8-8）。
| フィールド | 型 | 初期値 | 意味 |
|---|---|---|---|
| `version` | 文字列 | `"v4"` | 状態の版 |
| `phase` | 文字列 | `"icebreak"` | 現在のフェーズ（`ORDER` の値） |
| `segment` | 文字列 | `"unknown"` | セグメント |
| `slots` | 辞書 | `{}` | スロット → `{value, confidence, from}` |
| `user_ng` | リスト | `[]` | 利用者が言った NG |
| `derived_ng` | リスト | `DERIVED_NG_MINOR`（`new_state` が設定） | 年齢から導く NG（§6-5） |
| `pending` | 辞書 | `{}` | まだ有効でないスロットの値（§3-3） |
| `archived` | 辞書 | `{}` | セグメント → 退避したスロット |
| `conflicts` | 辞書 | `{}` | スロット → 矛盾した値のリスト |
| `unresolved` | 辞書 | `{}` | 結果の時点で解消しない矛盾（§12-2） |
| `notes` | 辞書 | `{}` | スロット → 正規化できなかった原文（`PII_RE` 適用後30字） |
| `asked` | リスト | `[]` | 提示した質問 ID（順） |
| `attempts` | 辞書 | `{}` | スロット → 直接質問の回数 |
| `topic_count` | 辞書 | `{}` | スロット → 話題として出した回数 |
| `turns` | 整数 | 0 | 数えたターン数 |
| `phase_turns` | 辞書 | `{}` | 計上先 → ターン数（§5-3） |
| `aside_count` | 整数 | 0 | 脱線・危険話題の回数 |
| `clarify_used` | 整数 | 0 | 聞き返しの回数 |
| `clarified` | リスト | `[]` | 聞き返し済みの質問 ID |
| `retried` | リスト | `[]` | 抽出失敗で再提示した質問 ID |
| `current` | 辞書または null | null | 現在の提示 `{qid, mode, choices, …}` |
| `pending_segment` | 文字列または null | null | 確認待ちのセグメント |
| `review_rounds` | 整数 | 0 | review の巡回数 |
| `last_card` | 辞書または null | null | 直近のカード |
| `review_log` | リスト | `[]` | review ごとの `{round, ack, touched, …}` |
| `flags` | リスト | `[]` | `school_banned`・`side_unknown`・`danger_warned` など |
| `notices_sent` | リスト | `[]` | 会話中に出した注意文のキー |
| `blocked` | 文字列または null | null | null または `"under_15"` |
| `restarts` | 整数 | 0 | やり直しの回数 |
| `result` | 辞書または null | null | 保存済みの結果 |
| `log` | リスト | `[]` | イベント `{t, kind, …}`（D-2） |

### D-2 ログのイベント（`log[].kind`）
| イベント | 記録する場面 |
|---|---|
| `extract_repaired`・`generate_repaired`・`result_repaired` | LLM 出力を修復して採用した（§8-4） |
| `extract_retry_ok`・`generate_retry_ok`・`result_retry_ok` | 再試行で検証を通った |
| `extract_fallback`・`generate_fallback`・`result_fallback` | 再試行後も失敗し代替に切り替えた |
| `ambiguous_extract` | 同じスロットに複数の値が返った（low にした） |
| `clarify_suppressed` | 聞き返しの条件を満たさず通常処理にした |
| `confirm_skipped` | 次が review・result のため確認質問を出さなかった |
| `confirm_unparsed` | 確認質問の返答からセグメントが取れなかった |
| `conflict_unresolved` | 結果の時点で矛盾が残った |
| `danger_skip` | 危険語に当たった発言を抽出しなかった（結果後は `where="post_result"`） |
| `discarded_unknown_slot` | 未定義のスロットを捨てた |
| `empty_input` | 空の発言 |
| `extract_gave_up` | 抽出が2回失敗し回答なしとして進んだ |
| `internal_error` | ハーネス内部の例外（状態を戻した。§5-4） |
| `invalid_segment` | 定義外のセグメント値を無視した |
| `jhs_fallback` | 年齢の質問への原文に `JHS_RE` が当たり `under_15` とした |
| `note_unnormalizable` | 正規化できない値をメモに残した |
| `post_result_message` | 結果後の発言（§5-7） |
| `rejected_by_rule` | 規則で値を却下した（`rule`＝`minor`・`low_over_high`・`nopref_on_fact`） |
| `restart` | やり直し |
| `result_postcheck_drop` | 事後チェックで推薦を落とした |
| `result_refill` | 落とした分を補充した |
| `review_reply_unparsed` | review の返答から抽出できなかった（`retry` は再提示したか） |
| `segment_change_pending` | セグメントの変更を保留した |
| `segment_switched` | セグメントを切り替えた |
| `unblocked` | 年齢の訂正でブロックを解除した |
