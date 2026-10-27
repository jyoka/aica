"""正社員のヒアリングの評価(設計メモ docs/design/fulltime-hearing.md §6)。使い方は README.md。

personas(事実表)→ actor(本人役と、時間・いらいらを数える Clock)→ judge(ラベル、事実の取り出しと判定)
→ forms(比べる形と台本の AI)→ conversation(1 会話)→ report(表)→ run(実行のコマンド)。
"""
