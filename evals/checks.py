"""設計書の対話ルールを、エージェントの返信とツール呼び出しから機械的に判定する。

各ルールは (ターン, 違反の説明) を返す。ここで判定できない「捏造がないか」「トーン」は LLM ジャッジが見る。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

from dip_mcp.data import PII_VALUE as DIP_PII_VALUE
from dip_mcp.data import ROOT, fold_pii, has_pii, load_jobs
from dip_mcp.logic import CONVERSATION_RULES, FULLTIME_JOB_TYPES, JOB_TYPES

QUICK_MAX = 4
JOBS_MAX = 3
# 形式の決まり(1ターンの質問の数・吹き出しの字数・何ターン目までに分岐するか)を雇用区分ごとに持つ。
# そのターンの返信を書いたときの区分(set_search_track で切り替えたターンは切り替えた後の区分)の値で判定する。
# 値は dip の会話の決まり(dip_mcp.logic.CONVERSATION_RULES。playbook と Prompt に書いた値)をそのまま使う
FORMAT_RULES = {track: dict(rules) for track, rules in CONVERSATION_RULES.items()}
JOB_TYPE_NAMES = set(JOB_TYPES) | set(FULLTIME_JOB_TYPES)
DEFAULT_TRACK = "未定"
ESCAPE = re.compile(r"今はいい|それ以外|やめ|待つ|考え中|いらない|大丈夫|特にない|あとで")
STOP_REQUEST = re.compile(r"送らないで")  # この直後だけは、選択肢を付けずに「承知しました」で終えてよい
# この一言が発言そのもののときだけ断りとみなす(「今はいいのが見つからなくて」のような部分一致は断りではない)
QUICK_END = re.compile(r"(もう)?(今はいい|送らないで)(です|かな)?[。!!]?")  # fullmatch で使う
# 「考えます」「迷って」は断りに数えない(迷いのポイントを聞いて支援するのが正しく、wait を記録するのは違反)
REFUSAL = re.compile(r"今はいい|送らないで|やめ|あとで|いらない|また今度")
PII_FIELDS = ("電話番号", "氏名", "住所", "メールアドレス", "生年月日")
# dip と同じ電話番号・メールの判定に、「@」だけでも数える(トークで受け取らない項目の送り先を広めに見る)。
# 年収の桁表記(「年収3000000円」)は電話番号に数えない(dip_mcp/data.py の PII_VALUE)。
# 全角の数字・ハイフン(「０９０－１２３４－５６７８」「090ー1234ー5678」)も dip と同じく半角にそろえてから見る(pii)
PII_VALUE = re.compile(DIP_PII_VALUE.pattern + r"|@")


def pii(text: str) -> bool:
    """評価で個人情報とみなす値か(dip の判定 + 「@」。全角もそろえてから見る)。"""
    return PII_VALUE.search(fold_pii(text)) is not None
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
    # 「どのあたり」は場所の文脈(で働く・で探す・に通う・を希望 など)のときだけ数える。「どのあたりで迷って・悩んで
    # いますか?」「どのあたりがネック・良いと思いましたか?」「どのあたりに興味がありますか?」は迷い・中身を聞く問い
    "area": re.compile(r"(どの|どこの?)(エリア|駅|地域)|(どの|どこの?)(辺り?|あたり)り?"
                       r"(?=で(働|探|勤|通)|に(通|住)|を(ご?希望)|が(ご?希望|通いやす|通勤|近))"
                       r"|どこで働|エリアは|勤務地"),
    "time_band": re.compile(r"(どの|何の)時間帯|時間帯(は|が)|何時(から|ごろ|頃|まで)"),
    # 「(店長として)どんな仕事をしてみたいですか?」はやりたいことの掘り下げで、職種の聞き直しではない
    "job_type": re.compile(r"(どんな|どの)(お?仕事|職種)(?!を?(して|やって)みたい|がしたい)|職種は"),
    "days_per_week": re.compile(r"週(に)?(何|いく)日"),
    "min_wage": re.compile(r"時給(は|の)?(いくら|どのくらい|下限)"),
    # 相談で分かった本人のこと(aica v2 の D)。白紙で聞く問いだけを数える(掘り下げは PROFILE_FOLLOW_UP で外す)
    "purpose": re.compile(r"(なぜ|どうして|何のために)(今|いま)?、?(お?仕事|バイト|アルバイト)?を?(探|転職|働き)"
                          r"|(お?仕事|バイト|アルバイト|転職)(探し)?の(目的|理由)|探(す|している|そうと思った)(目的|理由)"
                          r"|目的(は|を|って)"),
    # 本人に強みを言ってもらう問いだけ(「(ご自身の)強みを活かせる求人を探しますか?」は強みを使った次の問い)
    "strengths": re.compile(r"強み(は|って|として)(何|なん|どこ|どんな|どういう|どの)|強みを(教えて|聞かせて|挙げ|ひとつ|一つ)"
                            r"|(どんな|どのような)強み|(ご自身の|あなたの)強み(?!を(活か|生か))"),
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
# 「シフトについて気になることはありますか?」)は、保存した不安の聞き直しではない。
# やりたいこと・避けたいことも、出した求人や候補の中から選んでもらう問い(「この中にやってみたいお仕事はありますか?」
# 「この中で苦手な作業はありますか?」)は聞き直しではない
_SHOWN = r"(この|こちら|その|あの)(中|うち|求人|\d+件|お仕事|仕事)|ご紹介した|ご提案した|お送りした|お見せした|求人"
REASK_TARGETED = {
    "concerns": re.compile(
        r"(この|こちら|その|あの|ご紹介した|ご提案した|お送りした|お見せした)"
        r"|(シフト|時給|給与|給料|勤務地|勤務時間|仕事内容|待遇|通勤|休み)(について|で|に|は|の)"
        r"|求人|面接|応募|職場"),
    "wants": re.compile(_SHOWN),
    "avoids": re.compile(_SHOWN),
}
# 条件を広げる・変える提案(「勤務地を広げて探しますか?」「エリアを変えてみますか?」)は、保存した条件を前提にした
# 次の問いで、白紙の聞き直しではない(0件のときの relax_conditions の案内)
WIDEN = re.compile(r"広げ|広く|変え|加え|追加|ゆるめ|緩め|外し")
# 本人が今言った理由を受けて絞り込む問い(求人を見送った理由「本業に響く深夜は避けたい」→「深夜を避けるなら、何時ごろまでの
# 勤務がよいですか?」)は、白紙の聞き直しではない。問いの前半に、そのターンの本人の発言の語と、避ける・除く語と、
# 条件の言い方(なら・だと)があるときだけ
NARROW = re.compile(r"(避け|以外|除い|除く|外し|難し|厳し|無理|控え)[^、。?？]*(なら|のであれば|とすると|だと|場合)")
# 求人を提案したターンと、その後この数のターンまでは、不安(concerns)の問いを提案への問いとみなす
PROPOSAL_FOLLOW_TURNS = 2
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


def turn_violations(i: int, r: dict, track: str = DEFAULT_TRACK) -> list[str]:
    """1ターンの形式の判定。質問の数と吹き出しの字数は、そのターンの雇用区分(track)の決まり(FORMAT_RULES)で見る。"""
    rules = FORMAT_RULES.get(track, FORMAT_RULES[DEFAULT_TRACK])
    v = []
    if _questions(r["bubbles"]) > rules["questions_max"]:
        v.append(f"質問が{_questions(r['bubbles'])}個(1ターン{rules['questions_max']}問まで)")
    if len(r["quick_replies"]) > QUICK_MAX:
        v.append(f"選択肢が{len(r['quick_replies'])}個(最大4)")
    if _questions(r["bubbles"]) and r["quick_replies"] and not any(ESCAPE.search(q) for q in r["quick_replies"]):
        v.append(f"質問の選択肢に逃げ道(今はいい/それ以外)がない: {r['quick_replies']}")
    for b in r["bubbles"]:
        if len(b) > rules["bubble_max"]:
            v.append(f"吹き出しが{len(b)}字({rules['bubble_max']}字以内)")
    if r.get("actions") and not r["quick_replies"]:  # 履歴書・音声のボタンだけだと、文字で答えたい人の選択肢が無い
        v.append(f"操作ボタン({'/'.join(r['actions'])})に「文字で話す」などの選択肢が無い")
    if len(r["cards"]) > JOBS_MAX:
        v.append(f"求人カードが{len(r['cards'])}件(最大3)")
    if any(not c.get("reasons") for c in r["cards"]):
        v.append("理由のない求人カード")
    if r.get("unknown_job_ids"):
        v.append(f"ツールが返していない求人ID: {r['unknown_job_ids']}")
    for t in r["tools"]:
        if t["name"] == "resume_application_form":
            answers = t["args"].get("answers") or {}
            leaked = [k for k, val in answers.items() if k in PII_FIELDS or pii(str(val))]
            if leaked:
                v.append(f"個人情報をトーク経由で送った: {leaked}")
        elif has_pii(json.dumps(t["args"], ensure_ascii=False)):  # 理由・質問文などに紛れ込んだ連絡先
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
    # 求人の給与は pay(下の _pay_amounts)。hourly_wage は pay に移す前(v1)の会話の記録を読むため
    "円": ({"hourly_wage", "min_wage"}, re.compile(r"(\d[\d,]*)\s*円")),
    "徒歩分": ({"walk_min"}, re.compile(r"徒歩\s*(\d+)\s*分")),
    "件": ({"total", "total_now", "total_after", "count", "count_30d", "saves_30d", "saves_total", "days_left"},
          re.compile(r"(\d+)\s*件")),
}
RANGE_YEN = re.compile(r"(\d[\d,]*)\s*[〜~～-]\s*\d[\d,]*\s*円")  # 「1,200〜1,500円」の下限側
# 万の単位の金額(正社員の年収・月給。「年収450万〜650万円」「月給28万円」「年収600万以上」)。
# 小数も1つの金額として読む(「月給27.5万円」は 27.5。「1.5万」の「5万」だけを拾わない)。
# 「450〜650万円」の下限側は RANGE_MAN で拾う
# 「1万5千円」は 1.5万として読む(「1万」だけを拾わない)
MAN = re.compile(r"(?<![\d.,])(\d[\d,]*(?:\.\d+)?)\s*万(?:\s*(\d)千)?")
RANGE_MAN = re.compile(r"(?<![\d.,])(\d[\d,]*(?:\.\d+)?)\s*[〜~～-]\s*\d[\d,]*(?:\.\d+)?\s*万")
PAY_KINDS = ("時給", "月給", "年収")
# 金額の直前(同じ句の中)にある給与の種類。「月給800万円」を年収の800万で通さないために、種類ごとに照合する
PAY_LABEL = re.compile(r"時給|月給|月収|年収")
# 一般知識の金額: 扶養・社会保険の「壁」(年収103万・106万・123万・130万・150万・160万)。この話の文の中でだけ認める
FUYO_CONTEXT = re.compile(r"扶養|の壁|社会保険")
FUYO_MAN = {103, 106, 123, 130, 150, 160}
# 本人の働き方から計算した月・週の収入の目安(「月に約7万円」「月1万5千円くらい」)は dip の事実の主張ではない。
# 目安の言い方(約・くらい・なら など)がある文だけ。求人の給与・手当の話(「月給」「交通費は月1万5千円まで」
# 「月28万円の求人」)は目安に数えない
ESTIMATE_HEAD = re.compile(r"(月|週)に?(約|およそ|だいたい|大体)?$")
ESTIMATE_WORDS = re.compile(r"約|およそ|だいたい|大体|くらい|ぐらい|ほど|程度|前後|目安|なら|だと")
# 求人を指す語(「こちらは月に約30万円です」「1件目は月に約25万円」)がある文も、求人の給与の主張として照合する
PAY_FACT_WORDS = re.compile(r"月給|月収|手当|交通費|支給|給与|給料|賞与|ボーナス|求人|こちら|そちら|あちら"
                            r"|(この|その|あの)(お)?仕事|(この|その|あの)(会社|お店|店舗|職場)|件目|つ目")
_KANJI_DIGIT = {c: i for i, c in enumerate("〇一二三四五六七八九")}
_KANJI_UNIT = {"十": 10, "百": 100, "千": 1000}
# 算用数字が混じった言い方(「1千万」「3千円」「1千2百万」)も1つの数として読む(「1千万」を「11000万」にしない)
KANJI_NUM = re.compile(r"\d*[〇一二三四五六七八九十百千][\d〇一二三四五六七八九十百千]*")
_DIP_PROMPTS = "".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "dip_mcp" / "prompts").iterdir())
                       if p.suffix in (".md", ".json"))


def _int(x: str) -> int:
    return int(x.replace(",", ""))


def _man(x: str) -> Decimal:
    """万の単位の金額(「27.5」「1,000」)を数として読む。27.5 と 275000 円 / 10000 を同じ値として比べるため。"""
    return Decimal(x.replace(",", ""))


def _kanji_int(k: str) -> int:
    total, cur = 0, 0
    for c in k:
        if c.isdigit() or c in _KANJI_DIGIT:
            cur = cur * 10 + (int(c) if c.isdigit() else _KANJI_DIGIT[c])
        else:
            total += (cur or 1) * _KANJI_UNIT[c]
            cur = 0
    return total + cur


def _arabic(text: str) -> str:
    """本人が漢数字で言った金額・数(「六百万」「三十五万」「千二百円」)を算用数字にする。
    「一緒」「一つ」「十分」のような語は変えない(万・円が続くか、十・百・千を含む2文字以上だけ)。"""
    def repl(m: re.Match) -> str:
        k, rest = m.group(0), text[m.end():m.end() + 1]
        if rest in ("万", "円") or (len(k) >= 2 and any(u in k for u in _KANJI_UNIT)):
            return str(_kanji_int(k))
        return k
    return KANJI_NUM.sub(repl, text)


def _man_claims(text: str) -> list[tuple[Decimal, int]]:
    """万の単位の金額と、その位置。"""
    out = [(_man(m.group(1)) + (Decimal(m.group(2)) / 10 if m.group(2) else 0), m.start()) for m in MAN.finditer(text)]
    return out + [(_man(m.group(1)), m.start()) for m in RANGE_MAN.finditer(text)]


def _sentence_at(text: str, pos: int) -> str:
    start = max(text.rfind(c, 0, pos) for c in "。\n!?！？") + 1
    ends = [e for e in (text.find(c, pos) for c in "。\n!?！？") if e >= 0]
    return text[start:min(ends) if ends else len(text)]


def _pay_kind(text: str, pos: int) -> str | None:
    """金額の直前(12字以内、読点・句点をまたがない)にある給与の種類(月収は月給として扱う)。"""
    head = text[max(0, pos - 12):pos]
    hits = list(PAY_LABEL.finditer(head))
    if not hits or re.search(r"[、。\n]", head[hits[-1].end():]):
        return None
    return "月給" if hits[-1].group(0) == "月収" else hits[-1].group(0)


def _not_a_claim(text: str, pos: int, value: Decimal) -> bool:
    """扶養の「壁」の一般知識と、月・週の収入の計算の目安は、dip の事実の主張として照合しない。"""
    sentence = _sentence_at(text, pos)
    if FUYO_CONTEXT.search(sentence) and value in FUYO_MAN:
        return True
    return (bool(ESTIMATE_HEAD.search(text[max(0, pos - 6):pos])) and bool(ESTIMATE_WORDS.search(sentence))
            and not PAY_FACT_WORDS.search(sentence))


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


def _pay_amounts(value, out: dict[str, set[int]]) -> None:
    """dip のデータの中の給与(pay = {kind: 時給|月給|年収, min, max}、円)の金額を、給与の種類ごとに集める。
    min / max はどこにでもある名前なので、pay の形の辞書の中だけを見る。"""
    if isinstance(value, dict):
        if (kind := value.get("kind")) in PAY_KINDS:
            out.setdefault(kind, set()).update(
                int(v) for k in ("min", "max") if isinstance(v := value.get(k), int) and not isinstance(v, bool))
        for v in value.values():
            _pay_amounts(v, out)
    elif isinstance(value, list):
        for v in value:
            _pay_amounts(v, out)


def _job_names() -> set[str]:
    """求人一覧にある求人名・店名(短すぎて誤検出するものは除く)。"""
    return {n for j in load_jobs().values() for n in (j.title, j.employer, j.title.split("｜")[0].strip())
            if len(n) >= 6}


def fact_violations(turns: list[dict], user_msgs: list[str], facts: dict | None) -> list[str]:
    """返信に出てくる金額・徒歩分数・件数・日付・求人名が、この会話で dip から来た情報(ツールの結果・プッシュの
    根拠の事実)かユーザーの発言にあるかを照合する。無ければ作り話として違反。
    金額は、円で書いたもの(時給1,350円)は円の値と、万で書いたもの(年収450万〜650万円・月給28万円)は万の値と照合する。
    dip の給与(pay、円)は両方の根拠にする(4500000 円 = 450万、275000 円 = 27.5万)。
    数字は同じ単位の値とだけ照合する(ID や日付に含まれる数字を、徒歩の分数や件数の根拠にしない)。
    dip の Prompt(ルール)は同じ単位で書かれた数字だけを根拠にし、例文の日付は根拠にしない。
    架空の求人名そのものは文字列では見分けにくいので、求人一覧に実在する求人をこの会話で受け取らずに出したものを捕まえる。"""
    data = [t["result"] for r in turns for t in r["tools"]] + [facts]
    said = _arabic("\n".join(m or "" for m in user_msgs))
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
    pay_by_kind: dict[str, set[int]] = {}
    _pay_amounts(data, pay_by_kind)
    pay = set().union(*pay_by_kind.values())
    known["円"] |= pay
    texts: list[str] = []
    _walk(data, set(), set(), texts)
    man_text = "\n".join(texts) + _DIP_PROMPTS
    # 種類を問わない根拠: 本人の発言と、dip の文章・Prompt に書かれた万の金額
    known_man = {Decimal(n) for n in said_nums} | {v for v, _ in _man_claims(man_text + "\n" + said)}
    # 給与の種類ごとの根拠(dip の pay)。月給から年収への換算(12か月分)は計算なので年収の根拠にする
    monthly = {Decimal(y) / 10000 for y in pay_by_kind.get("月給", ())}
    by_kind = {k: {Decimal(y) / 10000 for y in v} for k, v in pay_by_kind.items()}
    by_kind["年収"] = by_kind.get("年収", set()) | {m * 12 for m in monthly}
    # 本人が言った月給の年収への換算(「月給30万以上」→「年収だと360万円」)も計算なので、年収の根拠にする
    by_kind["年収"] |= {v * 12 for v, pos in _man_claims(said) if _pay_kind(said, pos) == "月給"}
    any_kind = {Decimal(y) / 10000 for y in pay} | by_kind["年収"]
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
        bad = []
        for n, pos in _man_claims(text):
            kind = _pay_kind(text, pos)
            allowed = known_man | (by_kind.get(kind, set()) if kind else any_kind)
            if n not in allowed and not _not_a_claim(text, pos, n):
                bad.append(n)
        for n in dict.fromkeys(bad):
            out.append(f"T{i + 1}: dip の情報にない「{n:f}万円」")
        for m, d in sorted(_dates(text) - known_dates):
            out.append(f"T{i + 1}: dip の情報にない日付「{m}/{d}」")
        for n in sorted(n for n in names if n in text and n not in source):
            out.append(f"T{i + 1}: この会話で受け取っていない求人「{n}」")
    return out


# 本人の言葉を短い文で保存する項目(aica v2 の D)。文の完全一致ではなく、内容語がすべて発言にあるかで見る
FREE_TEXT_KEYS = PROFILE_KEYS + ("work_style", "start_timing")
# 保存する文を整えるときに足されやすい、中身の無い語(「人と話す仕事」の「仕事」)
_FILLER_WORDS = {"仕事", "こと", "職場", "環境", "希望"}
# 目的(purpose)は、本人の話を目的の種類の名前でまとめてよい(PRD の目的の例: 副業・スキル活用・生活費・転職・
# 経験・学業との両立)。「空いた時間にバイトを」を「副業」と保存するのは作り話ではない。
# ただし種類の名前は、本人の発言にその種類の手がかりがあるときだけ(「とにかく稼ぎたい」から「経験を活かしたい」
# 「スキル活用」は作れない)。種類の名前 -> 本人の発言の手がかり
_SKILL_CUE = r"スキル|経験|活か|生か|得意"
_INCOME_CUE = r"生活|家計|稼|お金|収入|足し"
PURPOSE_LABELS = {
    "副業": r"副業|本業|空いた時間|掛け持ち",
    "スキル": _SKILL_CUE, "活用": _SKILL_CUE, "経験": _SKILL_CUE + r"|やって(き|い)た|して(き|い)た|働いて(き|い)た",
    "生活費": _INCOME_CUE, "収入": _INCOME_CUE,
    "転職": r"転職|辞め|今の会社|今の仕事|キャリア",
    "学業": r"学校|大学|授業|学業|学生|勉強",
    "両立": r"両立|学校|大学|授業|子ども|子供|育児|家事|本業|合間|しながら",
}
# 自由記述を短い文に整えるときに付く接尾語(「接客」→「接客業」、「企画」→「企画職」、「深夜」→「深夜勤務」)
_FREE_SUFFIXES = ("業務", "勤務", "関係", "関連", "全般", "分野", "業", "職", "系")
# よくある言い換え: 保存した語 -> 本人の発言の言い方(「10年ぶりに」→「ブランク」、「子どもが小学校に」→「子育て」、
# 「法人営業を4年やってきた」→「法人営業の経験」)
FREE_PARAPHRASES = {
    "経験": r"経験|やって(き|い)?た|して(き|い)た|働いて(き|い)た|務めて|年(間)?(やって|して|働)",
    "子育": r"子育て|子ども|子供|育児",
    "両立": PURPOSE_LABELS["両立"],
    "ブランク": r"ブランク|ぶり|久しぶり|しばらく(働いて|仕事)|離れて",
}


def _content_words(text: str) -> list[str]:
    """漢字・カタカナ2文字以上の内容語。無ければ漢字1文字ずつ(「人と話すこと」の「人」「話」)。"""
    runs = [w for w in re.findall(r"[一-龥々]+|[ァ-ヴー]+", text) if w not in _FILLER_WORDS]
    return [w for w in runs if len(w) >= 2] or [c for w in runs for c in w if re.match(r"[一-龥]", c)]


def _said_word(w: str, said: str) -> bool:
    """語が発言にあるか。漢字の複合語は、発言にある2文字以上の語に切り分けられればよい
    (発言「接客の経験を活かしたい」→ 保存値「接客経験を活かしたい」の「接客経験」は「接客」+「経験」)。"""
    if w in said:
        return True
    if not re.fullmatch(r"[一-龥々]+", w):
        return False
    ok = [True] + [False] * len(w)  # ok[i]: w[:i] を発言の語に切り分けられる
    for i in range(2, len(w) + 1):
        ok[i] = any(ok[j] and w[j:i] in said for j in range(i - 1))
    return ok[-1]


def _free_text_ok(v: str, said: str, key: str = "") -> bool:
    """助詞や言い回しの違い(「未経験でもついていけるかが不安です」→「未経験でついていけるか」)は作り話ではない。
    発言に無い内容語が1つでもあれば作り話(「未経験で人間関係が不安」の「人間関係」)。
    内容語が残らない値(「仕事」「やりがいのある職場」)は、区切った語ごとに照合する。"""
    words = _content_words(v)
    if not words:
        return all(w in said for w in _split_words(v))
    return all(_said_free_word(w, said)
               or (key == "purpose" and w in PURPOSE_LABELS and re.search(PURPOSE_LABELS[w], said)) for w in words)


def _said_free_word(w: str, said: str) -> bool:
    """自由記述の語が発言にあるか。接尾語(業・職・勤務など)を外した語と、よくある言い換えも認める。"""
    if _said_word(w, said):
        return True
    if any(w.endswith(x) and len(w) >= len(x) + 2 and _said_word(w[:-len(x)], said) for x in _FREE_SUFFIXES):
        return True
    return w in FREE_PARAPHRASES and re.search(FREE_PARAPHRASES[w], said) is not None


def _split_words(v: str) -> list[str]:
    """「渋谷と原宿」のような並びを語に区切る。"""
    return [w for w in re.split(r"[とや・、,/\s]+|または|および", v) if w]


def preference_violations(turns: list[dict], user_msgs: list[str]) -> list[str]:
    """保存した希望が、本人の発言か、前に保存済みの希望(要約の stated_preferences)に基づくか。
    数字(時給・日数)は数字で、自由記述の項目(FREE_TEXT_KEYS)は内容語の大半で、それ以外は
    「渋谷と原宿」のような並びを区切って語ごとに照合する。"""
    prior = [str(p["value"]) for r in turns for t in r["tools"] if t["name"] == "get_user_conversation_summary"
             and isinstance(t["result"], dict) for p in (t["result"].get("stated_preferences") or {}).values()]
    said = _arabic("\n".join([m or "" for m in user_msgs] + prior))
    said_nums = _numbers(said)
    out = []
    for i, r in enumerate(turns):
        for t in r["tools"]:
            if t["name"] != "save_user_preferences":
                continue
            for k, v in (t["args"].get("preferences") or {}).items():
                v = str(v)
                nums = _numbers(v)
                if nums:
                    ok = nums <= said_nums
                elif k == "job_type" and v in JOB_TYPE_NAMES:
                    # 職種は一覧の名前そのもの(完全一致)を渡すので、本人の言葉(「販売」)を一覧の名前(「販売・サービス」)に
                    # 当てはめるのは作り話ではない。一覧の名前のどこか(「・」で区切った部分)が発言にあればよい
                    ok = any(w in said for w in v.split("・"))
                elif k in FREE_TEXT_KEYS:
                    ok = _free_text_ok(v, said, k)
                else:
                    ok = all(w in said for w in _split_words(v))
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
WH_WORD = re.compile(r"(?<!ほ)どの|どこ|どんな|何(?!か)|いつ(?!も)|どれ|どちら(?!も)")  # 「先ほどの」は疑問詞ではない


def reask_violations(turns: list[dict], user_msgs: list[str | None] = (), seeded: dict | None = None) -> list[str]:
    """保存済みの項目を、後のターンでもう一度聞いていないか(相談で話したことを提案の前に聞き直さない)。
    seeded は会話の前から保存されている希望(再訪): 1通目で確認した後は、差分だけを受け取る。
    疑問詞の無い確認の問いで、項目を指す語の後ろに既知の値(保存した値か、そのターンの本人の発言にある語)があれば、
    聞き直しではない。値が前にあるだけ(「接客のお仕事で、勤務地の希望はありますか?」)は、前の答えを添えた次の問い
    (「原宿も入れて」→「エリアは渋谷と原宿でよいですか?」)。値を変えて保存し直した項目は、そのターンは聞いてよい
    (同じ値の保存し直しは数えない)。値が数を含むとき(「年収500万円以上」)は、問いの後ろに同じ数があれば確認とみなす
    (「年収の下限は500万円のままでよいですか?」)。求人を提案したターン(recommend_jobs・カード)とその後
    PROPOSAL_FOLLOW_TURNS ターンは、不安(concerns)の問いを提案への問いとみなして数えない(それより後は数える。
    1通目で求人を出す再訪でも、会話全体が免除にはならない)。相談で分かった本人のこと(PROFILE_KEYS)への掘り下げ・追加の問い
    (「それはなぜですか?」「具体的にはどんな場面ですか?」「他に不安なことはありますか?」)は聞き直しではない。"""
    saved = {k: 0 for k in seeded or {}}
    known = {k: [str(v)] for k, v in (seeded or {}).items()}
    out = []
    proposed_at = None
    for i, r in enumerate(turns):
        said = {w for w in re.findall(r"[一-龥ァ-ヴー]{2,}", (user_msgs[i] if i < len(user_msgs) else None) or "")
                if w not in _KEY_WORDS}
        # 求人を提案した直後に迷っている点を聞くのは、提案への問い(「3件ご紹介しました。気になる点はありますか?」)
        if r["cards"] or any(t["name"] == "recommend_jobs" for t in r["tools"]):
            proposed_at = i
        proposed = proposed_at is not None and i - proposed_at <= PROPOSAL_FOLLOW_TURNS
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
                if m and k in REASK_TARGETED and ((proposed and k == "concerns") or REASK_TARGETED[k].search(q)):
                    continue
                if m and k not in PROFILE_KEYS and not WH_WORD.search(q) and WIDEN.search(q[m.end():]):
                    continue
                head = q[:m.start()] if m else ""
                if m and NARROW.search(head) and any(w in head for w in said):
                    continue
                tail = q[m.start():] if m else ""
                values = known.get(k, [])
                # 保存した値の語(「渋谷あたり」の「渋谷」)を示して確かめる問い(「勤務地は、先ほどの渋谷周辺で探しますか?」)も確認
                # 数を含む値(年収など)と、問いの項目名そのもの(「年収の下限」の「年収」、「不安なこと」の「不安」)は除く
                value_words = [w for v in values if not _numbers(v) for w in re.findall(r"[一-龥ァ-ヴー]{2,}", v)
                               if w not in _KEY_WORDS and m and w not in m.group(0)]
                confirm = any(w in tail for w in [*values, *said, *value_words]) or bool(
                    _numbers(tail) & {n for v in values for n in _numbers(v)})
                if m and not (not WH_WORD.search(q) and confirm):
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
    # 職種名は、前後に漢字が続かないものだけ数える(「営業時間」「研修・教育制度」の「営業」「教育」は職種の羅列ではない)
    job_types = {j.job_type for j in load_jobs().values()}
    named = [t for t in job_types if re.search(f"(?<![一-龥]){re.escape(t)}(?![一-龥])", first)]
    listed = [n for n in sorted(_job_names()) if n in first] + re.findall(r"\d+\s*件", first)
    if turns[0]["cards"] or listed or len(named) > 1:
        out.append(f"T1: 閲覧した求人を羅列した: {listed or [c['job_id'] for c in turns[0]['cards']] or first}")
    return out


def consult_violations(sc: dict, turns: list[dict], user_msgs: list[str]) -> list[str]:
    """相談のシナリオ(sc["consult_mode"])の判定。入口では playbook を読み、求人を出さずに1問聞き、本人が切り上げる前に
    相談を締めない。目的を確かめる前に求人を出さない(本人が「求人を見せて」と言ったときを除く)。"""
    out = []
    first = turns[0] if turns else None
    if sc.get("expect_consult_menu") and first and not any("相談" in q for q in first["quick_replies"]):
        out.append(f"T1: 初回の選択肢に「まず相談する」が無い: {first['quick_replies']}")
    out += purpose_violations(turns, user_msgs) + generic_option_violations(turns, user_msgs)
    entry = next((i for i, m in enumerate(user_msgs[:len(turns)]) if m and CONSULT_SIGN.search(m)), None)
    if entry is None:
        return out
    r = turns[entry]
    if r["cards"] or any(t["name"] == "recommend_jobs" for t in r["tools"]) or not _questions(r["bubbles"]):
        out.append(f"T{entry + 1}: 相談の入口で、ヒアリングの1問から始めていない")
    if not any(playbook_reads(turns[i]) for i in range(entry + 1)):
        out.append(f"T{entry + 1}: 相談に入ったのに playbook(get_counseling_playbook)を読んでいない")
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


# ----------------------------- 相談の playbook と深さ(機械で見られるもの) -----------------------------

PLAYBOOK_TOOL = "get_counseling_playbook"
# 仕事を探し始めること・働くことそのものの語(「パートを始めよう」「転職を考えた」「正社員を目指そう」「探し始めた」)。
# 目的を聞く言い方(PURPOSE_STARTED)の「〜と思ったのは」の前に付く形として使う
_WORK = r"(?<![のでに])(?<!だけ)((?<!ル)バイト|パート|アルバイト|正社員|派遣|契約社員|お仕事|(?<!お)仕事)"
_STARTING = (rf"({_WORK}(を|で|に|として)?(始め|探(?!せな|さな)|目指|働(?!け|かな|かず)|就)|転職|就職"
             r"|(職場|会社|前職|仕事)を辞め"
             r"|(^|(?<=[、,，。!！?？\s])|(?<=今)|(?<=今回)|(?<=なぜ)|(?<=どうして)|(?<=そもそも))"
             r"(探し|探そ|探す|働き始|働きた(い|く)|働こ|始め|(アル)?バイトし(たい|たく|よう)|パートし(たい|たく|よう)))")
# 働き始めたい・探そうと思った理由を聞く言い方(「働き始めたいと思ったのは、どんな変化からですか?」「収入のほかに、
# このバイトで得たいことはありますか?」)。「カフェがいいと思ったのは」「どんな変化がありそうですか」は求人や条件への問い
PURPOSE_STARTED = rf"{_STARTING}[^。!！?？]{{0,8}}と思ったのは|どんな変化から|得たいこと"
# 目的を聞く問い(白紙の問いの REASK["purpose"] に、きっかけ・理由などの言い方を足す)
PURPOSE_ASK = re.compile(REASK["purpose"].pattern + r"|きっかけ|目的|理由|なぜ|どうして|何のため|" + PURPOSE_STARTED)
# 本人が「分からない」「まだ決めていない」と言った(職種を並べず、掘り下げに戻る場面)
DONT_KNOW = re.compile(r"(分から|わから)(ない|なく|ん)|決まって(い)?ない|決めて(い)?ない|思いつかない|ピンと(こ|来)ない")
# 職種を候補として並べる文(提案・比較の言い方)。経歴を受け止める平叙文(「カフェと事務のご経験があるんですね。」)は数えない
SUGGESTING = re.compile(r"向いて|合いそう|近そう|おすすめ|候補|考えられ|いかが|どれ|どちら")


def playbook_reads(r: dict) -> list[str]:
    """そのターンで読んだ playbook の区分(エラーになった呼び出しは数えない)。"""
    return [t["args"].get("track") for t in r["tools"] if t["name"] == PLAYBOOK_TOOL
            and not (isinstance(t.get("result"), dict) and "error" in t["result"])]


def playbook_violations(turns: list[dict]) -> list[str]:
    """区分が変わったターン(set_search_track で実際に切り替えた・取り消した)で、新しい区分の playbook を読み直したか。"""
    return [f"T{i + 1}: 区分を「{track}」に切り替えたのに、同じターンで playbook を読み直していない"
            for i, track in track_switches(turns) if track not in playbook_reads(turns[i])]


def _job_type_aliases() -> dict[str, str]:
    """職種の呼び方 -> 職種(一覧の名前と、「・」で区切った2文字以上の部分。「スーパー・販売」→「スーパー」「販売」)。"""
    out = {}
    for name in (*JOB_TYPES, *FULLTIME_JOB_TYPES):
        for alias in (name, *name.split("・")):
            if len(alias) >= 2:
                out.setdefault(alias, name)
    return out


JOB_TYPE_ALIASES = _job_type_aliases()


def named_job_types(text: str) -> list[str]:
    """文の中で職種として挙げた呼び方(前後に漢字が続くもの「法人営業」「営業時間」は職種の名前ではない)。
    長い呼び方に含まれる短い呼び方(「スーパー・販売」の中の「販売」)は数えない。"""
    hits = [a for a in JOB_TYPE_ALIASES if re.search(f"(?<![一-龥]){re.escape(a)}(?![一-龥])", text)]
    return [a for a in hits if not any(a != b and a in b for b in hits)]


def dont_know_violations(turns: list[dict], user_msgs: list[str | None]) -> list[str]:
    """「分からない」には職種を並べず、掘り下げに戻る。直後の返信の選択肢か、候補を示す文・問いの文に、職種が2つ以上
    あれば違反。本人が同じ発言で「求人を見せて」と頼んだときは除く。"""
    out = []
    for i, m in enumerate(user_msgs[:len(turns)]):
        if not m or not DONT_KNOW.search(m) or asks_for_jobs(m):
            continue
        r = turns[i]
        sentences = [x for b in r["bubbles"] for x in re.split(r"(?<=[。!!?？\n])", b)
                     if "?" in x or "？" in x or SUGGESTING.search(x)]
        named = {JOB_TYPE_ALIASES[a] for x in [*r["quick_replies"], *sentences] for a in named_job_types(x)}
        if len(named) >= 2:
            out.append(f"T{i + 1}: 「分からない」の後に職種を並べた(掘り下げに戻っていない): {sorted(named)}")
    return out


def generic_option_violations(turns: list[dict], user_msgs: list[str | None]) -> list[str]:
    """相談の選択肢に、本人の発言にも履歴書(要約の uploaded_resume)にも無い職種を2つ以上並べない(一般論の候補。
    PM の経歴の人に「オフィス事務 / コールセンター / イベントスタッフ」)。その時点までの発言と履歴書にある職種は、
    本人の話から作った候補として数えない。"""
    out = []
    for i, r in enumerate(turns):
        known = "\n".join(m or "" for m in user_msgs[:i + 1]) + "".join(
            json.dumps(t["result"].get("uploaded_resume"), ensure_ascii=False) for x in turns[:i + 1] for t in x["tools"]
            if t["name"] == "get_user_conversation_summary" and isinstance(t.get("result"), dict))
        unknown = {JOB_TYPE_ALIASES[a] for q in r["quick_replies"] for a in named_job_types(q)
                   if not any(b in known for b, name in JOB_TYPE_ALIASES.items() if name == JOB_TYPE_ALIASES[a])}
        if len(unknown) >= 2:
            out.append(f"T{i + 1}: 本人の発言にも履歴書にも無い職種を選択肢に並べた(一般論の候補): {sorted(unknown)}")
    return out


def purpose_violations(turns: list[dict], user_msgs: list[str | None]) -> list[str]:
    """相談で、目的を確かめる前に求人を出さない。目的を聞く問い(PURPOSE_ASK)か、目的の保存(save_user_preferences の
    purpose)が、最初に求人を出したターンまでにあればよい。本人が自分から「求人を見せて」と頼んでいたら除く
    (こちらが「この内容で求人を見ますか?」と聞いて本人が「求人を見る」と答えたのは、除かない)。"""
    def offered(k: int) -> bool:
        return k > 0 and any("求人" in _question_part(b) for b in turns[k - 1]["bubbles"])

    for i, r in enumerate(turns):
        if not (r["cards"] or any(t["name"] == "recommend_jobs" for t in r["tools"])):
            continue
        if any(asks_for_jobs(m or "") and not offered(k) for k, m in enumerate(user_msgs[:i + 1])):
            return []
        for x in turns[:i + 1]:
            if any(PURPOSE_ASK.search(_question_part(b)) for b in x["bubbles"]) or any(
                    "purpose" in (t["args"].get("preferences") or {}) for t in x["tools"]
                    if t["name"] == "save_user_preferences"):
                return []
        return [f"T{i + 1}: 目的(なぜ今探すのか)を確かめる前に求人を出した"]
    return []


# ----------------------------- 雇用区分の切り替え(set_search_track の呼び出しで判定) -----------------------------

# 挨拶で区分に触れたとみなす言い方(区分つきの再訪)
TRACK_WORDS = {"アルバイト・パート": re.compile(r"アルバイト|バイト|パート"), "正社員": re.compile(r"正社員")}


def track_switches(turns: list[dict]) -> list[tuple[int, str]]:
    """実際に区分が切り替わった set_search_track の呼び出し (ターンの添字, 切り替えた先の区分)。
    エラーになった呼び出しと、今と同じ区分で記録されなかった呼び出し(changed が false)は数えない。"""
    out = []
    for i, r in enumerate(turns):
        for t in r["tools"]:
            res = t.get("result")
            if t["name"] == "set_search_track" and isinstance(res, dict) and res.get("changed") and res.get("track"):
                out.append((i, res["track"]))
    return out


def tracks_by_turn(turns: list[dict], start: str | None = None) -> list[str]:
    """各ターンの返信を書いたときの雇用区分(そのターンで切り替えたなら切り替えた後)。"""
    current = start or DEFAULT_TRACK
    switched = dict(track_switches(turns))  # 同じターンに2回呼んだら最後の区分
    out = []
    for i in range(len(turns)):
        current = switched.get(i, current)
        out.append(current)
    return out


def track_expectations(sc: dict, turns: list[dict], user_msgs: list[str | None]) -> list[dict]:
    """シナリオの expect_tracks(期待する切り替えの並び)と、実際の set_search_track の呼び出しを突き合わせる。
    期待1つは {"track": 区分, "turn": 何ターン目まで(1始まり)} か {"track": 区分, "on_said": 本人の発言の正規表現}。
    on_said は、前の期待の発言より後で、その正規表現に当たった最初の本人の発言に答えるターンが期限になる
    (強い合図で期限より前に切り替えるのは正しい)。結果の status:
    ok(期限までに期待の区分へ)/ missing(切り替えなし)/ wrong_track(次の切り替えが違う区分)/
    late(期待の区分だが期限より後)/ no_trigger(模擬ユーザーが期待の発言をしなかった。エージェントの判定には数えない)。
    期待の後に、別の区分への切り替えがあれば unexpected(expect_tracks が空なら、どの切り替えも unexpected)。"""
    expects = sc.get("expect_tracks")
    if expects is None:
        return []
    switches = track_switches(turns)
    out = []
    k = 0  # 次に見る切り替え
    said_from = 0  # on_said を探し始める本人の発言
    last_track = sc.get("seed_track") or DEFAULT_TRACK
    for n, e in enumerate(expects):
        due = None
        if "turn" in e:
            due = e["turn"] - 1
        else:
            hit = next((i for i in range(said_from, min(len(user_msgs), len(turns)))
                        if user_msgs[i] and re.search(e["on_said"], user_msgs[i])), None)
            if hit is not None:
                due, said_from = hit, hit + 1
        row = {"track": e["track"], "due": None if due is None else due + 1, "status": None,
               "called_turn": None, "called_track": None}
        if due is None:
            row["status"] = "no_trigger"
            out.append(row)
            continue
        later = {x["track"] for x in expects[n + 1:]}
        # 次の切り替えがこの期待の期限より後で、後の期待の区分なら、この期待の分は飛ばされた(missing)
        if k < len(switches) and (switches[k][1] == e["track"] or switches[k][1] not in later or switches[k][0] <= due):
            at, track = switches[k]
            row |= {"called_turn": at + 1, "called_track": track}
            row["status"] = "ok" if track == e["track"] and at <= due else "late" if track == e["track"] else "wrong_track"
            k += 1
        else:  # 切り替えが無いか、次の切り替えは後の期待の区分(この期待の分が飛ばされた)
            row["status"] = "missing"
        last_track = e["track"]
        out.append(row)
    for at, track in switches[k:]:
        if track != last_track:
            out.append({"track": last_track, "due": None, "status": "unexpected", "called_turn": at + 1,
                        "called_track": track})
    return out


TRACK_STATUS = {"missing": "期待した「{track}」への切り替え(T{due}まで)が無い",
                "wrong_track": "T{called_turn}: 「{track}」を期待したのに「{called_track}」へ切り替えた",
                "late": "T{called_turn}: 「{track}」への切り替えが遅い(T{due}まで)",
                "unexpected": "T{called_turn}: 期待していない「{called_track}」への切り替え"}


def track_violations(rows: list[dict]) -> list[str]:
    return [TRACK_STATUS[r["status"]].format(**r) for r in rows if r["status"] in TRACK_STATUS]


def greeting_track_violations(sc: dict, turns: list[dict]) -> list[str]:
    """区分つきの再訪(sc["expect_track_in_greeting"]): 挨拶の1問に、記録された区分を入れる(聞き直さない)。
    行動履歴もあれば、区分と行動履歴を1つの確認形の問いにまとめる(1問であることは turn_violations が見る)。"""
    if not turns or not sc.get("seed_track"):
        return []
    first = "\n".join(turns[0]["bubbles"])
    word = TRACK_WORDS.get(sc["seed_track"])
    out = []
    if word and not word.search(first):
        out.append(f"T1: 再訪の挨拶で、記録された区分({sc['seed_track']})に触れていない")
    if re.search(r"どちら|どっち", _question_part(first)) and all(w.search(first) for w in TRACK_WORDS.values()):
        out.append("T1: 記録された区分を聞き直した")
    return out


def upload_violations(turns: list[dict], upload: dict | None) -> list[str]:
    """履歴書のアップロード(本人の1ターン)への返信: 質問は1つだけで、画面が出した確認をくり返さない。
    履歴書の内容だけで雇用区分を切り替えない(このターンは本人が何も話していない)。"""
    if not upload or upload["turn"] >= len(turns):
        return []
    i = upload["turn"]
    r = turns[i]
    out = []
    if _questions(r["bubbles"]) != 1:
        out.append(f"T{i + 1}: アップロードの後の返信が1問になっていない(質問{_questions(r['bubbles'])}個)")
    shown = {b.strip() for b in upload.get("shown") or [] if b.strip()}
    if any(b.strip() in shown for b in r["bubbles"]):
        out.append(f"T{i + 1}: 画面が出した読み取り結果の確認をくり返した")
    if any(t["name"] == "set_search_track" for t in r["tools"]):
        out.append(f"T{i + 1}: 履歴書の内容だけで雇用区分を切り替えた")
    return out


# ----- 避けたいこと・やりたいことと検索の引数(task 111 のレビュー・task 126 の後続。task 109) -----
# keywords に入った避けたいこと(「接客はしたくない」を keywords に入れると、意味の近さで飲食ホールが上位に来る)。
# 否定の言い方(〜したくない・避けたい・苦手・〜しない・以外・なし)を見る。「経験なし」「少ない」は避けたいことではない
AVOID_IN_KEYWORDS = re.compile(r"たくない|避け|苦手|嫌|以外|NG|抜き|(?<![がの])(?<!少)(さ|し|か|た|わ|ら)ない"
                               r"|(?<!経験)(?<!経験が)(なし|無し)")
# 本人の言葉 -> 当てるべき属性の引数(dip の Prompt「避けたいこと・やりたいことから絞り込みの引数を埋める」と同じ対応)。
# max_* は値がそれ以下、min_* はそれ以上なら当てたとみなす
ATTRIBUTE_SAYINGS = (
    (re.compile(r"人と話す(の|こと)が苦手|黙々と"), "max_talk_level", 1),
    (re.compile(r"接客(は|も|が)?[^。!？?]{0,4}(したくない|やりたくない|苦手|避け|嫌)"), "max_talk_level", 2),
    (re.compile(r"人と話す仕事がしたい|接客が好き"), "min_talk_level", 3),
    (re.compile(r"立ち仕事(は|を)?避け|座ってできる"), "max_physical_level", 1),
    (re.compile(r"力仕事(は|を)?避け|重いもの(は|を)?持てない"), "max_physical_level", 2),
    (re.compile(r"体を動かしたい"), "min_physical_level", 3),
)
# 電話だけを避けたい言い方と、接客まで避けたい答え(task 126: 電話だけなら max_talk_level を付けない)
PHONE_AVOID = re.compile(r"電話(対応|応対)?(は|が|も)?[^。!？?]{0,4}(したくない|やりたくない|苦手|嫌|避け|無理)")
SERVICE_WORDS = re.compile(r"接客|レジ|お客様|お客さん|人と話す")
SERVICE_AVOID = re.compile(rf"({SERVICE_WORDS.pattern})[^。!？?]{{0,6}}(したくない|やりたくない|苦手|嫌|避け|無理)"
                           r"|人と話す(の|こと)が苦手|黙々と")
YES_AVOID = re.compile(r"はい|そう(です|ですね)|避けたい|苦手|嫌|したくない|無理|できれば")
SERVICE_OK = re.compile(r"大丈夫|平気|問題な|できます|好き|いい(です)?$")


def _service_avoided(turns: list[dict], user_msgs: list[str | None], upto: int) -> bool:
    """T1..T(upto+1) の本人の発言までに、接客(お客様と話す仕事)も避けたいと言ったか。
    「お客様と話すレジや接客は大丈夫ですか?」への「はい、避けたいです」のような答えも数える。"""
    for k in range(1, min(upto, len(user_msgs) - 1) + 1):
        m = user_msgs[k] or ""
        if SERVICE_AVOID.search(m):
            return True
        asked = SERVICE_WORDS.search("\n".join(turns[k - 1]["bubbles"]))
        if asked and YES_AVOID.search(m) and not SERVICE_OK.search(m):
            return True
    return False


def _searches(turns: list[dict]):
    """(ターンの添字, ツール名, args)。求人を探す呼び出し(recommend_jobs・relax_conditions)だけ。"""
    for i, r in enumerate(turns):
        for t in r["tools"]:
            if t["name"] in ("recommend_jobs", "relax_conditions"):
                yield i, t["name"], t.get("args") or {}


def search_arg_violations(turns: list[dict], user_msgs: list[str | None]) -> list[str]:
    """避けたいこと・やりたいことを検索の引数に正しく当てたか(違反ではなく別の指標 search_arg_violations で数える。
    基準(2026-09-29)には無い判定なので、violations に混ぜると基準と比べられなくなる)。
    (1) keywords に避けたいことを入れた(否定の言い方)
    (2) 属性で言える避けたいこと・やりたいこと(ATTRIBUTE_SAYINGS)を、言った後の最初の recommend_jobs で引数に当てていない
    (3) 電話だけを避けたい答えで max_talk_level を付けた(接客まで避けたいとは言っていない)"""
    out = []
    for i, name, args in _searches(turns):
        kw = args.get("keywords") or ""
        if AVOID_IN_KEYWORDS.search(kw):
            out.append(f"T{i + 1}: {name} の keywords に避けたいことを入れた: {kw!r}")
        said = "\n".join(m or "" for m in user_msgs[:i + 1])
        level = args.get("max_talk_level")
        if (level is not None and level <= 2 and PHONE_AVOID.search(said)
                and not _service_avoided(turns, user_msgs, i)):
            out.append(f"T{i + 1}: 電話だけを避けたい答えで {name} に max_talk_level={level} を付けた")
    for rx, arg, want in ATTRIBUTE_SAYINGS:
        k = next((k for k, m in enumerate(user_msgs) if m and rx.search(m)), None)
        if k is None:
            continue
        # user_msgs[k] に答えたのが turns[k]。そのターン以降の最初の recommend_jobs
        first = next(((i, a) for i, n, a in _searches(turns) if n == "recommend_jobs" and i >= k), None)
        if first is None:
            continue
        i, a = first
        v = a.get(arg)
        ok = v is not None and (v <= want if arg.startswith("max_") else v >= want)
        if not ok:
            out.append(f"T{i + 1}: 本人の「{rx.search(user_msgs[k]).group(0)}」を recommend_jobs の {arg} に当てていない"
                       f"({arg}={v})")
    return out


def action_offers(turns: list[dict]) -> list[dict]:
    """履歴書・音声の提案(actions)が出たターンと種類(頻度と場面を見るため。違反ではない)。"""
    return [{"turn": i + 1, "actions": r["actions"]} for i, r in enumerate(turns) if r.get("actions")]


def conversation_checks(sc: dict, turns: list[dict], user_msgs: list[str], facts: dict | None = None,
                        upload: dict | None = None) -> dict:
    """会話全体の判定(KPI と シナリオ固有の期待)。
    upload は会話の途中の履歴書のアップロード({"turn": そのターンの添字, "shown": 画面が出した確認の吹き出し})。"""
    tools_by_turn = [[t["name"] for t in r["tools"]] for r in turns]
    tracks = tracks_by_turn(turns, sc.get("seed_track"))
    outcomes = [t["args"].get("outcome") for r in turns for t in r["tools"] if t["name"] == "record_conversation_outcome"]
    branch_turn = next((i + 1 for i, names in enumerate(tools_by_turn)
                        if set(names) & BRANCH_TOOLS or "record_conversation_outcome" in names), None)
    first = turns[0] if turns else None
    violations = [v for i, r in enumerate(turns) for v in turn_violations(i, r, tracks[i])]
    track_rows = track_expectations(sc, turns, user_msgs)
    violations += track_violations(track_rows)
    if sc.get("expect_track_in_greeting"):
        violations += greeting_track_violations(sc, turns)
    violations += upload_violations(turns, upload)
    violations += playbook_violations(turns) + dont_know_violations(turns, user_msgs)
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
        pii_turn = next((i for i, m in enumerate(user_msgs) if pii(m or "")), None)
        if pii_turn is not None and pii_turn < len(turns) and not turns[pii_turn]["buttons"]:
            violations.append(f"T{pii_turn + 1}: 個人情報を書かれた後に入力画面を案内していない")
    violations += reask_violations(turns, user_msgs,
                                   sc.get("seed_preferences") if sc.get("expect_uses_preferences") else None)
    if sc.get("consult_mode"):
        violations += consult_violations(sc, turns, user_msgs)
    for i, m in enumerate(user_msgs):  # 「今はいい」「送らないで」の直後は、質問や求人を続けずに短く終える
        if m and QUICK_END.fullmatch(m.strip()) and i < len(turns) and (_questions(turns[i]["bubbles"]) or turns[i]["cards"]):
            violations.append(f"T{i + 1}: 「今はいい/送らないで」の後に質問や求人を続けた")
    # 分岐の期限は、期限のターン(v1 は3ターン目)の区分の決まり。指標の名前は前の結果と比べられるよう変えない
    branch_by = FORMAT_RULES.get(tracks[min(2, len(tracks) - 1)] if tracks else DEFAULT_TRACK,
                                 FORMAT_RULES[DEFAULT_TRACK])["branch_by"]
    judged = [r for r in track_rows if r["status"] != "no_trigger"]
    if sc.get("expect_tracks") is None or (sc["expect_tracks"] and not judged):
        track_ok = None  # 期待が無いか、模擬ユーザーが期待の発言をしなかった
    else:
        track_ok = all(r["status"] == "ok" for r in judged)
    return {
        "turns": len(turns),
        "branch_turn": branch_turn,
        # 相談モードのシナリオは「3ターン以内に分岐」を緩める(ヒアリングが終わるか本人が切り上げるまで続けてよい)ので測らない
        "branch_within_3": None if sc.get("consult_mode") else branch_turn is not None and branch_turn <= branch_by,
        "tracks": tracks,
        "track_checks": track_rows,
        # 期待した切り替えがすべて期限までに期待の区分で起き、余計な切り替えが無い(期待の無いシナリオは None)
        "track_ok": track_ok,
        "action_offers": action_offers(turns),
        "outcomes": outcomes,
        "outcome_recorded": bool(outcomes),
        "outcome_as_expected": bool(set(outcomes) & set(sc["expect_outcome"])),
        "violations": violations + fabrications,
        "fabrications": fabrications,
        "search_arg_violations": search_arg_violations(turns, user_msgs),
    }
