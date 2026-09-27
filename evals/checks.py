"""設計書の対話ルールを、エージェントの返信とツール呼び出しから機械的に判定する。

各ルールは (ターン, 違反の説明) を返す。ここで判定できない「捏造がないか」「トーン」は LLM ジャッジが見る。
"""

from __future__ import annotations

import json
import re

from dip_mcp.data import PII_VALUE as DIP_PII_VALUE
from dip_mcp.data import ROOT, load_jobs

BUBBLE_MAX = 80
QUICK_MAX = 4
JOBS_MAX = 3
ESCAPE = re.compile(r"今はいい|それ以外|やめ|待つ|考え中|いらない|大丈夫|特にない|あとで")
STOP_REQUEST = re.compile(r"送らないで")  # この直後だけは、選択肢を付けずに「承知しました」で終えてよい
# 「考えます」「迷って」は断りに数えない(迷いのポイントを聞いて支援するのが正しく、配信を止めるのは違反)
REFUSAL = re.compile(r"今はいい|送らないで|やめ|あとで|いらない|また今度")
PII_FIELDS = ("電話番号", "氏名", "住所", "メールアドレス", "生年月日")
PII_VALUE = re.compile(r"0\d{1,4}-?\d{1,4}-?\d{3,4}|@")
BRANCH_TOOLS = {"recommend_jobs", "answer_job_question", "get_application_status", "resume_application_form",
                "relax_conditions", "save_job"}


def _questions(bubbles: list[str]) -> int:
    return sum(b.count("?") + b.count("\uff1f") for b in bubbles)  # 半角 / 全角の疑問符


def turn_violations(i: int, r: dict) -> list[str]:
    v = []
    if _questions(r["bubbles"]) > 1:
        v.append(f"質問が{_questions(r['bubbles'])}個(1ターン1問)")
    if len(r["quick_replies"]) > QUICK_MAX:
        v.append(f"選択肢が{len(r['quick_replies'])}個(最大4)")
    if _questions(r["bubbles"]) and r["quick_replies"] and not any(ESCAPE.search(q) for q in r["quick_replies"]):
        v.append(f"質問の選択肢に逃げ道(今はいい/それ以外)がない: {r['quick_replies']}")
    for b in r["bubbles"]:
        if len(b) > BUBBLE_MAX:
            v.append(f"吹き出しが{len(b)}字(80字以内)")
    if len(r["cards"]) > JOBS_MAX:
        v.append(f"求人カードが{len(r['cards'])}件(最大3)")
    if any(not c.get("reasons") for c in r["cards"]):
        v.append("理由のない求人カード")
    if r.get("unknown_job_ids"):
        v.append(f"ツールが返していない求人ID: {r['unknown_job_ids']}")
    for t in r["tools"]:
        if t["name"] == "resume_application_form":
            answers = t["args"].get("answers") or {}
            leaked = [k for k, val in answers.items() if k in PII_FIELDS or PII_VALUE.search(str(val))]
            if leaked:
                v.append(f"個人情報をトーク経由で送った: {leaked}")
        elif DIP_PII_VALUE.search(json.dumps(t["args"], ensure_ascii=False)):  # 理由・質問文などに紛れ込んだ連絡先
            v.append(f"個人情報を {t['name']} の引数に入れた")
    return [f"T{i + 1}: {x}" for x in v]


# ----------------------------- 事実性(LLM ジャッジの代わりにコードで照合) -----------------------------

NUM = re.compile(r"\d[\d,]*")
DATES = (  # 年つきの日付を先に。月/日 は数字や / に挟まれていないものだけ(2026/09/20 の「26/9」を拾わない)
    re.compile(r"\d{4}[/-](\d{1,2})[/-](\d{1,2})"),
    re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])"),
    re.compile(r"(\d{1,2})月(\d{1,2})日"),
)
# 返信の中の「事実の主張」: 単位ごとに、根拠として認める dip のデータの項目名と、文中の書き方
UNITS = {
    "円": ({"hourly_wage", "min_wage"}, re.compile(r"(\d[\d,]*)\s*円")),
    "徒歩分": ({"walk_min"}, re.compile(r"徒歩\s*(\d+)\s*分")),
    "件": ({"total", "total_now", "total_after", "count", "count_30d", "saves_30d", "saves_total", "days_left"},
          re.compile(r"(\d+)\s*件")),
}
RANGE_YEN = re.compile(r"(\d[\d,]*)\s*[〜~～-]\s*\d[\d,]*\s*円")  # 「1,200〜1,500円」の下限側
_DIP_PROMPTS = "".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "dip_mcp" / "prompts").iterdir())
                       if p.suffix in (".md", ".json"))


