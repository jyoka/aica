# 06 実装着手可否チェック（Implementation Readiness Check）— PRD v4.1

- レビュー役：Goldfish（文脈なし）
- 対象：`/workspace/baito_harness/PRD_baito_agent_v4.md`（v4.1）
- 読んだもの：PRD 本体（全文）、付録A `bank_v4.py`、付録B `catalog_v4.py`、参照実装 `harness_v4.py`（全文）、`prompt_main/extract/review/result_v4_ja.txt`
- 読んでいないもの：`/workspace/egm_experiment/` 配下の既存レビュー・transcripts・results*.json、`PRD_baito_agent_v4_0.md`、v3 系、`/tmp`、`unit_checks_v4.py`・`sim_v4.py`（許可リストに無いため）
- ファイルは編集していない（本ファイルの新規作成のみ）。`simulate.py` は `results_v4.json` を上書きするため実行していない。読み取りだけの Python 実行で正規化とカード長を確かめた。
- 重大度：**Blocker**＝実装者が独自に決めると挙動か合否が変わる／**Major**＝迷いや手戻りが出るが、規則どおり読めば一応実装できる／**Nit**＝表現・整合の軽微な問題

## 件数
| 重大度 | 件数 |
|---|---|
| Blocker | 3 |
| Major | 16 |
| Nit | 17 |

---

## Blocker

### B-1 §8-3（L607）DIRECT の定義が矛盾している
- **問題**：「質問のフェーズが DIRECT（icebreak・hard_filter）なら…`attempts` を +1」とある。一方、§5-1・§7-2（`choice_direct` は「hard_filter・fill_gaps」）・参照実装 `DIRECT={"hard_filter","fill_gaps"}` では fill_gaps が DIRECT で、icebreak は DIRECT ではない。どちらを採るかで、`attempts`、`remaining_gaps`、reask の候補、fill_gaps の終わり方、ターン数（§11-5 の基準値）が変わる。§0 の「参照実装は本書と一致」の前提も崩れる。
- **追記・修正案**：L607 を「質問のフェーズが DIRECT（hard_filter・fill_gaps。§5-1）なら、不足の対象スロットの `attempts` を +1 する。reask も提示なので +1 する。icebreak の質問では `attempts` を増やさない。」に直す。

### B-2 §7-2「例外（必ず low）」と §8-6 手順2「review は high」の矛盾
- **問題**：§7-2 では、1回の抽出で同じ単一スロットに異なる値が複数あれば「必ず low（`ambiguous_extract`）」で、review 行も例外ではない。§8-6 手順2 では「自由入力の `slot_updates` は確信度 high・`from=review`」としている。参照実装 `_apply_ext` の曖昧検査は review 以外だけで、review では最後の値を high で入れる。low にすれば矛盾が生じ、2巡目が出てターン数が変わる。
- **追記・修正案**：§7-2 の例外行に「（review では適用しない。review では最後の値を high で統合し、ログ `ambiguous_extract` だけ残す）」を加え、§8-6 手順2 にも同じ文を置く。逆に例外を review にも適用するなら、参照実装と §11-5 の基準値を直す。

### B-3 §13-1 P3 の完了の定義を実行する手段が無い
- **問題**：§11-4 は「完了の定義はすべて §11-4 のコマンドで実行できる」としている。しかし実 LLM で sim を回すコマンド・環境変数・LLM 層の差し替え口（`llm.extract`/`llm.generate` を実装したモジュールの指定方法）が書かれていない。mock 前提のペルソナ（`malformed`・`spam_high` など、LLM の誤りを模したもの）を実 LLM でどう扱うか（除外・置換・そのまま）も無い。M-drop の「正しい更新」の判定基準（実 LLM では正解が無い）も無い。§16-1 の12（95%・2%・98%）は既定値があるが、何を母数にどう回すかは実装者次第で、合否が変わる。
- **追記・修正案**：「P3 は `BAITO_LLM=<module>`（`extract(payload)`・`generate(view)` を持つモジュール）を指定して `python3 sim_v4.py --real --out results_p3.json` で実行する。対象は §11-5 の20ペルソナのうち、LLM の誤りを模す `malformed`・`spam_high` を除く18ペルソナとし、ユーザー発言はペルソナの台本をそのまま使う。M-drop はペルソナ台本に付いた期待更新（`expected_updates`）と照合する。率の母数は段ごとの LLM 呼び出し回数（再試行を除く）とする。」のように、コマンド・対象・判定の正解・母数・出力先を明記する。

