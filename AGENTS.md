# AGENTS.md

コーディングエージェント(task-hub のタスク、Claude Code、Codex など)が aica で作業するときの入口です。
人向けの決まりは [CONTRIBUTING.md](CONTRIBUTING.md) にあります。食い違ったら CONTRIBUTING.md が正です。

## まず読むもの

| 知りたいこと | 読むもの |
|---|---|
| 業務の語 | [CONTEXT.md](CONTEXT.md) |
| 何を作るか | [docs/prd/](docs/prd/) |
| 全体の部品とデータの流れ | [docs/architecture/hld.md](docs/architecture/hld.md) |
| 運用の決定(段階、7 つの関門)。サービス、データストア、秘密情報、プラン、常駐プロセスを変えるとき | [docs/architecture.md](docs/architecture.md) |
| なぜこの作りなのか | [docs/adr/](docs/adr/)、[docs/design/](docs/design/) |
| 動かし方とコマンドの全部、設定 | [docs/usage.md](docs/usage.md)、[docs/configuration.md](docs/configuration.md) |

## 書き方

- コード、コメント、README、docs は日本語。周りのコードに合わせる。書き方は [CONTRIBUTING.md の「書き方」](CONTRIBUTING.md#書き方)
- README の構成は決まっている([CONTRIBUTING.md の「README の構成」](CONTRIBUTING.md#readme-の構成))。詳しいことは docs に置いてリンクする
- 使い方が変わる変更は、同じ PR で README と関係する docs も直す。部品や流れが変わるなら HLD も直す

## 確かめ方

`python` は `./.venv/bin/python` のことです。どれも実 API を呼びません。

```bash
python3.12 -m venv .venv && ./.venv/bin/pip install -r requirements.txt pytest ruff   # 準備
python -m pytest -q                 # テスト。作業中は python -m pytest -q tests/test_xxx.py
./.venv/bin/ruff check .            # lint(ruff.toml)
python -m evals.baito_sim           # アルバイト・パートのハーネスの回帰(モックの LLM)
```

- pytest を回すときは `.env` を置かない(あると一部のテストが落ちる)
- 全部の pytest は数分かかる。作業中は関係するファイルだけを回す
- 合否は PR の CI で決める。回帰の合格基準と CI が確かめないことは [CONTRIBUTING.md の「回帰の合格基準」](CONTRIBUTING.md#回帰の合格基準)

## 禁止事項

- **実 API を呼ばない。** Anthropic、OpenAI、LiveKit、Deepgram、Cartesia、LINE の Messaging API を呼ぶコマンドは、
  人が Goal で明示して予算を決めたときだけ使います。[docs/usage.md の「主なコマンド」](docs/usage.md#主なコマンド)で「実 API」の列が「使う」のもの
  (`evals.run`、`resume_intake.cli`、`scripts.gen_jobs`、`scripts.gen_job_vectors`、`scripts.deploy_smoke`、
  `scripts.line_rich_menu`(`--dry-run` を除く)、`evals.search_eval record` と `compare`、`evals.scenes --real` など)が当たります。
  `.env` の鍵を読ませて Web プロセスや音声ワーカーを起動するのも同じです
- **デプロイしない。** Render のサービスの作成や手動デプロイ、`render.yaml` の自動デプロイ(`autoDeployTrigger`)を
  有効にする変更、LINE のチャネルやリッチメニューの登録の変更はしません
- **CI に秘密情報を渡さない。** ワークフローに `secrets.*` や API キーを足しません。CI は鍵なしで通ることを確かめる場所です
- `.env`、API キー、`data/` の中身をコミットしません
- task-hub のマージキューと CI のトリガーの設定は人がします(エージェントは変えません。[CONTRIBUTING.md](CONTRIBUTING.md#task-hub-のマージキューと-ci-のトリガーを有効にする))
