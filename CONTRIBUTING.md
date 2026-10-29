# CONTRIBUTING

aica で作業する人への決まりです。エージェント向けの入口は [AGENTS.md](AGENTS.md) です。
AGENTS.md と食い違ったら、この文書が正です。業務の語は [CONTEXT.md](CONTEXT.md) にあります。

## 書き方

README と `docs/` は日本語です。

- 結論を先に書きます。前置きは書きません
- 1 文は短くします(目安は 40 字)
- 同じものには、毎回同じ語を使います。途中で言い換えません。語は [CONTEXT.md](CONTEXT.md) に合わせます
- 3 つ以上の並び、手順、比較は、箇条書きか表にします
- ハイフンは半角の `-` を使います。em ダッシュや en ダッシュは使いません
- コマンド、ファイル名、設定のキーは `` ` `` で囲みます
- 確かめていないことは、確かめていないと書きます
- 使い方が変わる変更は、同じ PR で README と関係する docs も直します。部品や流れが変わるなら HLD も直します

## README の構成

README は、初めて来た人が最初の画面で「何か」「なぜ使うか」「どう始めるか」をつかむためのページです。
詳しいことは docs に置き、README からリンクします。150 行前後、長くても 180 行に収めます。

| 順 | 見出し | 中身 |
|---|---|---|
| 1 | `# aica` | 太字の一言の説明と、何をするものか 2〜3 行 |
| 2 | `## しくみ` | Mermaid の図 1 枚と箇条書き 3 つ |
| 3 | `## 特長` | 1 行ずつ、7 つまで |
| 4 | `## はじめる` | 要るもの、コマンド 3 つ前後。詳しくは docs/usage.md |
| 5 | `## 使い方` | 最初の 1 分の流れ。詳しくは docs/usage.md |
| 6 | `## コマンド` | よく使うコマンドを 1 行ずつ。全部は docs/usage.md の「主なコマンド」 |
| 7 | `## ドキュメント` | docs への案内の表 |
| 8 | `## 開発` | テストのコマンドと AGENTS.md、この文書への案内 |

ライセンスのファイルは無いので、`## ライセンス` は置きません。
新しい見出しを足したくなったら、まず docs のどこかに置けないかを考えます。

## 置き場所

docs/ の分け方は変えません。新しい文書は、下の表の場所に置きます。

| 内容 | 場所 |
|---|---|
| 何ができるか、始め方 | [README.md](README.md) |
| 動かし方、LINE、LIFF、リッチメニュー、マイクの確認、アカウント連携、画面、コマンドの全部 | [docs/usage.md](docs/usage.md) |
| 設定、環境変数、保存する期間、ターン数の上限 | [docs/configuration.md](docs/configuration.md) |
| Render での公開の手順、環境変数、置くデータ、落ちたときの手順(runbook) | [docs/deploy/](docs/deploy/) |
| 何を作るか(要件と予算) | [docs/prd/](docs/prd/) |
| 部品とデータの流れ(HLD)、モジュールと関数(LLD)、設計の見直し | [docs/architecture/](docs/architecture/) |
| 運用の決定(段階と 7 つの関門。task-hub の `task check` が読むので、この名前で置く) | [docs/architecture.md](docs/architecture.md)(Architecture Brief) |
| 1 つの決定とその理由 | [docs/adr/](docs/adr/) |
| 機能ごとの設計メモ | [docs/design/](docs/design/) |
| 利用規約とプライバシーポリシー(画面に出す原文) | [docs/legal/](docs/legal/) |
| 調べたこと | [docs/research/](docs/research/) |
| 文書に入れる写真と図 | `docs/images/` |
| 業務の語の定義 | [CONTEXT.md](CONTEXT.md) |
| 評価の結果の集計と比較 | [evalkit/README.md](evalkit/README.md) |

## テスト

`python` は `./.venv/bin/python` のことです。どれも実 API を呼びません。

```bash
# 準備(リポジトリ直下。.venv/ は git に入らない)
python3.12 -m venv .venv && ./.venv/bin/pip install -r requirements.txt pytest ruff

python -m pytest -q                 # テスト。直したところに関係するファイルだけなら python -m pytest -q tests/test_xxx.py
./.venv/bin/ruff check .            # lint(ruff.toml)
python -m evals.baito_sim           # アルバイト・パートのハーネスの回帰(モックの LLM)
```

- `.env` があると、LiveKit などの設定を読んで、テストの一部が `AICA_INTERNAL_KEY` が無いと落ちます。
  pytest を回すときは `.env` を置かないでください(CI にも置きません)