---

## Major

### M-1 §10-2（L922・L926）値名と表示名が §6-1 と違う
- **問題**：`notify` と「届け出・許可が必要」が残っているが、§6-1 の正規値は `needs_notice`（表示名「届け出すればOK」／「届け出が必要」）である。本文の §15 は 05-M-1 で直したと書いている。
- **追記・修正案**：L922 を「学校：`needs_notice`（届け出すればOK）」、L926 を「副業：`needs_notice`（届け出が必要）。学校の `needs_notice` と同じ扱い」に直す。

### M-2 §3-2・§5-2・§8-6 review でのセグメント訂正が必ず捨てられる
- **問題**：既知→別値の review 訂正は「保留→確認質問」だが、review 中の次のフェーズは review か result しかないため、必ず `confirm_skipped` で消える（参照実装もそう動く）。§3-2 の「カードの【あなた】行で訂正できる」は、この規則のもとでは成り立たない。実装者は、即時切り替えにするか、意図どおり捨てるかで迷う。
- **追記・修正案**：どちらかに決めて書く。
  - 案A：「review での明示的な訂正（`review_explicit`）は、既知の別値でも即時に切り替える（ログ `segment_switched_review`）」
  - 案B：「review での訂正は保留せず捨て、ログ `segment_change_ignored_in_review` を残す。【あなた】行での訂正はできない」とし、L109 の括弧書きを削る。

### M-3 §8-6 手順3 昇格の対象「カードに表示した項目」
- **問題**：2巡目のカードは focus 項目だけを表示するのに、参照実装は `card["hard"]+card["prefs"]` の全項目（非表示を含む）を昇格させる。PRD どおりに作ると挙動が変わる（確信度・`from`・結果文の「推測」表示）。
- **追記・修正案**：「昇格の対象は、そのラウンドでカードに表示した項目（2巡目は focus の項目だけ）とする」とし、参照実装を直す。または「表示の有無にかかわらず、カード対象の全項目」と書き換える。

### M-4 §5-4 の5・§10-1 の2 JHS_RE 補完の適用範囲
- **問題**：「提示中の質問の `targets` に `age_band` があり、確認質問でないとき」だけでは次が決まらない。
  - (a) review（抽出 payload の targets に age_band が入る）が対象か
  - (b) 抽出の再試行の後か
  - (c) 抽出が2回とも失敗した（ext=None）ときにも補完するか
  
  参照実装は ext が None のとき補完しない。(c) は安全に関わる（中学生が推薦に進みうる）。
- **追記・修正案**：「補完は review・確認質問では行わない。抽出が最終的に失敗した場合（retry・代替を含む）でも、発言に JHS_RE が当たれば `age_band=under_15`（`from=free:<質問ID>`、確信度は §7-2）を適用する」と書く。そうしない場合はその旨を書く。

### M-5 §7-5・§5-4 1回の抽出に含まれる複数更新の適用順
- **問題**：例「20歳で深夜もOK」で `time_slots:[late_night]` が `age_band:18_plus` より先に来ると、年齢が空のため未成年扱いとなり深夜が除かれ、後から年齢が入っても戻らない。順序で結果が変わるのに、適用順の規定が無い（参照実装は LLM 出力の順）。
- **追記・修正案**：「1回の抽出の `slot_updates` は、`age_band` を最初に適用し、残りは付録A-2 の SLOTS の定義順に適用する」と書く（参照実装も合わせる）。

