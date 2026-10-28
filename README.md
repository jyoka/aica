# aica

**求職者と話して、アルバイト・パートから正社員まで、その人に合う仕事探しを相談と提案で支える AI エージェント。**

- LINE 公式アカウントの AI アシスタント(バイトル)を手元で再現する検証環境(旧称 line-sim)から始まりました
- LINE 側(会話の頭脳)と dip 側(求人とユーザー理解)に分け、**エージェントは dip のツール(MCP)を通してだけ dip の事実を得る**という境界を守っています。
  Web サーバーは dip の関数を直接呼びます([ADR-0003](docs/adr/0003-agent-gets-facts-only-through-dip-tools.md))
- 確かめたいのは「dip が書ける Prompt とツールの中身だけで、狙った会話体験になるか」です
- 求人、店舗、ユーザー、FAQ はすべて架空のデータです。社内資料の数値は含めていません

旧リポジトリ `jyoka/aica_ra_a2a_poc`(main、コミット `cda42fd`)から、履歴なしで移植しました。旧リポジトリの評価結果、
作業ログ、アーカイブは持ってきていません。履歴書の読み取り(`resume_intake/`)は task 63、音声相談(`voice_resume/`)は task 62 で移しました。

## しくみ

```mermaid
flowchart LR
    seeker(["求職者<br/>ブラウザ"])
    lineu(["求職者<br/>LINE アプリ"])
    lineapi[("LINE プラットフォーム")]
    web["Web プロセス(line_runtime)<br/>Web サーバー + エージェント"]
    dip["dip の MCP サーバー(dip_mcp)<br/>dip のツール、dip の Prompt、playbook"]
    lk[("LiveKit Cloud")]
    worker["音声ワーカー(voice_resume)<br/>音声アドバイザー"]
    data[("data/<br/>dip の状態、会話")]
    llm[("OpenAI / Anthropic")]

    seeker -- "トーク、履歴書" --> web
    lineu -- "トーク" --> lineapi -- "webhook" --> web
    web -- "MCP" --> dip
    web --> llm
    seeker -- "音声" --> lk --> worker
    worker -- "MCP" --> dip
    worker -- "結果" --> web
    dip --> data
    web --> data
```

- エージェントは業務の知識を持ちません。LINE の共通ルールと、dip から受け取る dip の Prompt と playbook で話します
- dip のツールは LLM を使わず、事実だけを返します。求人カードは、dip が返した求人だけをコードで描きます
- 音声相談もトークと同じ雇用区分、playbook、プロフィールを使い、結果はトークに戻ります

部品とデータの流れは [HLD](docs/architecture/hld.md) にあります。

## 特長