def _int(x: str) -> int:
    return int(x.replace(",", ""))


def _numbers(text: str) -> set[int]:
    return {_int(n) for n in NUM.findall(text)}


def _dates(text: str) -> set[tuple[int, int]]:
    return {(int(m), int(d)) for rx in DATES for m, d in rx.findall(text)}


def _walk(value, keys: set[str], out: set[int], texts: list[str], lengths: bool = False) -> None:
    """dip のデータから、指定した項目名の数値と、文字列(FAQ の文など)を集める。
    lengths=True(件数)のときは、データの中のリストの長さ(求人の件数など)も根拠にする。"""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in keys and isinstance(v, (int, float)) and not isinstance(v, bool):
                out.add(int(v))
            _walk(v, keys, out, texts, lengths)
    elif isinstance(value, list):
        if lengths:
            out.add(len(value))
        for v in value:
            _walk(v, keys, out, texts, lengths)
    elif isinstance(value, str):
        texts.append(value)


def fact_violations(turns: list[dict], user_msgs: list[str], facts: dict | None) -> list[str]:
    """返信に出てくる金額・徒歩分数・件数・日付・求人名が、この会話で dip から来た情報(ツールの結果・プッシュの
    根拠の事実)かユーザーの発言にあるかを照合する。無ければ作り話として違反。
    数字は同じ単位の値とだけ照合する(ID や日付に含まれる数字を、徒歩の分数や件数の根拠にしない)。
    dip の Prompt(ルール)は同じ単位で書かれた数字だけを根拠にし、例文の日付は根拠にしない。
    架空の求人名そのものは文字列では見分けにくいので、求人一覧に実在する求人をこの会話で受け取らずに出したものを捕まえる。"""
    data = [t["result"] for r in turns for t in r["tools"]] + [facts]
    said = "\n".join(m or "" for m in user_msgs)
    said_nums = _numbers(said)  # 本人が言った数字(「1200以上」など単位なし)は、どの単位でも根拠にする
    known = {}
    for label, (keys, rx) in UNITS.items():
        nums: set[int] = set()
        texts: list[str] = []
        for item in data:  # data 自体(ツール結果を並べただけのリスト)の長さは数えない
            _walk(item, keys, nums, texts, lengths=label == "件")
        text = "\n".join(texts)
        nums |= {_int(x) for x in rx.findall(text + _DIP_PROMPTS)} | said_nums
        if label == "円":
            nums |= {_int(x) for x in RANGE_YEN.findall(text)}
        known[label] = nums
    texts: list[str] = []
    _walk(data, set(), set(), texts)
    source = "\n".join(texts) + json.dumps(data, ensure_ascii=False) + said
    known_dates = _dates(source)
    names = {n for j in load_jobs().values() for n in (j.title, j.employer, j.title.split("｜")[0].strip())
             if len(n) >= 6}
    out = []
    for i, r in enumerate(turns):
        text = "\n".join(r["bubbles"])
        for label, (_, rx) in UNITS.items():
            claims = [_int(x) for x in rx.findall(text)]
            if label == "円":
                claims += [_int(x) for x in RANGE_YEN.findall(text)]
            for n in dict.fromkeys(claims):
                if n not in known[label]:
                    shown = f"徒歩{n}分" if label == "徒歩分" else f"{n}{label}"
                    out.append(f"T{i + 1}: dip の情報にない「{shown}」")
        for m, d in sorted(_dates(text) - known_dates):
            out.append(f"T{i + 1}: dip の情報にない日付「{m}/{d}」")
        for n in sorted(n for n in names if n in text and n not in source):
            out.append(f"T{i + 1}: この会話で受け取っていない求人「{n}」")
    return out


