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
# この一言が発言そのもののときだけ断りとみなす(「今はいいのが見つからなくて」のような部分一致は断りではない)
QUICK_END = re.compile(r"(もう)?(今はいい|送らないで)(です|かな)?[。!!]?")  # fullmatch で使う
# 「考えます」「迷って」は断りに数えない(迷いのポイントを聞いて支援するのが正しく、wait を記録するのは違反)
REFUSAL = re.compile(r"今はいい|送らないで|やめ|あとで|いらない|また今度")
PII_FIELDS = ("電話番号", "氏名", "住所", "メールアドレス", "生年月日")
# dip と同じ電話番号・メールの判定に、「@」だけでも数える(トークで受け取らない項目の送り先を広めに見る)。
# 年収の桁表記(「年収3000000円」)は電話番号に数えない(dip_mcp/data.py の PII_VALUE)
PII_VALUE = re.compile(DIP_PII_VALUE.pattern + r"|@")
# 再訪の挨拶で行動履歴に触れる言い方(「最近はカフェのお仕事を見ていましたか?」「ご覧になりましたか?」「見てましたか?」
# 「チェックされていましたか?」)。会話を通して1回だけ。本人がしたことを指す過去・進行の形だけを数える。
# 依頼や案内(「見ていただけますか」「見ていきましょう」「ご覧になれます」「ご覧になってください」)や、ボットが希望を
# 保存したという報告(「希望を保存しています」「前回の希望を保存していましたので」)は行動履歴への言及ではない
RECENT_ACTIVITY = re.compile(
    r"(?<!希望を)(?<!条件を)(?<!希望は)(?<!条件は)"
    r"(?:見て|ご覧になって|閲覧して|チェックして|保存して|(?:閲覧|チェック|保存)されて)い(?:た(?!だ|ら)|まし|らっしゃ)"
    r"|見て(?:た(?!ら)|まし)|ご覧にな(?:りまし(?!たら)|った(?!ら|上|うえ))|閲覧(?:履歴|した)"
)
# 1通目(再訪の挨拶)でだけ行動履歴への言及に数える言い方。現在形(「見ていますか?」)と「探していましたか?」は、
# 2通目以降では画面の求人(「今ご覧になっている求人」)や今回の希望(「渋谷で探していましたね」)を指すことが多い
RECENT_ACTIVITY_T1 = re.compile(
    RECENT_ACTIVITY.pattern
    + r"|(?:見て|ご覧になって|閲覧して|(?:閲覧|チェック)されて)い(?:ます|る)"
    + r"|探して(?:い(?:た|まし|らっしゃ)|た|まし)|探され(?:てい)?(?:た|まし)|お探し(?:でし|だっ)"
    + r"|気になってい(?:た|まし)|見てくださってい(?:た|まし)"
)
# 相談モードの入口: 選択肢「まず相談する」か、探し方そのものに迷っているサイン
CONSULT_SIGN = re.compile(r"相談|何から始め|何をすれば|探し方が分から|(合う|向いてる|向いている)(の|仕事|バイト)?が?分から")
# 「求人を見せて」: 相談の途中でも、このターンで提案に切り替える
SHOW_JOBS = re.compile(r"求人を?(見せて|見たい|出して|見る)")


