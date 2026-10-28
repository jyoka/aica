# 正社員のヒアリングの評価

正社員のチャットのヒアリングを、短気な本人役で測ります。基準は設計メモ [fulltime-hearing.md](../../docs/design/fulltime-hearing.md) の §6 です。
基準を変えるときは、先に設計メモを直します。

## 実行のコマンド

```sh
./.venv/bin/python -m evals.fulltime_hearing.run --form A --persona all --repeat 2 --limit-usd 2
```

| 引数 | 中身 | 既定 |
|---|---|---|
| `--form` | 比べる形。カンマ区切りで複数。表の差は、最初の形と比べる | 必須 |
| `--persona` | ペルソナの id(カンマ区切り)か `all` | `all` |
| `--repeat` | 形 × ペルソナごとの回数。§6.6 は 2 | 1 |
| `--ledger` | 費用台帳 | `evals/budget-fulltime-hearing.jsonl` |
| `--limit-usd` | 台帳の累計がこの額に達したら止める。Issue に書いた上限を渡す | 必須 |
| `--max-turns` | 1 会話の AI の返信の上限(費用の歯止め)。上限の返信が result でなければ、本人役と形にもう送らずに「ターン上限」で止める | 30 |
| `--same-samples` | 文が同じ意味かを判定する回数(1 以上) | 2 |

- 実 API を使います。本人役とジャッジはどの形でも、形 A はエージェントも、形 B〜D はハーネスのモデルも使います。キーはリポジトリ直下の `.env` から読みます。
- 1 会話の費用(task 248 の実績): 形 A は $0.06〜0.30(平均 $0.19)、形 B〜D は $0.05〜0.15(平均 約 $0.10)。設計メモ §7
- 台帳は 1 会話ごとに 1 行足します。会話を始める前に累計を見て、上限に達していたら止めます(終了コード 2)。
- 結果は `evals/results/fulltime-hearing-<日時>.json` と `.md` です。1 会話ごとに上書き保存するので、途中で止めても残ります。
  `evals/results/` は git に入りません。基準に残すものは `evals/baseline/` にコピーします。
- 計測の結果は、設計メモの §7 に 1 行ずつ書きます。
- いくつかの実行の結果を 1 つの報告にまとめるときは `python -m evals.fulltime_hearing.report <JSON>... [--exclude "2:D/p6-unknown#1"] --out x.md`(費用 0)。task 248 の結果と報告は `evals/baseline/fulltime-hearing/`

例:

```sh
# 本人役を確かめる(§6.6 の 1 を実 API の本人役とジャッジで。エージェントは呼ばない)
./.venv/bin/python -m evals.fulltime_hearing.run --form script-bad,script-good --persona p1-sales,p4-short --limit-usd 0.5
# A を測る(§6.6 の 2)
./.venv/bin/python -m evals.fulltime_hearing.run --form A --repeat 2 --limit-usd 2
# B〜D を比べる(§6.6 の 4。A は別に回した結果と比べる)
./.venv/bin/python -m evals.fulltime_hearing.run --form B,C,D --repeat 2 --limit-usd 10
```

## 形

| 形 | 中身 | 取れた事実の数え方 |
|---|---|---|
| `A` | 今の正社員の相談(`dip_mcp/playbooks/fulltime_chat.md`)。LINE のトークとして回し、LIFF の画面は使わない。本人役が読むのは LINE に送るメッセージ(`line_channel.to_messages`)の字 | 会話の記録と、先に送った履歴書から、ジャッジが事実を取り出す |
| `script-good` | 台本の良い AI。1 回に 1 問、80 字まで、聞き直さない、ボタンとパス、§3.3 の時間の予算 | 台本が本人の言葉のまま持つ |
| `script-bad` | 台本の悪い AI。長い、2 つ聞く、聞き直す、面接口調 | 同上 |
| `B`、`C`、`D`、`D-noband` | ハーネス(`dip_mcp/career/`)を、トークと同じモデルの呼び方(`line_runtime/career_chat.py`)でつないだもの(`forms.py` の `FormHarness`。5/5 のタスク)。1 ターンは `dip_mcp/career/session.py` の `turn` と同じく、モデルの出力が要るたびにターンの始めからやり直す。dip の保存とトークの AI は通らない。形 D は Flex の中の字(`line_runtime/career_ui.py`)を本人役が読む。`D-noband` は帯を切った D(`CAREER_UI_BAND=off`)。履歴書を先に送っている人は、モックの回帰と同じく履歴書の欄を事実表の値で先に埋める。「あとで」で閉じたら、本人が断った離脱。モックの回帰は `python -m evals.career_sim`(費用 0) | ハーネスの終わりの状態(`harness.captured`) |

