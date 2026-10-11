# evalkit

line-sim の評価結果を集計・比較するための小さな道具。標準ライブラリだけで書いてあるので、
どの venv からでも、システムの `python3` からでも動く(以前は ca-agent の評価にも使っていた)。

```bash
python3 evalkit/aggregate.py summary RESULT.json        # ケースごと・全体の集計(95% 区間つき)
python3 evalkit/aggregate.py diff BEFORE.json AFTER.json # 変更前後の差。ノイズ幅を超えたものだけ改善/悪化、内なら「差なし」
python3 -m unittest discover -s evalkit                  # テスト
```

結果ファイルの形と集計のルールは `aggregate.py` の先頭に書いてある。評価ハーネス(`evals/run.py`)は
この形で書き出し、自分の Markdown レポートにも `summary_md` を使う。

目安: 割合の指標は「ケース数 × 回数」が小さいとノイズ幅が大きい(9ケース × 3回 = 27回で、全勝なら区間幅 ±6pt、9割で ±12pt、5割で ±18pt)。
小さな改善を見たいときは回数を増やす。