# 本人が相談を切り上げた・会話を終えた発言(これより前に相談を締めるのは打ち切り)
CONSULT_CUTOFF = re.compile(r"今はいい|送らないで|やめ|あとで|いらない|また今度|ありがとう|今日はこれで|考えます")
# 保存済みの項目をもう一度聞いている質問(同じことを聞き直さない。再訪で前の希望を確認した後は、差分だけを受け取る)
REASK = {
    "experience": re.compile(r"経験(は|が)?(あります|ありますか|を教えて)|これまで(に)?(どんな|何)"),
    "area": re.compile(r"(どの|どこの?)(エリア|駅|辺り?|あたり|地域)|どこで働|エリアは|勤務地"),
    "time_band": re.compile(r"(どの|何の)時間帯|時間帯(は|が)|何時(から|ごろ|頃|まで)"),
    "job_type": re.compile(r"(どんな|どの)(お?仕事|職種)|職種は"),
    "days_per_week": re.compile(r"週(に)?(何|いく)日"),
    "min_wage": re.compile(r"時給(は|の)?(いくら|どのくらい|下限)"),
    # 相談で分かった本人のこと(aica v2 の D)。白紙で聞く問いだけを数える(掘り下げは PROFILE_FOLLOW_UP で外す)
    "purpose": re.compile(r"(なぜ|どうして|何のために)(今|いま)?、?(お?仕事|バイト|アルバイト)?を?(探|転職|働き)"
                          r"|(お?仕事|バイト|アルバイト|転職)(探し)?の(目的|理由)|探(す|している|そうと思った)(目的|理由)"
                          r"|目的(は|を|って)"),
    "strengths": re.compile(r"強み(は|を|って|として)"),
    "wants": re.compile(r"(やりたい|やってみたい|挑戦したい|してみたい)(こと|お?仕事)|興味(の|が)ある(こと|お?仕事)"),
    "avoids": re.compile(r"(避けたい|やりたくない|したくない|苦手な)(こと|お?仕事|作業)"),
    "concerns": re.compile(r"(不安|心配)(な|に思う|に感じる)?(こと|点)|(不安|心配)(は|が)(あり|ござい)"
                           r"|気になる(こと|点)(は|が|って)"),
    # 正社員を探すときの条件
    "min_annual_salary": re.compile(r"年収(は|の)?(いくら|どの(くらい|ぐらい)|どれ(くらい|ぐらい)|下限|希望|ご希望|目安)"
                                    r"|(希望|ご希望)の?年収"),
    "work_style": re.compile(r"(どんな|どのような|希望の|ご希望の)働き方|働き方(は|の(ご?希望)|について|に(ご?希望|こだわり))"
                             r"|リモート(は|を)?(ご?希望)"),
    "start_timing": re.compile(r"いつ(から|ごろ|頃)(から)?(働|入社|転職|始め|勤務)|いつまでに(転職|入社|働)"
                               r"|(入社|転職|勤務開始|開始|働き始め)(の|る)?(時期|タイミング)"),
}
# 相談で分かった本人のこと(目的・強み・やりたいこと・避けたいこと・不安)は、保存した後に掘り下げてよい
# (playbook の決まり: 目的とやりたいことは1回は掘り下げる)。掘り下げ・追加を求める問いは聞き直しに数えない
PROFILE_KEYS = ("purpose", "strengths", "wants", "avoids", "concerns")
# 提案した求人や特定の条件・面接・応募について聞く問い(「この求人について不安な点はありますか?」
# 「シフトについて気になることはありますか?」)は、保存した不安の聞き直しではない
REASK_TARGETED = {"concerns": re.compile(
    r"(この|こちら|その|あの|ご紹介した|ご提案した|お送りした|お見せした)"
    r"|(シフト|時給|給与|給料|勤務地|勤務時間|仕事内容|待遇|通勤|休み)(について|で|に|は|の)"
    r"|求人|面接|応募|職場")}
PROFILE_FOLLOW_UP = re.compile(r"具体的|もう少し|詳しく|例えば|たとえば|どんな場面|どういう(こと|意味|点|ところ)"
                               r"|それは(なぜ|どうして)|なぜそう|どうしてそう|そう(思|感じ)|他に|ほかに")
BRANCH_TOOLS = {"recommend_jobs", "answer_job_question", "get_application_status", "resume_application_form",
                "relax_conditions", "save_job"}


