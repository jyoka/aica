"""正社員のヒアリングの評価シナリオ(evals/career_choices.py。task 315)が、どれも合格する。費用 0。

答えの候補(1)、スキップと中断(2)、「他の候補」(3)、あいまいな問い(4)、聞き返し(5)、PdM の規模の聞き返しの回帰と、
短気な本人役のモックの回帰(6)。設計メモ docs/design/fulltime-hearing.md の §5.3。
"""

from __future__ import annotations

import pytest

from evals import career_choices


@pytest.mark.parametrize("name", list(career_choices.CASES))
def test_case_passes(name):
    assert career_choices.CASES[name]() == []


def test_main_says_all_passed(capsys):
    assert career_choices.main([]) == 0
    assert capsys.readouterr().out.strip().endswith("すべて合格")
