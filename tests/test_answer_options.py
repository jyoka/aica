"""問いの選択肢に答えの候補を入れる(会話の改善 C (5/10)、jyoka/tasks#226)。実 LLM なし、実 API なし。

jyoka/tasks#221 の会話(evals/baseline/sim-20261025-230120.json)では、87 ターンのうち 28 ターンで、問いの選択肢が
逃げ道だけ(「それ以外 / 今はいい」など)だった。Prompt と playbook を「答えの候補2〜3個と『今はいい』」に直し、
評価の機械判定(evals.checks.turn_violations)に次を足した:
(a) 逃げ道だけの選択肢、(b) 「今はいい」のほかの中身の無い逃げ道(「それ以外」など)、(c) 「文字で話す」の選択肢、
(d) 逃げ道は「今はいい」(入口D の「やめた」も認める)。(e) 候補が作れない自由な問いは、選択肢なしで終えてよい。
"""

from __future__ import annotations

import json

import pytest

from dip_mcp.data import ROOT
from evals.checks import conversation_checks, escape_only, turn_violations
from evals.run import recheck

BASELINE_221 = ROOT / "evals" / "baseline" / "sim-20261025-230120.json"
BASELINE_H = ROOT / "evals" / "baseline" / "sim-20261004-113109.json"
PROMPT = (ROOT / "dip_mcp" / "prompts" / "baitoru_assistant.md").read_text(encoding="utf-8")
COMMON_CHAT = (ROOT / "dip_mcp" / "playbooks" / "common_chat.md").read_text(encoding="utf-8")
ONLY = "質問の選択肢が逃げ道だけ"
EXTRA = "「今はいい」のほかに中身の無い逃げ道がある"
NO_ESCAPE = "質問の選択肢に逃げ道(今はいい)がない"
TEXT = "「文字で話す」の選択肢がある"


def t(bubbles, quick_replies=(), actions=()):
    return {"bubbles": list(bubbles), "quick_replies": list(quick_replies), "cards": [], "buttons": [],
            "tools": [], "actions": list(actions), "unknown_job_ids": []}


def kinds(r: dict) -> list[str]:
    return [k for k in (ONLY, EXTRA, NO_ESCAPE, TEXT) if any(k in v for v in turn_violations(0, r))]


# ----------------------------- (a)〜(d) 1ターンの判定 -----------------------------


@pytest.mark.parametrize("quick", [
    ["それ以外", "今はいい"],  # #221 で 28 ターンのうちいちばん多い形(T-career-fulltime の T2〜T7)
    ["思いつかない", "それ以外", "今はいい"],
    ["特にない", "それ以外", "今はいい"],
    ["まだ決めていない", "それ以外", "今はいい"],
    ["今はいい"],  # 「それ以外」を消しただけでも、答えの候補が無い
    # 「それ以外」を禁じた後にモデルが流れやすい言い換え(「まだ」「特に」と語尾の揺れ)
    ["まだ分からない", "今はいい"],
    ["特に決めていない", "今はいい"],
    ["わかりません", "今はいい"],
    ["特にありません", "今はいい"],
    # H の基準(sim-20261004-113109)の T-career-fulltime T6 と T-blank-parent T5
    ["特に決めていない", "それ以外", "今はいい"],
    ["まだ分からない", "それ以外", "今はいい"],
])
def test_escape_only_options_are_caught(quick):
    r = t(["今回、バイトを探そうと思ったきっかけは何ですか?"], quick)
    assert escape_only(r) and kinds(r) == [ONLY]


@pytest.mark.parametrize("bubbles, quick, actions", [
    # 本人の言葉から作った答えの候補と「今はいい」
    (["コンビニで楽しかったのは、どんな場面でしたか?"], ["常連さんとの会話", "品出しの段取り", "今はいい"], ()),
    # 候補が作れない自由な問いは、選択肢を付けずに文で答えてもらう
    (["今回、バイトを探そうと思ったのはなぜですか?『生活費の足しに』のような一言で大丈夫です。"], [], ()),
    # 履歴書・音声の提案を付けた経歴の問いは、ボタンが答え方になるので「今はいい」だけでよい
    (["これまでどんなお仕事をされてきましたか?", "職務経歴書や履歴書があれば、読み込むと早いです。"], ["今はいい"],
     ("resume_upload", "voice_consult")),
    # 問いでない返信(締め)は数えない
    (["承知しました。またいつでも声をかけてください。"], ["今はいい"], ()),
    # 「まだ決めていない」は時期の問いの答えになる
    (["いつごろから働きたいですか?"], ["来月から", "すぐに", "まだ決めていない", "今はいい"], ()),
    # 空欄の数字の問いの「分からない」も答えになる
    (["経費精算の確認は、何件でしたか?"], ["30件", "分からない", "今はいい"], ()),
    # 入口D(応募フォームの離脱)は、入口の指示どおり「やめた」が断りの選択肢
    (["コンビニ夜勤の応募フォームで、入力が残っています。どこで止まりましたか?"],
     ["入力が面倒だった", "仕事内容が不安", "条件が合わなかった", "やめた"], ()),
])
def test_answer_candidates_and_free_questions_are_not_violations(bubbles, quick, actions):
    r = t(bubbles, quick, actions)
    assert not escape_only(r) and kinds(r) == []


