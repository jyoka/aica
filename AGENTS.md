# aica のエージェント向けの決まり

コーディングエージェント(task-hub のタスク、Claude Code、Codex など)がこのリポジトリで作業するときの決まりです。
業務の語は [CONTEXT.md](CONTEXT.md)、しくみとコマンドの全体は [README.md](README.md) にあります。

## テストの実行コマンド

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

§13-1 P3(実 LLM での測定、`python -m evals.baito_sim --real`)は実 API を使うので、CI では確かめません。
人が予算を決めて回します。

`line_runtime/baito_chat.py` があるのに `evals.baito_sim` が無いときは、CI を落とします。

### CI が確かめないこと

- ブラウザのテスト(Playwright を入れていないので skip)
- 実 API を使う評価(`python -m evals.run`、`python -m evals.baito_sim --real` など)
- デプロイと LINE の実機での動き

PR の説明でこれらに触れるときは、手元で回したのか、回していないのかを書き分けます。

## 禁止事項

- **実 API を呼ばない。** Anthropic、OpenAI、LiveKit、Deepgram、Cartesia、LINE の Messaging API を呼ぶコマンドは、
  人が Goal で明示して予算を決めたときだけ使います。README の「主なコマンド」で「実 API」の列が「使う」のもの
  (`evals.run`、`resume_intake.cli`、`scripts.gen_jobs`、`scripts.gen_job_vectors`、`scripts.deploy_smoke`、
  `scripts.line_rich_menu`(`--dry-run` を除く)、`evals.search_eval record` と `compare` など)が当たります。
  `.env` の鍵を読ませて Web プロセスや音声ワーカーを起動するのも同じです
- **デプロイしない。** Render のサービスの作成や手動デプロイ、`render.yaml` の自動デプロイ(`autoDeployTrigger`)を
  有効にする変更、LINE のチャネルやリッチメニューの登録の変更はしません
- **CI に秘密情報を渡さない。** ワークフローに `secrets.*` や API キーを足しません。CI は鍵なしで通ることを確かめる場所です
- `.env`、API キー、`data/` の中身をコミットしません

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
