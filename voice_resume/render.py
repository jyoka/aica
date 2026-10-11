"""下書き(envelope JSON)→ 読める Markdown。

書類の種類(envelope の document_kind)で見出しと並びを変える(aica v2 の G):
- 履歴書(document_kind の無い 1.0 の下書きも): 基本情報 → 職務要約 → 学歴 → 職歴・経験 → … → 本人の希望
- 職務経歴書: 職務要約 → 職務経歴(職歴ごとの業務と成果)→ 活かせる経験・知識・技術 → 資格 → 自己PR → 本人の希望。
  学歴は職務経歴書に書かないので出さない(JSON には残る)
"""

from __future__ import annotations

from resume_draft.schema import document_kind_of

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
    """envelope(resume_draft.schema.envelope の返り値)を、書類の種類に合わせた下書きの Markdown にする。"""
    kind = document_kind_of(env)
    out = [f"# {kind}(下書き)", ""]
    out.append(f"作成: {env.get('created_at', '')} / 元データ: {env.get('source', '')}")
    out.append("")
    out.append("> この下書きは、面談で本人が話した内容だけから作られています。")
    out.append("> 内容を必ずご自身で確認・修正してから使ってください。")
    body = _career_sheet(env) if kind == "職務経歴書" else _resume(env)
    return "\n".join(out + body) + "\n"


def _resume(env: dict) -> list[str]:
    """履歴書の並び。"""
    resume = env.get("resume", {})
    out = [_section("基本情報(ご自身で記入してください)")]
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
            out.append(f"- {_work_head(w)}")
            for d in w.get("duties", []):
                out.append(f"  - {d}")
            for a in w.get("achievements", []):
                out.append(f"  - 工夫・成果: {a}")
    else:
        out.append("-(会話に出なかったため空欄)")

    out += _other_experience(resume)

    if resume.get("skills"):
        out.append(_section("できること・得意なこと"))
        out.extend(f"- {s}" for s in resume["skills"])

    out += _certifications(resume)

    if resume.get("self_pr"):
        out.append(_section("自己PR"))
        out.append(resume["self_pr"])

    return out + _preferences(resume) + _unconfirmed(resume)


def _career_sheet(env: dict) -> list[str]:
    """職務経歴書の並び: 職務要約と、職歴ごとの業務・成果を先に出す。個人情報の欄は氏名だけ(本人が記入する)。"""
    resume = env.get("resume", {})
    out = [_section("氏名(ご自身で記入してください)")]
    out.append(f"- {_PERSONAL_LABELS['name']}: {env.get('personal', {}).get('name') or '____________'}")

    out.append(_section("職務要約"))
    out.append(resume.get("job_summary") or "(会話に出なかったため空欄。ご自身で記入してください)")

    out.append(_section("職務経歴"))
    if resume.get("work_history"):
        for w in resume["work_history"]:
            out.append(f"\n### {_work_head(w)}")
            if w.get("duties"):
                out.append("- 業務内容:")
                out.extend(f"  - {d}" for d in w["duties"])
            if w.get("achievements"):
                out.append("- 成果:")
                out.extend(f"  - {a}" for a in w["achievements"])
    else:
        out.append("-(会話に出なかったため空欄)")

    if resume.get("skills"):
        out.append(_section("活かせる経験・知識・技術"))
        out.extend(f"- {s}" for s in resume["skills"])

    out += _certifications(resume)

    if resume.get("self_pr"):
        out.append(_section("自己PR"))
        out.append(resume["self_pr"])

    out += _other_experience(resume)
    return out + _preferences(resume) + _unconfirmed(resume)


def _work_head(w: dict) -> str:
    return " ".join(x for x in [w.get("period"), w.get("organization"), w.get("employment_type"), w.get("role")] if x)


def _other_experience(resume: dict) -> list[str]:
    if not resume.get("other_experience"):
        return []
    out = [_section("その他の経験(学校・家庭・地域など)")]
    for o in resume["other_experience"]:
        line = f"{o.get('kind', '')}: {o.get('title', '')}"
        if o.get("description"):
            line += f"({o['description']})"
        out.append(f"- {line}")
    return out


def _certifications(resume: dict) -> list[str]:
    out = [_section("資格・免許")]
    if resume.get("certifications"):
        for c in resume["certifications"]:
            line = c.get("name", "")
            if c.get("acquired"):
                line += f"({c['acquired']})"
            out.append(f"- {line}")
    else:
        out.append("- 特になし(会話に出なかった場合も空欄です)")
    return out


def _preferences(resume: dict) -> list[str]:
    prefs = resume.get("preferences", {})
    if not any(prefs.get(k) for k in ("desired_work", "conditions", "notes")):
        return []
    out = [_section("本人の希望")]
    for label, key in (("やってみたい仕事", "desired_work"), ("働き方の希望", "conditions"), ("その他", "notes")):
        for v in prefs.get(key, []):
            out.append(f"- {label}: {v}")
    return out


def _unconfirmed(resume: dict) -> list[str]:
    if not resume.get("unconfirmed"):
        return []
    return [_section("確認したい点(下書きに入れていません)"), *(f"- {u}" for u in resume["unconfirmed"])]