def asks_for_jobs(m: str) -> bool:
    """「求人を見せて」の依頼か。後ろに断りが続く「求人を見るのはあとでいいです」は依頼ではない。"""
    hit = SHOW_JOBS.search(m)
    return bool(hit) and not REFUSAL.search(m, hit.end())


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


def _job_names() -> set[str]:
    """求人一覧にある求人名・店名(短すぎて誤検出するものは除く)。"""
    return {n for j in load_jobs().values() for n in (j.title, j.employer, j.title.split("｜")[0].strip())
            if len(n) >= 6}


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
    names = _job_names()
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


# 本人の言葉を短い文で保存する項目(aica v2 の D)。語の完全一致ではなく、内容語の大半が発言にあるかで見る
FREE_TEXT_KEYS = PROFILE_KEYS + ("work_style", "start_timing")
# 保存する文を整えるときに足されやすい、中身の無い語(「人と話す仕事」の「仕事」)
_FILLER_WORDS = {"仕事", "こと", "職場", "環境", "希望"}


def _content_words(text: str) -> list[str]:
    """漢字・カタカナ2文字以上の内容語。無ければ漢字1文字ずつ(「人と話すこと」の「人」「話」)。"""
    runs = [w for w in re.findall(r"[一-龥々]+|[ァ-ヴー]+", text) if w not in _FILLER_WORDS]
    return [w for w in runs if len(w) >= 2] or [c for w in runs for c in w if re.match(r"[一-龥]", c)]


def _free_text_ok(v: str, said: str) -> bool:
    """助詞や言い回しの違い(「未経験でもついていけるかが不安です」→「未経験でついていけるか」)は作り話ではない。"""
    words = _content_words(v)
    return not words or sum(w in said for w in words) * 2 > len(words)


def preference_violations(turns: list[dict], user_msgs: list[str]) -> list[str]:
    """保存した希望が、本人の発言か、前に保存済みの希望(要約の stated_preferences)に基づくか。
    数字(時給・日数)は数字で、自由記述の項目(FREE_TEXT_KEYS)は内容語の大半で、それ以外は
    「渋谷と原宿」のような並びを区切って語ごとに照合する。"""
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
                if nums:
                    ok = nums <= said_nums
                elif k in FREE_TEXT_KEYS:
                    ok = _free_text_ok(v, said)
                else:
                    ok = all(w in said for w in words)
                if not ok:
                    out.append(f"T{i + 1}: 本人が言っていない希望を保存した: {k}={v}")
    return out


def _question_part(b: str) -> str:
    """吹き出しのうち質問の文だけ(疑問符を含む文)。保存済みの値を復唱するまとめの平叙文は聞き直しではない。"""
    return "".join(s for s in re.split(r"(?<=[。!!\n])", b) if "?" in s or "？" in s)


# 本人の発言から取る「今回の値」の候補から外す、項目名そのものの語
_KEY_WORDS = {"エリア", "時間帯", "時間", "職種", "勤務地", "仕事", "求人", "希望", "条件"}
# 疑問詞。これを含む問い(「どのエリアで探しますか?」)は白紙の問いで、値を示して確かめる形ではない。
# 「どう」は値を示して尋ねる形(「渋谷と原宿でどうですか?」)にしか付かないので含めない。「何か」「いつも」「どちらも」も疑問詞ではない
WH_WORD = re.compile(r"どの|どこ|どんな|何(?!か)|いつ(?!も)|どれ|どちら(?!も)")