def test_extra_escapes_besides_ima_wa_ii_are_caught():
    """逃げ道は「今はいい」1つ。候補と並んでいても、「それ以外」「特にない」「思いつかない」は付けない。"""
    assert kinds(t(["どのエリアで探しますか?"], ["新宿", "渋谷", "それ以外", "今はいい"])) == [EXTRA]
    assert kinds(t(["避けたいことはありますか?"], ["立ち仕事", "特にない", "今はいい"])) == [EXTRA]
    # 言い換えも数える
    assert kinds(t(["避けたいことはありますか?"], ["立ち仕事", "特にありません", "今はいい"])) == [EXTRA]
    assert kinds(t(["気になる職種はありますか?"], ["接客", "事務", "思いつきません", "今はいい"])) == [EXTRA]
    # #221 の T-side-job T3: 「今はいい」が無く、「それ以外」がある
    assert kinds(t(["夜からですね。どのエリアで探しますか？"], ["新宿", "渋谷", "都内ならどこでも", "それ以外"])) == [
        EXTRA, NO_ESCAPE]


def test_text_mode_option_is_caught():
    """#221 の C-consult-propose T7 と T-switch-to-fulltime T4: LINE ではもともと文字で話している。"""
    r = t(["これまでに経験したお仕事やバイトはありますか?", "職務経歴書や履歴書があれば、読み込むと早いです。"],
          ["文字で話す", "今はいい"], ("resume_upload", "voice_consult"))
    assert kinds(r) == [TEXT]


# ----------------------------- (e) 自由な問いで終える会話 -----------------------------


def test_a_free_question_may_end_without_options_but_a_statement_may_not():
    sc = {"expect_outcome": ["consult"]}
    greeting = t(["はじめまして。今日は何から始めますか?"], ["求人を探す", "まず相談する", "今はいい"])
    said = [None, "まず相談する"]
    free = t(["いくつか伺いながら一緒に整理します。", "今回、バイトを探そうと思ったのはなぜですか?"])
    statement = t(["いくつか伺いながら一緒に整理します。"])
    assert not any("最後の返信に選択肢がない" in v for v in conversation_checks(sc, [greeting, free], said)["violations"])
    assert "T2: 最後の返信に選択肢がない(次に開いた人が書くしかない)" in conversation_checks(
        sc, [greeting, statement], said)["violations"]


# ----------------------------- #221 の会話で数え直す(費用なし) -----------------------------


def test_221_conversations_have_28_escape_only_turns_out_of_87():
    runs = [r for r in json.loads(BASELINE_221.read_text(encoding="utf-8"))["runs"] if r["status"] == "ok"]
    checked = {r["case"]: recheck(r["detail"]) for r in runs}
    assert sum(c["turns"] for c in checked.values()) == 87
    assert sum(len(c["escape_only_turns"]) for c in checked.values()) == 28
    assert checked["T-career-fulltime"]["escape_only_turns"] == [2, 3, 4, 5, 6, 7]
    assert "T2: " + ONLY + "(答えの候補が無い): ['それ以外', '今はいい']" in checked["T-career-fulltime"]["violations"]
    # 「文字で話す」は別の違反で数える(#221 の直す所 11)
    assert any(TEXT in v for v in checked["C-consult-propose"]["violations"])
    assert any(TEXT in v for v in checked["T-switch-to-fulltime"]["violations"])


def test_h_baseline_counts_rephrased_non_answers_as_escape_only():
    """H の基準では「特に決めていない / それ以外 / 今はいい」「まだ分からない / それ以外 / 今はいい」も逃げ道だけに数え、
    92 ターン中 25 ターンになる(言い換えを取りこぼすと 23 ターン)。"""
    runs = [r for r in json.loads(BASELINE_H.read_text(encoding="utf-8"))["runs"] if r["status"] == "ok"]
    checked = {r["case"]: recheck(r["detail"]) for r in runs}
    assert sum(c["turns"] for c in checked.values()) == 92
    assert sum(len(c["escape_only_turns"]) for c in checked.values()) == 25
    assert 6 in checked["T-career-fulltime"]["escape_only_turns"]
    assert 5 in checked["T-blank-parent"]["escape_only_turns"]


# ----------------------------- Prompt と playbook の決まり -----------------------------


def test_prompt_and_playbook_ask_for_answer_candidates_and_keep_ima_wa_ii():
    rule = next(line for line in PROMPT.splitlines() if line.startswith("- 選択肢(クイックリプライ)は最大4つ"))
    assert "答えの候補2〜3個と「今はいい」" in rule and "逃げ道は「今はいい」1つだけ" in rule
    assert "必ず「今はいい」か「それ以外」" not in PROMPT
    assert "答えの候補2〜3個と「今はいい」" in COMMON_CHAT
    assert "「それ以外」「今はいい」だけにして" not in COMMON_CHAT
    # 候補が作れない自由な問いは、選択肢を付けずに文で答えてもらう(逃げ道だけにしない)
    assert "選択肢を付けずに文で答えてもらう" in PROMPT and "選択肢を付けずに文で答えてもらう" in COMMON_CHAT
    assert "「文字で話す」のような選択肢は付けない" in PROMPT