### M-6 §5-7 結果後の年齢訂正（unblock）の境界
- **問題**：
  - (a) `turns` が既に15のときに訂正ターンを数えると16になり、§4-1 の M-over に違反する。
  - (b) 解除後に `review_rounds`・`show_result` フラグ・`last_card`・`retried`・`asked`（`hf_age`）をリセットするかが書かれていない（参照実装はどれもリセットせず、`phase=hard_filter` に戻すだけ）。
- **追記・修正案**：「解除時は `review_rounds`・`show_result`・`last_card` を初期化し、`asked` は維持する。`turns` が `MAX_TURNS` に達していれば、ターンを数えずに直ちに finalize→result をやり直す（ログ `unblock_at_cap`）」のように決める。

### M-7 §9-1（L856）generate に渡す「ユーザーの直近の発言」の経路が無い
- **問題**：生成段は直近の発言を user メッセージとして渡すとあるが、`llm.generate(view)` の引数は view だけで、§8-5 の view にも `user_text` が無い。§8-2 のインターフェースのままでは実装できない。
- **追記・修正案**：§8-5 の view に `user_text`（直近の発言。NFKC 前の原文、無ければ空文字）を加えるか、`llm.generate(view, user_text)` に変えると書き、参照実装も合わせる。

### M-8 付録C・§0-1 シグネチャを本文で決めていない
- **問題**：
  - 「関数・クラス（40）」とあるが、実際には41個並んでいる。
  - 「引数と戻り値は参照実装 harness_v4.py と同じとする」と丸投げしており、「付録以外を読まなくても実装できる」（§0-1）と矛盾する。例：`validate_extract`→(clean, probs)、`confidence_for` の引数、`update_block(final)`、`apply_review(ext, button_ok, promote)`、`pick_question`→(q, mode)。
  - `SLOTS`・`SLOT_PRIORITY` のデータ構造も決まっていない。
- **追記・修正案**：付録C を「名前・引数・戻り値・副作用」の表に直し、件数を41に直す。SLOTS の型（例：`{key: {"kind": "hard|mid|fact", "multi": bool, "segments": [...], "values": {...}}}`）を付録A-2 に書く。

### M-9 §13-1 P0 の完了条件と段階的な実装
- **問題**：P0 で節17（41名がすべて存在するか）の合格を求めているが、P1/P2 の関数を書く前に満たすには stub が要る。stub でよいのか、節単位の実行方法（`unit_checks --section N` など）も書かれていない。
- **追記・修正案**：「P0 では未実装の関数を `raise NotImplementedError` の stub で置いてよい。節17は名前の存在だけを見る。各段階は `python3 unit_checks_v4.py --sections 1-5` のように節を指定して実行する」と書く。

### M-10 §11-4 移植版を回したときの出力先とモジュールの置き場所
- **問題**：`BAITO_HARNESS=aica_port python3 simulate.py v4` で、基準値の `results_v4.json` が上書きされうる。比較の手順・出力ファイル名・`aica_port` の置き場所（PYTHONPATH）も書かれていない。
- **追記・修正案**：「移植版は `/workspace/baito_harness/aica_port.py` に置く。`BAITO_HARNESS=aica_port` のときの出力は `results_v4_aica_port.json` とし、基準値ファイルは上書きしない。比較は `compare_results.py results_v4.json results_v4_aica_port.json`」のように書く。

### M-11 §13-1 P4 shadow run の判定方法
- **問題**：
  - (a) 実ユーザーには真の年齢が無いので、M-minor（「未成年なのに未成年モードにならない」）が判定できない。
  - (b) v3 の会話が終わった時点で v4 が result に達していない場合の扱いが無い。
  - (c) 「v3 の会話を再生しない」としながら、v3 の質問への回答を v4 の `turn()` に入れる手順になっていて、v4 のボタン文言とも一致しない（すべて自由入力になる）。
