"""履歴書下書き(envelope JSON)→ 読める Markdown。"""

from __future__ import annotations

_PERSONAL_LABELS = {
    "name": "氏名",
    "name_kana": "ふりがな",
    "birth_date": "生年月日",
    "address": "住所",
    "phone": "電話番号",
    "email": "メールアドレス",
}


def _section(title: str) -> str:
    return f"\n## {title}\n"


def to_markdown(env: dict) -> str:
    """envelope(schema.envelope の返り値)を履歴書下書きの Markdown にする。"""
    resume = env.get("resume", {})
    out = ["# 履歴書(下書き)", ""]
    out.append(f"作成: {env.get('created_at', '')} / 元データ: {env.get('source', '')}")
    out.append("")
    out.append("> この下書きは、面談で本人が話した内容だけから作られています。")
    out.append("> 内容を必ずご自身で確認・修正してから使ってください。")

    out.append(_section("基本情報(ご自身で記入してください)"))
    for key, label in _PERSONAL_LABELS.items():
        out.append(f"- {label}: {env.get('personal', {}).get(key) or '____________'}")

    if resume.get("job_summary"):
        out.append(_section("職務要約"))
        out.append(resume["job_summary"])

    out.append(_section("学歴"))
    if resume.get("education"):
        for e in resume["education"]:
            line = " ".join(x for x in [e.get("period"), e.get("school"), e.get("status")] if x)
            out.append(f"- {line}")
    else:
        out.append("-(会話に出なかったため空欄。ご自身で記入してください)")

    out.append(_section("職歴・経験"))
    if resume.get("work_history"):
        for w in resume["work_history"]:
            head = " ".join(x for x in [w.get("period"), w.get("organization"), w.get("employment_type"), w.get("role")] if x)
            out.append(f"- {head}")
            for d in w.get("duties", []):
                out.append(f"  - {d}")
            for a in w.get("achievements", []):
                out.append(f"  - 工夫・成果: {a}")
    else:
        out.append("-(会話に出なかったため空欄)")

    if resume.get("other_experience"):
        out.append(_section("その他の経験(学校・家庭・地域など)"))
        for o in resume["other_experience"]:
            line = f"{o.get('kind', '')}: {o.get('title', '')}"
            if o.get("description"):
                line += f"({o['description']})"
            out.append(f"- {line}")

    if resume.get("skills"):
        out.append(_section("できること・得意なこと"))
        out.extend(f"- {s}" for s in resume["skills"])

    out.append(_section("資格・免許"))
    if resume.get("certifications"):
        for c in resume["certifications"]:
            line = c.get("name", "")
            if c.get("acquired"):
                line += f"({c['acquired']})"
            out.append(f"- {line}")
    else:
        out.append("- 特になし(会話に出なかった場合も空欄です)")

    if resume.get("self_pr"):
        out.append(_section("自己PR"))
        out.append(resume["self_pr"])

    prefs = resume.get("preferences", {})
    if any(prefs.get(k) for k in ("desired_work", "conditions", "notes")):
        out.append(_section("本人の希望"))
        for label, key in (("やってみたい仕事", "desired_work"), ("働き方の希望", "conditions"), ("その他", "notes")):
            for v in prefs.get(key, []):
                out.append(f"- {label}: {v}")

    if resume.get("unconfirmed"):
        out.append(_section("確認したい点(下書きに入れていません)"))
        out.extend(f"- {u}" for u in resume["unconfirmed"])

    return "\n".join(out) + "\n"
