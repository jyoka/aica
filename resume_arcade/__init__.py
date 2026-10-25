"""レジュメ・アーケード(docs/prd/resume-arcade.md の (3/3)): ゲームの AI と、できた書類の写し。

LLM を使うが、会話でも業務の規則でもない部品(履歴書の読み取り resume_intake と同じ扱い。HLD の 1.1)。
- stages: 段ごとの指示の文(prompts)と出力の形(schemas)で、aica の LLM のプロバイダ(line_runtime.llm)を 1 回呼び、
  事実を確かめる(本人の言葉に無い数字は空欄に、無い引用は捨てる。決まりは依存の無い resume_lines)
- draft: できた書類の Markdown を Resume Draft(source: arcade)に写す
保存してよいかの決まり(遊びの記録、AI の回数、下書きの置き場所)は dip の関数(dip_mcp.arcade)にある。
dip_mcp はこのパッケージを import しない。
"""