- **追記・修正案**：「M-minor は、ログの発言に JHS_RE・年齢表現があるケースだけを人手でラベル付けして判定する。v3 の会話が終わったら v4 は `show_result` を入れて結果まで進める。v3 の発言は自由入力として扱い、ボタン一致は見ない。shadow で見るのは §4-1 の M-over・M-banned・M-silent-low と例外の有無だけ」のように範囲を決める。

### M-12 §5-5・§5-4 review での聞き返し（clarify）を本文で除外していない
- **問題**：review で clarify をしないことは §15 の 05-B6 にしか書かれていない。本文の §5-5 だけを読むと、review でも聞き返しをするように読める。
- **追記・修正案**：§5-5 に「review・確認質問・result 後では聞き返しをしない（`intent=clarify` は無視する）」と書く。

### M-13 §8-8・付録D 状態の JSON のキーの書式
- **問題**：参照実装と移植版で状態を共有する（`to_dict`/`from_dict`）としているが、`retried` の要素（"review1" など）、`current` の書式、`asked` の順序保持、`hourly_wage_min` の型（int への戻し）などが決まっていない。
- **追記・修正案**：付録D に各キーの型と例を載せる。例：「`retried`：文字列のリスト。質問は質問 ID、review は `review<n>`」「`hourly_wage_min`：int」。

### M-14 §7-5 の10・§6-3 複数スロットで「こだわりなし」とリストを和集合にする場合
- **問題**：`time_slots` などの複数スロットで、既存がリスト・新しい値が `こだわりなし`（文字列）のとき、またその逆のときの和集合が決まっていない。
- **追記・修正案**：「複数スロットで `こだわりなし` が来たら、既存値を置き換える。`こだわりなし` の上に具体値が来たら、具体値で置き換える（和集合にしない）」と書く。

### M-15 §10-5 `NOTICE.danger` の文面が DANGER_RE に当たる
- **問題**：注意文に「SNSで高額」「受け取るだけ」が含まれ、DANGER_RE に当たる。「ハーネスの固定文は DANGER_RE に当たらない」という自己検査の方針と矛盾する（返答の検証で代替に落ちる原因になる）。
- **追記・修正案**：「固定文の検査から `NOTICE.danger` を除く」と明記するか、文面を「SNSの『すぐ稼げる』系の募集」などの当たらない表現に変える（§16-1 の1：法務の確認の対象）。

### M-16 §6-3 の1 複数値の分割正規表現で表示名が壊れる
- **問題**：`[、,，/／・と&\s]+` で分割するため、表示名「推し活・趣味」「生活費・家計の足し」「経験・スキル」「人とのつながり」が文字列で来ると分割されてしまい、正規化できない（実測：4つとも None。リストで来ればマッチする）。§6-3 の3「表示名で一致」と食い違い、M-drop の取りこぼしになる。
- **追記・修正案**：「分割の前に、文字列全体を表示名・正規値と完全一致で照合し、一致すればそれを採る。分割はその後に行う」と書き、参照実装も合わせる（§5-4 の4のボタン判定と同じ考え方）。

---

## Nit

