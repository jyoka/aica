"""送る前に伏せる(task 211、docs/design/privacy.md の 3.2)。実 LLM も実 LINE も OpenAI も呼ばない。

- トークの発言の電話番号・メールアドレス・住所らしい所は、LLM に送る前、会話を保存する前に [個人情報を削除] に置き換わる
  (Web と LINE の入口の turn.accept。handle を直に呼ぶ評価も runtime.event_input で伏せる)。画面には伏せたことを知らせる
- system prompt に LINE のユーザーIDを載せない。ツールの line_user_id は PinUserMiddleware が会話のユーザーに固定する
- dip のツールの結果にもユーザーIDは入らない(LLM に届かない)
- 音声相談の調べものの問いに個人情報らしい値があれば、web で調べずに断る。問いは LLM が書くので、住所は住む言葉が無くても形で断る
- 住所らしい所は、同じ文に住んでいる所を言う言葉があるときだけ伏せる。電話番号とメールアドレスはいつも伏せる
- 伏せても、求人の検索と会話の流れは変わらない(ふつうの条件の言い方、駅の名前、丁目は伏せない。
  住所を伏せるのは住所の部分だけで、同じ文の検索の条件は残る)

    ./.venv/bin/python -m pytest -q tests/test_redact_before_send.py

値はすべて架空(電話番号・メールアドレス・住所は例示用の形)。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from dip_mcp import logic
from dip_mcp import server as dip_server
from dip_mcp.data import State, load_jobs
from line_runtime import turn
from line_runtime.agent import PINNED_USER
from resume_draft.pii import (
    REDACTED,
    has_pii,
    looks_like_address,
    personal_value,
    redact_personal,
)
from tests.test_arcade import _free_port, browser  # noqa: F401  browser はフィクスチャ
from tests.test_dip_summary_read import TOOL_ARGS
from tests.test_handle import ScriptedModel, call, reply, run
from tests.test_revisit import Greeter, greet, propose
from tests.test_talk_page import app, open_talk, stream  # noqa: F401  app はフィクスチャ

DEPS = SimpleNamespace(state=None)  # 同意はテストの既定で「している」扱い(tests/conftest.py)。状態は読まない
PHONE, MAIL, ADDRESS = "090-1234-5678", "sample.user@example.com", "みほん市みほん町1丁目2-3"
SAID = f"渋谷で日中に働きたいです。連絡は {PHONE} か {MAIL} に。家は {ADDRESS} です"
SENT = f"渋谷で日中に働きたいです。連絡は {REDACTED} か {REDACTED} に。家は {REDACTED} です"


def _no_personal(text: str) -> bool:
    return all(v not in text for v in (PHONE, MAIL, "1丁目", "2-3"))


# ----------------------------- 受け付けで伏せる -----------------------------


def test_accept_redacts_phone_mail_and_address_and_marks_the_turn():
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", SAID, None)
    assert t.text == SENT and t.redacted is True
    # 全角で書いても伏せる(照合の前に半角にそろえる決まりは resume_draft.pii と同じ)。電話番号は住む言葉が無くても伏せる。
    # 電話番号の形は住所の形(1-2-3)にも当たるが、住む言葉の無い文では住所とみなさないので、電話番号の所だけを伏せる
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", "渋谷で働きたい。電話は０９０－１２３４－５６７８です", None)
    assert t.text == f"渋谷で働きたい。電話は{REDACTED}です" and t.redacted
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", f"{PHONE}に連絡して渋谷で日中がいい", None)
    assert t.text == f"{REDACTED}に連絡して渋谷で日中がいい"
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", f"{MAIL} に送って", None)
    assert t.text == f"{REDACTED} に送って"


# 電話番号は書き方ではなく、区切り(空白、-、括弧、.)を除いた数字の並びで見る(全角は先に半角にそろえる)。番号はどれも架空
PHONE_WAYS = ["(03)1234-5678", "+81 90-1234-5678", "+81-90-1234-5678", "090 1234 5678", "０９０−１２３４−５６７８",
              "（０３）１２３４－５６７８", "+81(0)90-1234-5678", "(+81)3-1234-5678", "＋８１ ９０ １２３４ ５６７８", "090.1234.5678",
              "( 03 ) 1234 - 5678"]
# 数字の並びでも電話番号ではないもの(空白で区切った数のすぐ後ろが単位、0 で始まらない、桁が足りない、81 の前に + も括弧も無い、
# 数字のすぐ後ろの「.」「-」から始まる日付の期間)
NOT_PHONES = ["時給(1200円)", "年収3000000円", "2026-10-26", "年収3,000,000円", "0120円", "定員 0 100 200 3000人",
              "81 90 1234 5678", "定員 0 1000 2000 3000人", "１個@100円", "メールは@で",
              "2001.04.01-2005.03.31まで勤めていました", "2020.04.01 - 2024.03.31"]
# 電話番号のすぐ後ろに単位の字で始まる語が続くもの。単位とみなさず、番号の所を伏せる
PHONE_THEN_WORD = [
    ("連絡先は(03)1234-5678 月曜は休みです", f"連絡先は{REDACTED} 月曜は休みです"),
    ("+81 90-1234-5678 日中なら出られます", f"{REDACTED} 日中なら出られます"),
    ("+81-90-1234-5678本人の携帯です", f"{REDACTED}本人の携帯です"),
    ("090.1234.5678 日中つながります", f"{REDACTED} 日中つながります"),
    ("(03)1234-5678 分かりにくければメールで", f"{REDACTED} 分かりにくければメールで"),
    ("電話(03)1234-5678 年末は不在", f"電話{REDACTED} 年末は不在"),
    ("090.1234.5678日中", f"{REDACTED}日中"),
    ("(03) 1234 5678日中", f"{REDACTED}日中"),
    ("+81 90 1234 5678時間外は留守電", f"{REDACTED}時間外は留守電"),
]


@pytest.mark.parametrize("phone", PHONE_WAYS)
def test_phone_numbers_are_redacted_however_they_are_written(phone):
    said = f"連絡は{phone}にお願いします"
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", said, None)
    assert t.text == f"連絡は{REDACTED}にお願いします" and t.redacted, phone
    # 後ろに別の数が続いても、電話番号の所は伏せる
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", f"電話は {phone} 2回かけた", None)
    assert t.text == f"電話は {REDACTED} 2回かけた", phone
    # 本人の値の照合(dip の保存)と、調べものの問いも同じ決まりで当たる
    assert has_pii(said) and personal_value(said), phone
    assert has_pii(f"調べること: {phone} だれの番号"), phone


@pytest.mark.parametrize(("said", "sent"), PHONE_THEN_WORD)
def test_a_word_after_a_phone_number_does_not_let_it_through(said, sent):
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", said, None)
    assert t.text == sent and t.redacted, said
    assert has_pii(said) and personal_value(said), said


def test_a_number_with_a_unit_after_a_phone_number_is_kept():
    # 空白で区切った別の数のすぐ後ろが単位なら、人数や回数として残す
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", "電話は 090 1234 5678 3人で使っています", None)
    assert t.text == f"電話は {REDACTED} 3人で使っています"


@pytest.mark.parametrize("text", NOT_PHONES)
def test_numbers_that_are_not_phone_numbers_are_kept(text):
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", f"{text}で探して", None)
    assert t.text == f"{text}で探して" and not t.redacted, text
    assert not has_pii(text), text


def test_mail_addresses_are_redacted_with_fullwidth_at_and_without_a_dot():
    for mail in ("sample.user@example.com", "sample_user+job@example.co.jp", "ｓａｍｐｌｅ＠ｅｘａｍｐｌｅ．ｃｏｍ",
                 "sample-user@example", "ｓａｍｐｌｅ＠ｅｘａｍｐｌｅ"):
        t = turn.accept(DEPS, "w-0123456789abcdef", "message", f"メールは {mail} です", None)
        assert t.text == f"メールは {REDACTED} です", mail
        assert has_pii(f"調べること: {mail} の持ち主"), mail


def test_a_mail_address_written_right_next_to_japanese_redacts_only_the_address():
    # 名前とドメインに、かなや漢字は含めない。前後の文(検索の条件)は残す
    for said, sent in (("連絡はsample@example.comで新宿のカフェを探して", f"連絡は{REDACTED}で新宿のカフェを探して"),
                       ("連絡はsample@example.comで新宿三丁目5分以内のカフェを探して",
                        f"連絡は{REDACTED}で新宿三丁目5分以内のカフェを探して"),
                       ("メールはsample@example.comです", f"メールは{REDACTED}です"),
                       ("メールはｓａｍｐｌｅ＠ｅｘａｍｐｌｅ．ｃｏｍです", f"メールは{REDACTED}です")):
        t = turn.accept(DEPS, "w-0123456789abcdef", "message", said, None)
        assert t.text == sent and t.redacted, said


# 求人を探すときのふつうの言い方。どれも伏せない(伏せると検索の条件が消える)
ORDINARY = [
    "渋谷で日中", "時給1200円以上で週3-4日", "10時-18時で働きたい", "2026-11-01から働けます", "年収400-500万円",
    "新宿区で3-6か月", "1日8時間まで", "土日祝休みで残業は月10時間まで", "正社員で年収600万以上", "中野駅の近く",
    "まず相談する", "今はいい", "東京都内のコンビニ", "新宿三丁目駅の近くで働きたい", "渋谷駅の近くで",
    # AI は求人の最寄り駅(「新宿三丁目」)をそのまま言う。住む言葉の無い地名、駅、丁目、数の幅は伏せない
    "新宿三丁目の飲食ホールが気になります", "新宿三丁目で働きたい", "新宿三丁目5分以内で探して", "新宿三丁目の5-10人の職場",
    "新宿三丁目18時から働ける", "新宿三丁目1200円以上", "新宿三丁目 18-22", "大井町3-4人の職場", "西新宿2-8-1の近くの店",
    "番地は言いたくないです", "住み込みで新宿三丁目の求人", "住宅手当がある新宿三丁目の求人",
    # 引っ越しの仕事と、前に漢字が付く「家は」は住む言葉ではない
    "引っ越しのバイトを新宿三丁目5分以内で探して", "引越しスタッフ、新宿三丁目の近く", "専門家は新宿三丁目にいますか",
]


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_search_words_are_not_redacted(text):
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", text, None)
    assert t.text == text and t.redacted is False


def test_job_card_buttons_send_the_title_unchanged():
    """求人カードのボタンが送る文(talk-core.js、LINE の Flex)は、どの求人でも伏せない(求人の話が続けられる)。"""
    for job_id, job in load_jobs().items():
        for text in (f"「{job.title}」について詳しく教えて", f"「{job.title}」({job_id})を保存して"):
            t = turn.accept(DEPS, "w-0123456789abcdef", "message", text, None)
            assert t.text == text, text


def test_too_long_is_checked_on_what_the_user_wrote_and_on_what_is_sent():
    with pytest.raises(turn.BadTurn):
        turn.accept(DEPS, "w-0123456789abcdef", "message", PHONE * 500, None)
    # 伏せると長くなる(「番地」の 2 字が [個人情報を削除] の 9 字に)。送る文も上限を超えない
    text = "家は" + " 番地" * 1666
    assert len(text) <= turn.MAX_TEXT_CHARS
    with pytest.raises(turn.BadTurn):
        turn.accept(DEPS, "w-0123456789abcdef", "message", text, None)


def test_redaction_repeats_until_nothing_is_left():
    """電話番号のすぐ後ろにメールアドレスが続いても、どちらも伏せる(1 回では、メールが前の文字に続いて見えて当たらない)。
    伏せた文をもう一度伏せても変わらない(runtime.event_input でもう一度伏せる)。"""
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", f"{PHONE}.{MAIL}", None)
    assert t.text == REDACTED * 2 and redact_personal(t.text) == t.text
    assert redact_personal(SENT) == SENT


def test_addresses_are_redacted_only_with_words_about_where_the_user_lives():
    """住所らしい所は、同じ文に住んでいる所を言う言葉(「住んでいる」「住所」「在住」「自宅」「家は」「引っ越し」など)が
    あるときだけ伏せる。駅の名前や数の幅は住所と同じ形になることがあり、形だけでは見分けられないため。"""
    # 伏せるのは住所の部分(地名から番地まで)だけ。住む言葉と文の残りは残る
    for text, said in (("新宿三丁目に住んでいます", f"{REDACTED}に住んでいます"),
                       ("住所は新宿区西新宿2-8-1です", f"住所は{REDACTED}です"),
                       ("西新宿三丁目に住んでいます", f"{REDACTED}に住んでいます"),
                       ("新宿三丁目5-1に住んでいます", f"{REDACTED}に住んでいます"),
                       ("渋谷区神南1丁目2-3在住", f"{REDACTED}在住"),
                       ("自宅は渋谷区神南1-2-3", f"自宅は{REDACTED}"),
                       ("実家はみほん市みほん町3丁目", f"実家は{REDACTED}"),
                       ("みほん町３－５に引っ越しました", f"{REDACTED}に引っ越しました"),
                       ("１－２－３に住む", f"{REDACTED}に住む"),
                       ("東京都みほん区みほん町1丁目2-3みほんビル101号室に住む", f"{REDACTED}に住む")):
        t = turn.accept(DEPS, "w-0123456789abcdef", "message", text, None)
        assert t.text == said and t.redacted, text
        assert personal_value(text), text
    # 句読点や空白の無い 1 文に住所と検索の条件を書いても、条件は残る
    for text, said in (("新宿区西新宿2-8-1に住んでいて近くのカフェで働きたい", f"{REDACTED}に住んでいて近くのカフェで働きたい"),
                       ("みほん市みほん町1-2-3に住んでいて時給1200円以上のカフェを探しています",
                        f"{REDACTED}に住んでいて時給1200円以上のカフェを探しています"),
                       ("新宿三丁目に住んでいて渋谷駅近くのカフェで働きたい", f"{REDACTED}に住んでいて渋谷駅近くのカフェで働きたい"),
                       ("自宅はみほん市3-5なので近くの倉庫の仕事がいいです", f"自宅は{REDACTED}なので近くの倉庫の仕事がいいです"),
                       ("家は新宿区西新宿2-8-1の近くの店で週3-4日", f"家は{REDACTED}の近くの店で週3-4日")):
        t = turn.accept(DEPS, "w-0123456789abcdef", "message", text, None)
        assert t.text == said and t.redacted, text
    # 住所の部分だけを伏せても、地名、番地、建物の名前は残さない
    for text, said in (("自宅はみほん市あおば台1-2-3です", f"自宅は{REDACTED}です"),
                       ("千代田区霞が関1-2-3に住んでいます", f"{REDACTED}に住んでいます"),
                       ("みほん台1-2-3-405に住んでいます", f"{REDACTED}に住んでいます"),
                       ("自宅はみほん町1-2-3のぞみハイツ201号室です", f"自宅は{REDACTED}です"),
                       ("自宅は1-2-3 みほんハイツＡ棟101です", f"自宅は{REDACTED}です"),
                       ("自宅はみほん町123番地の4です", f"自宅は{REDACTED}です"),
                       ("みほん町2番3号に住んでいます", f"{REDACTED}に住んでいます"),
                       ("東京都 みほん市 みほん町1-2-3に住んでいます", f"{REDACTED}に住んでいます"),
                       ("一軒家はみほん市みほん町1-2-3です", f"一軒家は{REDACTED}です"),
                       ("住所\n東京都みほん市\nみほん町1-2-3", f"住所\n{REDACTED}"),
                       # 町名の後ろに「丁目-番地」の 2 つ組だけを書く住所と、「4の5の6」の形
                       ("住所は東京都新宿区西新宿2-8です", f"住所は{REDACTED}です"),
                       ("住所は東京都中央区銀座4-5です", f"住所は{REDACTED}です"),
                       ("住所は長野県みほん市大字みほん1234-5です", f"住所は{REDACTED}です"),
                       ("住所は東京都中央区銀座4の5の6です", f"住所は{REDACTED}です"),
                       # 住む言葉と同じ文では、地名の後ろの数の並びを、数が 1 つでも住所とみなす(「12の3」「二番八号」も)
                       ("住所はみほん県みほん郡みほん町大字みほん1234です", f"住所は{REDACTED}です"),
                       ("住所はみほん町552です", f"住所は{REDACTED}です"),
                       ("自宅はみほん市あおば町12の3です", f"自宅は{REDACTED}です"),
                       ("住所はみほん町552のみほんハイツ101号室です", f"住所は{REDACTED}です"),
                       ("住所はみほん町二番八号です", f"住所は{REDACTED}です"),
                       ("住所は港区芝公園4丁目2の8です", f"住所は{REDACTED}です"),
                       # 地名と番地の間に空白 1 つがあっても、地名から番地まで伏せる
                       ("住所はみほん市みほん町 552です", f"住所は{REDACTED}です"),
                       ("住所はみほん町 5-6です", f"住所は{REDACTED}です"),
                       ("住所はみほん郡みほん町大字みほん 1234です", f"住所は{REDACTED}です"),
                       ("住所は東京都千代田区千代田 1-1-1です", f"住所は{REDACTED}です"),
                       # 郵便番号から書いた住所は、郵便番号も伏せる
                       ("住所は〒100-0001 東京都千代田区千代田1-1です", f"住所は{REDACTED}です"),
                       # 住む言葉のすぐ後ろのひらがなの町名は、助詞の字で終わっても残さない
                       ("住所はみなと台3-5-1です", f"住所は{REDACTED}です"),
                       ("住所はひばりが丘3丁目5-1です", f"住所は{REDACTED}です"),
                       ("住んでいるのはみなと台3-5-1です", f"住んでいるのは{REDACTED}です"),
                       ("引っ越し先は東京都中央区銀座4-5-6です", f"引っ越し先は{REDACTED}です"),
                       # 文の始まりの助詞は地名に含めない
                       ("を新宿三丁目に住んでる", f"を{REDACTED}に住んでる")):
        t = turn.accept(DEPS, "w-0123456789abcdef", "message", text, None)
        assert t.text == said and t.redacted, text
    # 伏せるのは住む言葉と同じ文の住所らしい所だけ。ほかの文の条件は残る
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", "新宿三丁目5分以内で探して。家は 渋谷区神南1丁目2-3 です", None)
    assert t.text == f"新宿三丁目5分以内で探して。家は {REDACTED} です"
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", "住所は、みほん市みほん町1丁目。渋谷駅の近くで働きたい", None)
    assert t.text == f"住所は、{REDACTED}。渋谷駅の近くで働きたい"
    # 見出しだけの行(「住所:」「【住所】」「自宅」)の後ろの行の住所も伏せる。連絡先を貼るときによくある書き方
    for text, said in (("住所:\n東京都みほん区みほん町1-2-3", f"住所:\n{REDACTED}"),
                       ("【住所】\nみほん市みほん町1-2-3", f"【住所】\n{REDACTED}"),
                       ("自宅\nみほん市みほん町3丁目", f"自宅\n{REDACTED}"),
                       ("現住所：\n\nみほん市みほん町1-2-3", f"現住所：\n\n{REDACTED}"),
                       ("住所\n〒100-0001\nみほん市みほん町3丁目", f"住所\n{REDACTED}"),
                       ("住所:\nみほん市みほん町1-2-3\n新宿三丁目5分以内で探して",
                        f"住所:\n{REDACTED}\n新宿三丁目5分以内で探して")):
        t = turn.accept(DEPS, "w-0123456789abcdef", "message", text, None)
        assert t.text == said and t.redacted, text
        assert personal_value(text), text
    # ふつうの文の改行は、次の行に効かせない
    text = "住所は言いたくないです\n新宿三丁目5分以内で探して"
    assert turn.accept(DEPS, "w-0123456789abcdef", "message", text, None).text == text
    assert not personal_value(text)
    # 住む言葉の無い文は、地名、駅、丁目、番地の形でも伏せない(personal_value も同じ)
    for text in ORDINARY:
        assert not personal_value(text), text
    # 住む言葉と同じ文でも、後ろに単位が続く数(人数、回数、給与、日付、年代、駅の数、時間)と、時給・週などの後ろの数、
    # 桁の区切り・時刻・幅の続き(「1,000円」「18:00」「4〜5時間」)は住所とみなさない。どれも求人の検索の条件
    for text in ("住んでる所から5-10人の職場", "自宅から時給1000-1200円", "家から3-6か月以内に住所を決めたい",
                 "自宅は町田市で時給1200円", "家から2駅以内で20代歓迎の店", "自宅から近い所で週3日、1日4時間",
                 "自宅から一番近い店舗", "自宅の近くで時給は1,000円以上がいい", "家は新宿で、時給は1,200円以上",
                 "自宅の近くで、18:00から22:00まで働きたい", "自宅から近くで、朝6:30から", "自宅から近い所で、1日4〜5時間",
                 "自宅から近い所で、1日4～5時間", "家は駅から10分で、1日8h働きたい", "自宅は町田市で時給 1200",
                 "自宅から近くで 3,000円", "自宅の近くで簿記2級を活かしたい"):
        assert turn.accept(DEPS, "w-0123456789abcdef", "message", text, None).text == text, text
    t = turn.accept(DEPS, "w-0123456789abcdef", "message", "住所はみほん町2-3で月3-5回、年収400-500万円", None)
    assert t.text == f"住所は{REDACTED}で月3-5回、年収400-500万円"


def test_research_queries_with_an_address_shape_are_personal_without_words_about_living():
    """調べものの問いは LLM が書いて外へ出る(OpenAI と検索エンジン)。本人が話した住所を住む言葉を外して書き写せるので、
    住む言葉が無くても番地まである住所の形なら looks_like_address が真(本人の文の personal_value は偽のまま)。
    番地の無い地名、駅、丁目までの地名と、単位の付いた数は、今までどおり調べる。"""
    for query in ("調べること: みほん市みほん町1-2-3 から近いコンビニバイト", "調べること: みほん町552 近くの飲食店の評判",
                  "調べる会社・お店: ナイトマート(手がかり: みほん市みほん町1-2-3の近く)", "みほん町 552 の近く",
                  "みほん市みほん町1丁目2-3の店", "大字みほん１２３４の店", "みほん町二番八号の店", "1-2-3-405",
                  "みほん市3-5", "西新宿2-8", "大字みほん 1234", "みほん町字あおば 12", "みほん村 12", "西新宿三丁目2の店"):
        assert looks_like_address(query), query
        assert not personal_value(query), query
    # 区切りの無い数 1 つは、すぐ前が町名の終わり(町・村・丁目・番地・大字や字の名前)のときだけ番地とみなす。
    # 問いはキーワードを空白で区切るので、言葉の後ろの年や件数、店の名前の数は調べる
    for query in ("調べること: 中野のコンビニ", "新宿三丁目のカフェ", "西新宿1丁目の店", "コンビニバイト 時給 2026年",
                  "コンビニバイト 時給 2026", "簿記2級 難易度", "新宿駅から徒歩5分", "TOEIC 800点", "5-10人の店",
                  "全国の、コンビニのアルバイト・パートの平均時給。", "18:00から22:00の求人",
                  "調べること: 東京都 最低賃金 2026", "調べること: 宅建 合格率 2025", "調べること: コンビニバイト おすすめ 10選",
                  "調べること: 東京23区の、カフェのアルバイト・パートの平均時給。", "調べる会社・お店: ローソンストア100\n",
                  "第2新卒", "漢字検定 2", "マツモトキヨシ 新宿3号店"):
        assert not looks_like_address(query), query


def test_every_job_station_is_kept_with_or_without_eki():
    """求人データのどの最寄り駅も、「駅」を付けても外しても伏せない(AI が言う求人の最寄り駅を本人が返せる)。"""
    for station in {j.station for j in load_jobs().values()}:
        for name in (station, station.removesuffix("駅")):
            text = f"{name}の求人が気になります"
            assert turn.accept(DEPS, "w-0123456789abcdef", "message", text, None).text == text, text


# ----------------------------- Web のトーク: LLM と会話の保存に届かない -----------------------------


def test_web_talk_redacts_before_the_llm_and_the_saved_conversation_and_search_still_works(app):  # noqa: F811
    from fastapi.testclient import TestClient

    seen: list = []

    def search(messages):
        seen.append(list(messages))
        return call("recommend_jobs", {"line_user_id": PINNED_USER, "area": "渋谷", "time_band": "日中"}, "rec")

    def remembers(messages):  # 次のターンでも、前の発言は伏せた文のまま
        seen.append(list(messages))
        return reply(["1件目は駅から近いです。"])

    model = Greeter(script=[greet, search, propose, remembers])
    app.set_model(model)
    with TestClient(app.app) as client:
        opened, greeting = open_talk(client)
        uid = opened["user"]
        evs = stream(client, uid, "message", greeting["thread"], SAID)
        r = evs[-1]
        assert r["kind"] == "reply" and r["redacted"] is True
        assert len(r["cards"]) == 3  # 伏せても求人の検索はそのまま
        nxt = stream(client, uid, "message", greeting["thread"], "1件目は?")[-1]
        assert nxt["bubbles"] == ["1件目は駅から近いです。"] and "redacted" not in nxt  # 伏せなかったターンには付けない
        hist = client.get("/api/talk/history").json()
    assert model.script == []

    first, second = seen
    humans = [m.content for m in first if m.type == "human"]
    assert humans[-1] == SENT  # LLM に送ったのは伏せた文
    assert all(_no_personal(m.content) for m in second if m.type == "human")
    assert uid not in first[0].content  # system prompt にユーザーIDは無い
    # 保存した会話(再読み込みで描き直す履歴)も伏せた文。画面が知らせを出せるよう redacted が付く
    said = next(t for t in hist["turns"] if t["user"] == SENT)
    assert said["redacted"] is True and len(said["bot"]["cards"]) == 3
    assert all("redacted" not in t for t in hist["turns"] if t is not said)


def test_handle_called_directly_also_redacts(tmp_path):
    """評価などが LineRuntime.handle を直に呼んでも(turn.accept を通らない)、伏せた文だけが LLM と会話に入る。"""
    seen: list = []

    class Seeing(ScriptedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append(list(messages))
            return super()._generate(messages, stop, run_manager, **kwargs)

    model = Seeing(script=[reply(["承知しました。"])])
    (r,) = run(model, [("sim-new", "message", SAID)], tmp_path)
    assert r["bubbles"] == ["承知しました。"]
    assert [m.content for m in seen[0] if m.type == "human"] == [SENT]


# ----------------------------- system prompt にユーザーIDを載せない -----------------------------


def test_system_prompt_has_no_user_id_and_tools_still_reach_this_user(tmp_path):
    """LLM は line_user_id に PINNED_USER を渡すだけ。保存も検索も、この会話のユーザーの分に入る。"""
    uid = "w-0123456789abcdef"
    seen: list = []

    class Seeing(ScriptedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append(messages[0].content)
            return super()._generate(messages, stop, run_manager, **kwargs)

    model = Seeing(script=[
        call("save_user_preferences", {"line_user_id": PINNED_USER, "preferences": {"area": "渋谷"}}),
        call("recommend_jobs", {"line_user_id": PINNED_USER, "area": "渋谷", "time_band": "日中"}, "t2"),
        reply(["渋谷・日中で探しました。"]),
    ])
    (r,) = run(model, [(uid, "message", "渋谷で日中")], tmp_path)
    assert all(uid not in s and f"「{PINNED_USER}」" in s for s in seen)
    assert len(set(seen)) == 1  # 毎回同じ文(プロンプトキャッシュを壊さない)
    assert [t["name"] for t in r["tools"]] == ["save_user_preferences", "recommend_jobs"]
    state = State(tmp_path / "state.json")
    assert logic.conversation_summary(state, uid)["stated_preferences"]["area"]["value"] == "渋谷"
    users = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["users"]
    assert PINNED_USER not in users  # 「me」というユーザーは作られない


@pytest.mark.parametrize("uid", ["w-0123456789abcdef", "w-0123456789abcdef:sim-applied"])
def test_dip_tools_do_not_return_the_user_id(tmp_path, monkeypatch, uid):
    """どのツールの結果にも、呼んだユーザーのIDが入らない(結果は LLM に届くので)。
    開発者画面のシナリオユーザー(匿名ID:シナリオ)は、画面の URL にシナリオの名前だけが載り、匿名IDは載らない。"""
    state = State(tmp_path / "state.json")
    state.reset()
    if ":" in uid:
        state.reset_user(uid, template=uid.partition(":")[2])
    monkeypatch.setattr(dip_server, "state", state)
    anon = uid.partition(":")[0]
    tools = {t.name for t in asyncio.run(dip_server.mcp.list_tools())}
    assert tools == set(TOOL_ARGS)  # ツールを足したら、ここでも確かめる
    for name, args in TOOL_ARGS.items():
        if name not in ("answer_job_question", "get_counseling_playbook"):
            args = {"line_user_id": uid, **args}
        out = asyncio.run(dip_server.mcp.call_tool(name, args))
        assert anon not in json.dumps(out, ensure_ascii=False, default=str), name


# ----------------------------- LINE のトーク -----------------------------


def test_line_talk_redacts_before_the_llm_and_nothing_personal_is_written(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from line_runtime import line_channel
    from line_runtime.line_channel import line_user
    from tests.test_line_webhook import ID_KEY, RAW_USER, _event, _post, _server

    monkeypatch.setenv(line_channel.ID_KEY_ENV, ID_KEY)  # test_line_webhook の line_id_key と同じ
    monkeypatch.delenv(line_channel.LIFF_ID_ENV, raising=False)
    monkeypatch.delenv(line_channel.LOGIN_CHANNEL_ENV, raising=False)

    uid = line_user(RAW_USER)
    seen: list = []

    def search(messages):
        seen.append(list(messages))
        return call("recommend_jobs", {"line_user_id": PINNED_USER, "area": "渋谷", "time_band": "日中"}, "rec")

    model = Greeter(script=[search, propose])
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        assert _post(client, [_event("message", text=SAID)]).status_code == 200
    assert model.script == []
    (msgs,) = seen
    assert [m.content for m in msgs if m.type == "human"] == [SENT]
    assert uid not in msgs[0].content
    ((_, body),) = fake.sent()
    assert any(m["type"] == "flex" for m in body["messages"])  # 求人のカードは届く
    for p in (p for p in tmp_path.rglob("*") if p.is_file()):
        raw = p.read_bytes()
        assert PHONE.encode() not in raw and MAIL.encode() not in raw and "1丁目".encode() not in raw, p.name


# ----------------------------- 画面: 伏せたことを本人に知らせる -----------------------------


@pytest.fixture
def served(app):  # noqa: F811
    """本物の HTTP サーバー(uvicorn)で画面を出す。モデルは起動の前に差し込む(ランタイムは起動のときに作る)。"""
    import threading
    import time

    import uvicorn

    app.set_model(Greeter(script=[greet, reply(["承知しました。"]), reply(["はい。"])]))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(300):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(10)


def test_talk_screen_tells_the_user_under_their_bubble_and_after_reload(served, browser):  # noqa: F811
    """本番のトーク画面で個人情報らしい所を含めて送ると、本人の吹き出しのすぐ下に知らせが出る。再読み込みで描き直した履歴は、
    伏せた後の文と同じ知らせ。伏せない発言には出さない。"""
    page = browser.new_context().new_page()
    page.set_default_timeout(10_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(served + "/")
    page.locator("#log .row.bot .bubble", has_text="バイト探しをお手伝いします").wait_for()
    page.locator("#t").fill(SAID)
    page.locator("#send").click()
    note = page.locator("#log .redacted")
    note.wait_for()
    assert note.inner_text() == "電話番号などは送らないようにしました"
    # 本人の吹き出し(送った直後は本人が書いたまま。このブラウザの中だけ)のすぐ下、返信より前
    assert note.evaluate("n => n.previousElementSibling.matches('.row.me') && n.nextElementSibling.matches('.row.bot')")
    assert page.locator("#log .row.me .bubble").last.inner_text() == SAID
    page.locator("#t").fill("渋谷で日中")
    page.locator("#send").click()
    page.locator("#log .row.bot .bubble", has_text="はい。").wait_for()
    assert note.count() == 1  # 伏せない発言には出さない

    page.reload()
    page.locator("#log .row.bot .bubble", has_text="はい。").wait_for()
    assert note.count() == 1
    assert page.locator("#log .row.me .bubble").nth(0).inner_text() == SENT
    assert note.evaluate("n => n.previousElementSibling.matches('.row.me')")
    assert errors == []