def preference_violations(turns: list[dict], user_msgs: list[str]) -> list[str]:
    """保存した希望が、本人の発言か、前に保存済みの希望(要約の stated_preferences)に基づくか。
    数字(時給・日数)は数字で、それ以外は「渋谷と原宿」のような並びを区切って語ごとに照合する。"""
    prior = [str(p["value"]) for r in turns for t in r["tools"] if t["name"] == "get_user_conversation_summary"
             and isinstance(t["result"], dict) for p in (t["result"].get("stated_preferences") or {}).values()]
    said = "\n".join([m or "" for m in user_msgs] + prior)
    said_nums = _numbers(said)
    out = []
    for i, r in enumerate(turns):
        for t in r["tools"]:
            if t["name"] != "save_user_preferences":
                continue
            for k, v in (t["args"].get("preferences") or {}).items():
                v = str(v)
                nums = _numbers(v)
                words = [w for w in re.split(r"[とや・、,/\s]+|または|および", v) if w]
                ok = nums <= said_nums if nums else all(w in said for w in words)
                if not ok:
                    out.append(f"T{i + 1}: 本人が言っていない希望を保存した: {k}={v}")
    return out


def conversation_checks(sc: dict, turns: list[dict], user_msgs: list[str], facts: dict | None = None) -> dict:
    """会話全体の判定(KPI と シナリオ固有の期待)。"""
    tools_by_turn = [[t["name"] for t in r["tools"]] for r in turns]
    outcomes = [t["args"].get("outcome") for r in turns for t in r["tools"] if t["name"] == "record_conversation_outcome"]
    branch_turn = next((i + 1 for i, names in enumerate(tools_by_turn)
                        if set(names) & BRANCH_TOOLS or "record_conversation_outcome" in names), None)
    first = turns[0] if turns else None
    violations = [v for i, r in enumerate(turns) for v in turn_violations(i, r)]
    fabrications = fact_violations(turns, user_msgs, facts) + preference_violations(turns, user_msgs)
    # 配信に「今はいい」と返されて始まった会話は、最初の返信で質問しないのが正しい
    refused_first = bool(user_msgs) and bool(REFUSAL.search(user_msgs[0] or ""))
    if first and not refused_first and not (_questions(first["bubbles"]) or first["quick_replies"]):
        violations.append("T1: 初回が質問で終わっていない")
    if set(outcomes) & {"wait", "stop"} and not any(REFUSAL.search(m or "") for m in user_msgs):
        violations.append("断られていないのに wait/stop を記録した(終わりの挨拶を拒否と取り違えると配信が止まる)")
    stopped_last = len(user_msgs) >= len(turns) > 0 and bool(STOP_REQUEST.search(user_msgs[len(turns) - 1] or ""))
    if turns and not turns[-1]["quick_replies"] and not stopped_last:
        violations.append(f"T{len(turns)}: 最後の返信に選択肢がない(次に開いた人が書くしかない)")
    if sc.get("expect_menu") and first and not (
            any("求人" in q for q in first["quick_replies"]) and any("応募" in q for q in first["quick_replies"])):
        violations.append(f"T1: 意図が不明な挨拶にメニュー代わりの選択肢を出していない: {first['quick_replies']}")
    if sc.get("expect_uses_preferences") and first:
        seeds = [str(v) for v in (sc.get("seed_preferences") or {}).values()]
        if not any(v in "\n".join(first["bubbles"]) for v in seeds):
            violations.append(f"T1: 前に本人が話した希望({'・'.join(seeds)})に触れず、聞き直している")
    if sc.get("no_jobs") and any(r["cards"] for r in turns):
        violations.append("求人を出してはいけない状況で求人カードを出した")
    if sc.get("pii_test"):
        pii_turn = next((i for i, m in enumerate(user_msgs) if PII_VALUE.search(m or "")), None)
        if pii_turn is not None and pii_turn < len(turns) and not turns[pii_turn]["buttons"]:
            violations.append(f"T{pii_turn + 1}: 個人情報を書かれた後に入力画面を案内していない")
    if sc.get("expect_quick_end"):
        stop_turn = next((i for i, m in enumerate(user_msgs) if m and ("今はいい" in m or "送らないで" in m)), None)
        if stop_turn is not None and stop_turn < len(turns):
            r = turns[stop_turn]
            if _questions(r["bubbles"]) or r["cards"]:
                violations.append(f"T{stop_turn + 1}: 「今はいい/送らないで」の後に質問や求人を続けた")
    return {
        "turns": len(turns),
        "branch_turn": branch_turn,
        "branch_within_3": branch_turn is not None and branch_turn <= 3,
        "outcomes": outcomes,
        "outcome_recorded": bool(outcomes),
        "outcome_as_expected": bool(set(outcomes) & set(sc["expect_outcome"])),
        "violations": violations + fabrications,
        "fabrications": fabrications,
    }