| ID | 節 | 問題 | 修正案 |
|---|---|---|---|
| N-1 | §5-4 Stage3 表（L239） | 「抽出失敗1回目以外の決定的な再提示…では呼ばない」が、review での1回目の失敗（LLM を呼ばずにカードを再提示）と読み合わない | 「review の再提示は常に LLM を呼ばない」と例外を書く |
| N-2 | §9-1 | review の変数に `card_json` が無いが、prompt_review が `{{card_json}}` を使う（§8-5 の view にはある） | review 行を「上記＋`card_text`・`card_json`・`closing`」に直す |
| N-3 | §8-4・§8-6 | 返答の上限600字は固定だが、review のカード＋closing は最大で約751字になる（実測。主婦の最大ケースでカード673字）。必ず代替に落ちる | review だけ上限を「カード長＋300字」にするか、代替が正しい挙動だと明記する |
| N-4 | §8-4 | ユーザー由来の NG 文字列や notes がカードに入って MONEY_RE に当たると、代替に落ちる | 「カードに含むユーザー由来の文字列は MONEY_RE の検査対象外」と書く |
| N-5 | §8-3 の6 | reverse_pick のスコアを1.0に固定していることと、M-waste（無駄な質問）の関係が書かれていない | 「reverse_pick は M-waste の対象外」と書く |
| N-6 | §8-7 | 「remote が yes かつ remote が真」の前者がスロット、後者が職種の属性であることが読み取りにくい | 「ユーザーの `remote` が yes、かつ職種の `remote` が True」と書く |
| N-7 | §8-7 | `result_json` に `guessed_hard` が無い（推測の HARD を明示するには結果側で要る） | 含めるか、無くてよい理由を書く |
| N-8 | §5-2 の8 | 「turns ≥ 10」が定数になっていない。HF_MAX・SC_MAX が PHASE_CAP と重複している | `SCENARIO_EXIT_TURN=10` を定数一覧に入れ、重複を削る |
| N-9 | §15 | 05-M-8 の「こだわりなし」の意味づけが §15 にしか無い | §6-3 に本文として書く |
| N-10 | §6-3 の2 | 整数と文字列の比較（"1200" と 1200）の正規化の順が読み取りにくい | 「数字だけの文字列は int にしてから比較する」と書く |
| N-11 | §8-4 | 「slot_updates 内の segment → トップレベルが無ければ」の、トップレベルが null のときの扱い | 「トップレベルが null か欠けていれば」と書く |
| N-12 | §3-2 | confirm_unparsed の「または何も残さない」が、いつ何も残さないのか曖昧 | 「抽出が失敗したら `confirm_unparsed`、segment が null なら何も残さない」のように場合を分ける |
| N-13 | §5-6・§4-1 | 脱線ターンでは抽出の更新を捨てるが、これが M-drop の取りこぼしに数えるのか書かれていない | 「脱線ターンの更新は M-drop の母数に入れない」と書く |
| N-14 | §5-7・§13-1 P3 | restart でログを消すため、P3 の回数の集計がずれる | 「restart 前のログは `log_archive` に退避し、集計に含める」と書く |
| N-15 | §9-1・prompt_extract | `allowed_slots` はキーだけ渡すのに、プロンプトは「キーか表示名」と言っている | どちらかに揃える |
| N-16 | §15-6 | 05-B6 の「上限到達時は HARD を昇格させない」が本文に無い | §8-6 の3に書く |
| N-17 | §8-7・§12-2 | 矛盾から unknown_hard になったスロットについて、「未定」の行が重複して出る | 「同じスロットの未定は1行にまとめる」と書く |

---

## 整合を確認できたもの
- §11-5：20ペルソナのターン合計は、PRD の表と参照実装の挙動（読み取りで追跡）で一致する。Q-mid の基準値9、Q-scenario ≥ 3 も成り立つ。
- 付録A：49問、付録B：24件（`night_only=True` の職種は無い）。PRD の件数と一致する。
- NOTICE は15種、定数一覧は21個で、本文の件数と一致する。
- ORDER・PHASE_CAP・MAX_TURNS 15・REVIEW_RESERVE 2・MAX_REVIEW_ROUNDS 2・MAX_ATTEMPTS 2・TOPIC_CAP 2・MAX_CLARIFY 2・MAX_ASIDES 3・LLM_RETRIES 1・SEGMENT_SCORE 1.5 は、本文と参照実装で一致する。
- §5-2 の遷移表、§5-3 の計上規則（`sum(phase_turns)==turns`）、§7-5 の統合規則11行は、参照実装と一致する（B-1・B-2・M-3 を除く）。
- §11-4 の BAITO_HARNESS による差し替え自体は動く（モジュール名で import して切り替わる）。ただし出力先と置き場所が未定義（M-10）。

---

## 判定

**NO-GO** — DIRECT の定義（B-1）と review での曖昧抽出の確信度（B-2）が本文の中で矛盾しており、P3 の完了の定義を実行する手段（B-3）も無い。そのため、実装者がどちらを採るかで、挙動と合否が変わってしまう。