- 求職者と話すチャネルは 2 つ: Web のトーク画面(LINE 風の画面)と LINE 公式アカウント。LINE は設定したときだけ開きます([LINE で試す](docs/usage.md#line-で試す))
- アルバイト・パートの相談と、正社員のヒアリングのハーネスを持ちます
- 履歴書の読み込み、音声相談、レジュメ・アーケード(遊びながら職務経歴書を作るゲーム)から、プロフィールを育てます
- LINE では、LIFF 画面、リッチメニュー、アカウント連携(dip の会員と結ぶ)が使えます
- 開発者画面で、シナリオごとに dip から見た状態と、ターンごとの処理の流れを見られます
- 模擬ユーザーとジャッジで会話を採点する評価と、モックの LLM で回す回帰を持ちます
- 1 日のターン数の上限と費用台帳で、費用を抑えます

## はじめる

要るもの: Python 3.12、Anthropic の API キー、OpenAI の API キー(求人の意味検索の埋め込みだけに使う)。

```bash
python3.12 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
# リポジトリ直下の .env に ANTHROPIC_API_KEY=... と OPENAI_API_KEY=... を書く(git には入らない)
./.venv/bin/python -m line_runtime.server   # トーク画面 http://127.0.0.1:8030/ 、開発者画面 http://127.0.0.1:8030/dev
```

- `ANTHROPIC_API_KEY` が無いと、Web プロセスは起動の途中で止まります。`OPENAI_API_KEY` が無いと、検索は部分一致に戻ります
- 音声相談、Render での公開、LINE で試す手順は [docs/usage.md](docs/usage.md) にあります
- 環境変数の全部は [docs/configuration.md](docs/configuration.md) にあります

## 使い方

1. トーク画面(`http://127.0.0.1:8030/`)を開き、同意の画面で「同意して始める」を押す。
   押すとエージェントが挨拶のために実 API を呼び、費用がかかります
2. トークに希望を書くと、エージェントが質問し、dip が返した求人をカードで出します
3. 入力欄の「+」から、履歴書のアップロードと音声相談に入れます
4. 開発者画面(`/dev`)では、左でシナリオを選び、中央で話し、右で構成図とこのターンの処理の流れを見ます。シナリオを選ぶまで実 API を呼びません

画面の一覧、LINE、LIFF、リッチメニュー、マイクの確認、アカウント連携の手順は [docs/usage.md](docs/usage.md) にあります。

## コマンド

`python` は `./.venv/bin/python` のことです。全部の一覧と「実 API」の列は [docs/usage.md の「主なコマンド」](docs/usage.md#主なコマンド)にあります。

| コマンド | すること | 実 API |
|---|---|---|
| `python -m line_runtime.server` | Web プロセスを起動する(`127.0.0.1:8030`) | 挨拶と会話で使う |
| `python -m voice_resume.agent dev` | 音声ワーカーを起動する | 使う |
| `python -m pytest -q` | テスト | 使わない |
| `python -m evals.baito_sim` | アルバイト・パートのハーネスの回帰(モックの LLM) | 使わない |
| `python -m evals.career_sim` | 正社員のヒアリングのハーネスの回帰(モック) | 使わない |
| `python -m evals.run` | 評価。模擬ユーザーとシナリオを会話させて採点する | 使う |
| `python -m line_runtime.ledger` | 費用台帳を人と日ごとに集計する | 使わない |

## ドキュメント

| 知りたいこと | 読むもの |
|---|---|
| 動かし方、LINE、LIFF、リッチメニュー、マイクの確認、アカウント連携、画面、コマンドの全部 | [docs/usage.md](docs/usage.md) |
| 設定、環境変数、保存する期間、ターン数の上限 | [docs/configuration.md](docs/configuration.md) |
| Render での公開の手順、通知とメモリ不足の毎日の点検、環境変数、置くデータ | [docs/deploy/render.md](docs/deploy/render.md) |
| 運用の決定(段階、7 つの関門、落ちたときの対応、次にやること) | [docs/architecture.md](docs/architecture.md)(Architecture Brief) |
| 何を作るか(aica v2、レジュメ・アーケード、アルバイト・パートのハーネス) | [docs/prd/](docs/prd/) |
| 全体の図、部品、データの流れ、信頼の境界、用語 | [docs/architecture/hld.md](docs/architecture/hld.md)(HLD) |
| モジュール、HTTP の口、dip のツール、データの形、失敗の扱い、テスト | [docs/architecture/lld.md](docs/architecture/lld.md)(LLD) |
| 設計の問題と直し方の案 | [docs/architecture/review-2026-10.md](docs/architecture/review-2026-10.md) |
| 決めたことの記録 | [docs/adr/](docs/adr/) |
| 機能ごとの設計メモ(シナリオごとの会話設計、個人情報など) | [docs/design/](docs/design/) |
| 利用規約とプライバシーポリシー | [docs/legal/](docs/legal/README.md) |
| 応募の先の設計の調査 | [docs/research/apply-flow.md](docs/research/apply-flow.md) |
| 業務の語の定義 | [CONTEXT.md](CONTEXT.md) |
| 評価の結果の集計と比較 | [evalkit/README.md](evalkit/README.md) |

主な文書の一覧(ADR の 1 件ずつなど)は [docs/usage.md の「文書の一覧」](docs/usage.md#文書の一覧)にあります。

## 開発

```bash
python -m pytest -q            # テスト(実 API を呼ばない。.env を置かずに回す)
./.venv/bin/ruff check .       # lint
python -m evals.baito_sim      # アルバイト・パートのハーネスの回帰
```

エージェントで作業するときは [AGENTS.md](AGENTS.md)、人の決まり(書き方、置き場所、回帰の合格基準)は [CONTRIBUTING.md](CONTRIBUTING.md) を読んでください。
