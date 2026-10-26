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
python -m evals.baito_sim           # アルバイト・パートのハーネスの回帰(モックの LLM。PR #115 で入る)
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

`evals.baito_sim` は、PRD の `simulate.py v3` の 8 ペルソナをモックの LLM で最後まで話させ、
PRD(`docs/prd/baito_harness/PRD_baito_agent.md`)の次の基準に照らします。

- §11.3 の受け入れチェックの 1〜7: 8/8 のペルソナが結果まで届く(`LOOP`、`CRASH` がない、`turns ≤ 15`)、review を
  1 回以上する、空の HARD と low の HARD が 0、hs16 に深夜の NG が付く、セグメントの質問を必ず出す、終わりに矛盾が
  残らない、`irrelevant`、`dropped`、`wasted` が 0
- §11.3 の 8: §4.1 のベースラインより悪くならない(ターン数、シナリオのターン数、空の MID、同じ質問の繰り返し)

§11.3 の 9(実 LLM につないだあとの transcript の確認)は実 API を使うので、CI では確かめません。
人が予算を決めて回します。

`evals.baito_sim` が main に入るまで(PR #115)は、CI はバイトのハーネスの回帰を回しません。
そのときはチェックの画面に警告(「バイトのハーネスの回帰を回していない」)が出て、ジョブは緑のままです。
緑でも、警告が出ていれば回帰は確かめていません。PR の説明にそう書きます。
`line_runtime/baito_chat.py` があるのに `evals.baito_sim` が無いときは、CI を落とします。

### CI が確かめないこと

- ブラウザのテスト(Playwright を入れていないので skip)
- 実 API を使う評価(`python -m evals.run`、§11.3 の 9 など)
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