def reask_violations(turns: list[dict], user_msgs: list[str | None] = (), seeded: dict | None = None) -> list[str]:
    """保存済みの項目を、後のターンでもう一度聞いていないか(相談で話したことを提案の前に聞き直さない)。
    seeded は会話の前から保存されている希望(再訪): 1通目で確認した後は、差分だけを受け取る。
    疑問詞の無い確認の問いで、項目を指す語の後ろに既知の値(保存した値か、そのターンの本人の発言にある語)があれば、
    聞き直しではない。値が前にあるだけ(「接客のお仕事で、勤務地の希望はありますか?」)は、前の答えを添えた次の問い
    (「原宿も入れて」→「エリアは渋谷と原宿でよいですか?」)。値を変えて保存し直した項目は、そのターンは聞いてよい
    (同じ値の保存し直しは数えない)。求人を提案したターン(recommend_jobs・カード)以降は、不安(concerns)の問いを
    提案への問いとみなして数えない。相談で分かった本人のこと(PROFILE_KEYS)への掘り下げ・追加の問い
    (「それはなぜですか?」「具体的にはどんな場面ですか?」「他に不安なことはありますか?」)は聞き直しではない。"""
    saved = {k: 0 for k in seeded or {}}
    known = {k: [str(v)] for k, v in (seeded or {}).items()}
    out = []
    proposed = False
    for i, r in enumerate(turns):
        said = {w for w in re.findall(r"[一-龥ァ-ヴー]{2,}", (user_msgs[i] if i < len(user_msgs) else None) or "")
                if w not in _KEY_WORDS}
        # 求人を提案した後に迷っている点を聞くのは、提案への問い(「3件ご紹介しました。気になる点はありますか?」)
        proposed = proposed or bool(r["cards"]) or any(t["name"] == "recommend_jobs" for t in r["tools"])
        for t in r["tools"]:
            if t["name"] == "save_user_preferences":
                for k, v in (t["args"].get("preferences") or {}).items():
                    if str(v) in known.get(k, []):
                        saved.setdefault(k, i)
                    else:
                        saved[k] = i
                        known.setdefault(k, []).append(str(v))
        for b in r["bubbles"]:
            if not _questions([b]):
                continue
            q = _question_part(b)
            for k, at in saved.items():
                m = REASK[k].search(q) if at < i and k in REASK else None
                if m and k in PROFILE_KEYS and PROFILE_FOLLOW_UP.search(q):
                    continue
                if m and k in REASK_TARGETED and (proposed or REASK_TARGETED[k].search(q)):
                    continue
                if m and not (not WH_WORD.search(q) and any(w in q[m.start():] for w in [*known.get(k, []), *said])):
                    out.append(f"T{i + 1}: 保存済みの「{k}」を聞き直した: {b}")
    return out


def recent_activity_violations(turns: list[dict]) -> list[str]:
    """再訪(行動履歴のみ): 1通目で最近の行動に触れる。触れるのは会話を通して1回だけで、求人を羅列しない
    (1通目に求人カード・求人名・件数を出さず、職種も1つだけ)。"""
    if not turns:
        return []
    first = "\n".join(turns[0]["bubbles"])
    out = []
    if not RECENT_ACTIVITY_T1.search(first):
        out.append("T1: 再訪の挨拶で最近の行動に触れていない")
    # 1通目は1つの挨拶なので、言及と追いかけの問い(「見ていましたね。今も探していらっしゃいますか?」)で1回と数える
    mentions = int(bool(RECENT_ACTIVITY_T1.search(first))) + sum(
        len(RECENT_ACTIVITY.findall(b)) for r in turns[1:] for b in r["bubbles"])
    if mentions > 1:
        out.append(f"最近の行動に{mentions}回触れた(1回だけ)")
    job_types = {j.job_type for j in load_jobs().values()}
    listed = [n for n in sorted(_job_names()) if n in first] + re.findall(r"\d+\s*件", first)
    if turns[0]["cards"] or listed or sum(t in first for t in job_types) > 1:
        out.append(f"T1: 閲覧した求人を羅列した: {listed or [c['job_id'] for c in turns[0]['cards']] or first}")
    return out


