# 会話の層の場面の一覧

会話の層(設計メモ [conversation-layer.md](../../docs/design/conversation-layer.md) §3)の物差しです。
どの入り口でも同じ形で、起きうる発言と、期待する振る舞いを並べます。どれも架空のデータです。

## 置き場所

| 場所 | 中身 |
|---|---|
| `career/` | 正社員ヒアリングの途中の場面 |
| `baito/` | アルバイト・パートの相談の途中の場面 |
| `talk/` | 相談の外(トークの AI)の場面。最初のひとこと、相談の前後 |
| `__init__.py` | 読み込み(`load_scenes`)と形の確かめ(`validate`)。場面の形と読む段の正解(`reading`)の形は docstring にあります |
| `runner.py` | 場面を回す仕組み。入り口と版ごとの回し方(`VERSIONS`)と、点(`tally`) |
| `checks.py` | コードで判定する条件(`CODE_CHECKS`)の中身 |
| `judge.py` | 採点の条件(`JUDGE_CHECKS`)のジャッジのプロンプトと、出力の受け取り方 |

入り口ごとに 1 フォルダ、種類(雑談、逆質問、1 回に複数の答え、直す、感情、守り、短い答え、操作、入り口をまたぐ、jyoka/tasks#326 のトーク)ごとに 1 ファイルです。

## 書き方

- 1 場面は、入り口(`entry`)、架空の前の会話(`known`、`profile`、`history`、`asking`)、本人の発言(`says`)、期待する振る舞い(`expect`)です
- 打った発言には、読む段の正解(`reading`)を置きます。設計メモ §4.3 の act の並びと、相づちに使う本人の言葉(`hook`)です。ボタンには置きません
- 期待する振る舞いは、コードで判定する条件(`CODE_CHECKS`)と、LLM の採点の条件(`JUDGE_CHECKS`)で書きます。どの場面にも、コードの条件を 1 つ以上置きます
- 欄と問いの id は、その入り口の質問バンクのものを使います。入り口を移した後の状態を見る条件には `entry` を書きます
- 会社と人の名前は「みほん」で始めます

## 回し方

```sh
./.venv/bin/python -m evals.scenes                                # 全部の入り口を今の版で(career c1、baito v4、talk t0)
./.venv/bin/python -m evals.scenes --entry career --version c1    # 1 つの入り口と版
./.venv/bin/python -m evals.scenes --entry career --version c2    # 正社員の会話の層の版(task 355)
./.venv/bin/python -m evals.scenes --scene t326-order -v          # 1 場面の返信とすべての条件
./.venv/bin/python -m evals.scenes --log evidence/scenes          # 結果の JSON(results.json)と点(score.md)も書く
```

実 API は呼びません(費用 0。全部で 10 秒ほど。talk は dip の MCP の子プロセスを 1 つ起こす)。

- career と baito は、dip の相談のツール(`session.start`、`session.turn`)を、ランタイムと同じ呼び直しで回します。
  モデルの代わりは台本の LLM です。読む段は `reading` をその版の形に写して返し(c2 は act の並びで読むので、写さずにそのまま)、
  言う段は「相づち + 問いの文」を返します。c2 では、場面の前の会話(`history`)を層の覚え(直前のやりとり)にも入れ、
  計画が相づちなし(逆質問)なら相づちを付けません。「[スタンプ]」は、c1 では返事なし(今の LINE の入口が捨てる)、
  c2 では文の発言として渡します(task 356)。
  相づちはわざと単調(「〜ですね。」)にしてあります。言い回しを散らすのはハーネスの仕事なので、モデルの運で点が変わらないようにするためです。
  c2 の言う段は、問いの文に、ハーネスが勧める言い方(質問バンクの `variants` のどれか)を使います。c2 の返信には部品の並び(`parts`)が付くので、
  `order` はそれで判定します。`keeps_question` は、同じ問いの言い方のどれが返事にあっても合格です(同じ問いを出し直すときは言い方を変えるため)
- 本人がやめて相談を閉じたターンは、ランタイムがトークの AI に渡します。返事の文と、移るか・区分と値を引き継ぐかはトークの AI しだいなので、
  それを見る条件は判定しません(skip)。逆質問の答えの部品(`order` の `answer`)も、部品の並びを返す層でないと判定しません
- talk は、本物のランタイムをモックのトークの AI で回します。判定するのは守りの前処理(`crisis_reply`、`llm_calls_max`)と `stays` だけです。
  ほかの条件は、実モデルで比べるとき(設計メモ §5 の 9 枚目)に見ます
- 採点の条件は判定しません(skip)。`runner.run(judge=judge.judge_with(client, model))` で採点しますが、実モデルを呼ぶので 9 枚目のカードで使います
- 終了コードは、場面を回せなかったときだけ 1 です(条件の不合格では 0。点を出す道具なので)

点の表の列は、入り口と版、場面の数、コードの条件をすべて満たした場面(判定した条件のある場面のうち)、コードの条件の合格、
守りの条件(`crisis_reply`、`no_made_up_number`、`no_slot_contains`)の合格、モックで判定しない条件の数、採点の条件の数です。
今の版の点は設計メモ §7 にあります。

回せる版は `runner.py` の `VERSIONS` にあります(career は `c1` と `c2`)。新しい版(v5、t1)は、ここに回し方を足すと、同じ場面と条件で回せます。

## 確かめ方

```sh
./.venv/bin/python -m pytest -q tests/test_conversation_scenes.py tests/test_scene_runner.py
```

`test_conversation_scenes.py` は、場面の数(全部で 60 以上、種類ごとに 3 以上)、入り口ごとの種類、押したボタンが質問バンクにあるか、
読む段の正解の形、形の誤りを確かめます。`test_scene_runner.py` は、回す仕組みと条件の判定と、ジャッジのプロンプトと出力の受け取り方
(台本の LLM)を確かめます。