形は `start()`、`send(text, pressed)`、`captured()`、`saved_texts()`、`wasted()` を持ちます。`captured()` の形は `judge.py` の
docstring にあります(`None` を返すと、会話の記録から取り出します)。形 A の完了と離脱は、次のように数えます。

- 完了: 要約を記録したか、求人を提案した(カードを見せた)。
- 離脱(AI が閉じた): `wait` を記録した。要約が付いていても完了に数えません。
- 離脱(本人が断った): 本人が「今はいい」と返し、その返信で会話が閉じた。答えた数によらず同じに数えます。「今はいい！」「今はいいです。」のように、空白や句読点、記号(全角も半角も)が付いていても同じです。
  playbook は、1 問でも答えていれば `consult` と要約、0 問なら `wait` を記録するので、そのままだと数え方がそろわないためです。

## 仕組み

| ファイル | 役割 |
|---|---|
| `personas.py` と `evals/personas/fulltime_hearing/` | ペルソナ 6 人と事実表(§6.3)。2/5 のモックの本人役も同じ事実表を使う |
| `actor.py` | 本人役。LLM はボタンか文を返すだけ。時間といらいらを数えて離脱を決めるのは `Clock`(コード) |
| `judge.py` | いらいらのラベル(聞き直し、2 つ聞く、面接口調)と誘導(形 B〜D は質問バンクの決まった候補を渡し、誘導に数えない。設計メモ §6.4)、形 A の事実の取り出し、正しく取れた事実の判定 |
| `conversation.py` | 1 つの会話を回し、指標と関門を出す |
| `report.py` | 形ごと、ペルソナごとの表。合格の線と、良くなったとみなす差(§6.4) |

本人役の決まり(§6.1):

- 推定時間は、読む 400 字/分、打つ 40 字/分、ボタン 1 回 2 秒。読む字は吹き出し、ボタンのラベル、Flex の中の字の合計
- 5 分を超えたら離脱。いらいらのできごとが 3 回たまったら「もういいです」で離脱。80 字を超えた吹き出しは、コードが 1 回に数える
- 3 分を超えたら、本人役に「疲れている」と伝える(ボタンか短い言葉 1 つ)。守ったかは記録だけ(疲れたのに 11 字以上打った回数)
- result の返信を読んで 5 分を超えても、result に着いたので完了に数える
- 加点の決まりは入れない

離脱の判定をコードに持たせたのは、実 API なしのテストでも離脱を確かめるためです(設計メモの §6.7)。

数の事実の判定(§6.4、`judge.code_verdict`):

- 取れた数か区切りの単位(`unit`)が、事実表の単位の頭(括弧の前)と同じときだけ、値か区切りで比べます。「名」は「人」、「万」は「万円」と同じに見ます
- 単位が無いか違うときは、本人の文の中で、事実表の単位がすぐ後ろに付いた数だけを見ます。120 社に「20人以上」、3 年に「3か月」は正しくなりません

## テスト

実 API なしで、本人役とジャッジを台本の LLM に替えて回します。

```sh
./.venv/bin/python -m pytest -q tests/test_fulltime_hearing_eval.py
```

悪い台本で 6 人とも離脱し、良い台本で 6 人とも完了すること(§6.6 の 1)を確かめます。