- ブラウザのテスト(Playwright)は、`playwright` が入っていないと skip されます。画面を変えたときは
  `./.venv/bin/pip install playwright==1.63.0` を入れて手元で回します
- 全部の pytest は数分かかります。作業中は関係するファイルだけを回し、最後に 1 回だけ全部を回します

## 回帰の合格基準

回帰が通ったかどうかは、自分の worktree での実行ではなく、PR の CI(`.github/workflows/ci.yml`)の結果で判断します。
CI は API キーを持たない環境で、次の 3 つを回します。すべて通ったときだけ合格です。

| 確かめること | コマンド | 合格 |
|---|---|---|
| テスト | `python -m pytest -q` | 失敗が 0(ブラウザのテストは skip) |
| lint | `ruff check .` | 指摘が 0 |
| バイトのハーネスの回帰 | `python -m evals.baito_sim` | 終了コード 0。最後に「すべて合格」と出る |

`evals.baito_sim` は、PRD v4.1(`docs/prd/baito_harness/PRD_baito_agent_v4.md`)の参照資料を一時ディレクトリに写し、
移植版(`BAITO_HARNESS=aica_port`。本体は `dip_mcp/baito/harness.py`)で次の 2 つを回します。写しの中で回すので、
基準値のファイル(`results_v4.json`、`transcripts/v4_*.txt`)は上書きしません。

- 単体チェック `unit_checks_v4.py`: 131/131 が合格する
- シミュレーター `simulate.py v4`(20 ペルソナ、モックの LLM): §13-1 P4 の 1 の条件に照らす
  - §4-1 の合否指標(`v4_agg` の `crash`、`loop`、`not_reached`、`minor_violation`、`result_violation`、
    `silent_empty_hard`、`dropped`、`irrelevant`、`wasted`、`repeated` など)がすべて 0、`max_turns` が 15 以下
  - §4-2 の「はい」の 4 項目(Q-turns、Q-scenario、Q-mid、Q-review)が §11-5 の基準値(`results_v4.json`)の範囲内

CI は `--log evidence/baito_sim` を付けて回し、単体チェックとシミュレーターの出力、移植版の結果、会話ログの写し、合否を
証跡に残します。pytest の `tests/test_baito_harness.py` も同じ検査を回します。

会話の層の版 v5(`dip_mcp/baito/v5.py`)は、`python -m evals.baito_sim --version v5` で 20 ペルソナを同じ基準値に照らします
(`BAITO_HARNESS=aica_port_v5`。単体チェックは v4 だけ)。pytest の `tests/test_baito_v5.py` が回し、PRD の結果の検査
(未成年の NG、深夜、中学生以下、金額、カードに無い職種)もそこで確かめます。

§13-1 P3(実 LLM での測定、`python -m evals.baito_sim --real`)は実 API を使うので、CI では確かめません。
人が予算を決めて回します。

`line_runtime/baito_chat.py` があるのに `evals.baito_sim` が無いときは、CI を落とします。

### CI が確かめないこと

- ブラウザのテスト(Playwright を入れていないので skip)
- 実 API を使う評価(`python -m evals.run`、`python -m evals.baito_sim --real` など)
- デプロイと LINE の実機での動き

PR の説明でこれらに触れるときは、手元で回したのか、回していないのかを書き分けます。

## task-hub のマージキューと CI のトリガーを有効にする

CI が main で通るようになってから、task-hub を動かしている Mac で人が設定します(エージェントは変えません)。

1. GitHub の Actions で、main の `ci` のワークフローが緑になっていることを確かめます
   (`gh run list -R jyoka/aica --workflow ci --branch main --limit 1`)
2. `~/.config/task-hub/config.ini` に次を足します。セクションがすでにあれば、その中に行を足します。
   `ci =` の行がすでにあれば、`, jyoka/aica` を後ろに足します

   ```ini
   [triggers]
   ci = jyoka/aica       ; main の CI が落ちたら「Fix failing CI on jyoka/aica」のカードを Backlog に作る

   [merge]
   queue = on            ; 承認した PR を 1 つずつ main に追いつかせ、CI が通ったらマージする
   checks = all          ; PR のチェック(ci の test)をすべて待つ(既定)
   ci_minutes = 60       ; CI を待つ長さ(分)。pytest は数分で終わる
   ```

3. `task watch` を動かし直します(設定はプロセスの起動のときに 1 回だけ読みます)
4. In review の aica のカードを `task merge <番号>` でキューに入れ、`task log <番号>` に `ci-wait` と `merged` が
   出ることを確かめます

詳しい動きは task-hub の `docs/operations.md`(マージキュー)と `docs/triggers.md` にあります。