def consult_violations(sc: dict, turns: list[dict], user_msgs: list[str]) -> list[str]:
    """相談モード(sc["consult_mode"])の判定。入口では求人を出さずに1問聞き、本人が切り上げる前に相談を締めない。"""
    out = []
    first = turns[0] if turns else None
    if sc.get("expect_consult_menu") and first and not any("相談" in q for q in first["quick_replies"]):
        out.append(f"T1: 初回の選択肢に「まず相談する」が無い: {first['quick_replies']}")
    entry = next((i for i, m in enumerate(user_msgs[:len(turns)]) if m and CONSULT_SIGN.search(m)), None)
    if entry is None:
        return out
    r = turns[entry]
    if r["cards"] or any(t["name"] == "recommend_jobs" for t in r["tools"]) or not _questions(r["bubbles"]):
        out.append(f"T{entry + 1}: 相談の入口で、ヒアリングの1問から始めていない")
    for i in range(entry, len(turns)):
        m = user_msgs[i] or ""
        if i > entry and (CONSULT_CUTOFF.search(m) or asks_for_jobs(m)):
            break
        names = [t["name"] for t in turns[i]["tools"]]
        if "record_conversation_outcome" in names and not turns[i]["cards"] and "recommend_jobs" not in names:
            out.append(f"T{i + 1}: 本人が切り上げる前に相談を締めた")
            break
    for i in range(entry, min(len(turns), len(user_msgs))):  # 相談の途中でも「求人を見せて」には、そのターンで求人を出す
        m = user_msgs[i] or ""
        if m and asks_for_jobs(m) and not (turns[i]["cards"] or {"recommend_jobs", "relax_conditions"}
                                           & {t["name"] for t in turns[i]["tools"]}):
            out.append(f"T{i + 1}: 「求人を見せて」の後に求人を探さなかった")
    saved = {k for r in turns for t in r["tools"] if t["name"] == "save_user_preferences"
             for k in (t["args"].get("preferences") or {})}
    out += [f"相談で聞いた「{k}」を保存していない" for k in sc.get("expect_saved_preferences") or [] if k not in saved]
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
    if first and not (_questions(first["bubbles"]) or first["quick_replies"]):
        violations.append("T1: 初回が質問で終わっていない")
    if "wait" in outcomes and not any(REFUSAL.search(m or "") for m in user_msgs):
        violations.append("断られていないのに wait を記録した(終わりの挨拶を拒否と取り違えている)")
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
    if sc.get("expect_recent_activity"):
        violations += recent_activity_violations(turns)
    if sc.get("pii_test"):
        pii_turn = next((i for i, m in enumerate(user_msgs) if PII_VALUE.search(m or "")), None)
        if pii_turn is not None and pii_turn < len(turns) and not turns[pii_turn]["buttons"]:
            violations.append(f"T{pii_turn + 1}: 個人情報を書かれた後に入力画面を案内していない")
    violations += reask_violations(turns, user_msgs,
                                   sc.get("seed_preferences") if sc.get("expect_uses_preferences") else None)
    if sc.get("consult_mode"):
        violations += consult_violations(sc, turns, user_msgs)
    for i, m in enumerate(user_msgs):  # 「今はいい」「送らないで」の直後は、質問や求人を続けずに短く終える
        if m and QUICK_END.fullmatch(m.strip()) and i < len(turns) and (_questions(turns[i]["bubbles"]) or turns[i]["cards"]):
            violations.append(f"T{i + 1}: 「今はいい/送らないで」の後に質問や求人を続けた")
    return {
        "turns": len(turns),
        "branch_turn": branch_turn,
        # 相談モードのシナリオは「3ターン以内に分岐」を緩める(ヒアリングが終わるか本人が切り上げるまで続けてよい)ので測らない
        "branch_within_3": None if sc.get("consult_mode") else branch_turn is not None and branch_turn <= 3,
        "outcomes": outcomes,
        "outcome_recorded": bool(outcomes),
        "outcome_as_expected": bool(set(outcomes) & set(sc["expect_outcome"])),
        "violations": violations + fabrications,
        "fabrications": fabrications,
    }
