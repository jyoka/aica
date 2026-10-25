"""個人情報の決まり。読み取り(resume_intake)、下書き(voice_resume)、dip(dip_mcp)が同じものを使う。

- 探す: has_pii / redact_pii(PII_VALUE。電話番号とメールアドレス)。トークの発言、相談の記録、保存する値の照合に使う
- 住所: ADDRESS_LIKE / address_spans(住所らしい所。住む言葉 RESIDENCE_WORDS と同じ文のときだけ)。本人の値を保存・反映するかは personal_value(電話、メール、住所)で、
  文の中で伏せるのは redact_personal(同じ 3 つ)で決める。dip の希望の保存、まとめ、フォームの続き、雇用区分の根拠が使う
  トークの発言(line_runtime.turn.accept)も同じ 3 つを伏せる。音声相談の調べものの問い(voice_resume.advisor)は LLM が書いて外へ出るので、
  電話、メールか、住む言葉が無くても住所の形があれば断る(has_pii / looks_like_address)
- 消す: scrub_pii(SCRUB_PATTERNS。電話番号、メール、郵便番号)。Resume Draft の文字列から機械的に落とす
どちらも照合の前に fold_pii で全角の数字・ハイフンなどを半角にそろえる(1文字を1文字に写すので、位置は元の文字列と同じ)。
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable

# 項目名に関係なく、値に含まれていたら個人情報とみなすもの(電話番号・メールアドレス)。
# 電話番号は、区切り(空白、-、括弧、.)を除いた数字の並びで見る(_PHONE)。書き方を 1 つずつ足すと漏れるため。
# - 0 で始まり 10〜11 桁になる並び(「(03)1234-5678」「090 1234 5678」「090.1234.5678」)
# - +81 か、括弧の中の 81 で始まり、その後に 9〜10 桁が続く並び(「+81 90-1234-5678」「(+81)3-1234-5678」。間の「(0)」も越える)
# 区切りを除くのは、数字と区切りだけが続く間(区切りは 1 か所 3 字まで。改行は越えない)。並びのどの数から始めても、
# どこかの数の終わりで 10〜11 桁になれば当たる(前に「1 」、後ろに「 2回」が続いても)。短い方(10 桁)から試すので、
# 10 桁の番号の後ろの別の数(「(03)1234-5678 2回」の「2」)は含めない。1 つの並びに当たり方がいくつもあるとき
# (「〒150-0001 03-1234-5678」の、前からある形の「0001 03-1234」と「03-1234-5678」)、伏せるときはどれも伏せる(_PHONE_ALL。redact_pii)。
# 数字のすぐ後ろの「.」「-」からは始めない(「2001.04.01-2005.03.31」の「04.01-2005」を 0 で始まる 10 桁と読まない)。
# 単位で外すのは、空白で前と区切った別の数(4 桁まで)のすぐ後ろが単位(円・万・人・分など。_NOT_PHONE_AFTER)のときだけ
# (「090 1234 5678 3人」の「3人」は人数なので含めない。「定員 0 100 200 3000人」は電話番号にしない)。
# 番号の終わりに続く語(「(03)1234-5678 月曜」「090.1234.5678日中」「+81-90-1234-5678本人」)では外さない。単位の字で始まる語は多く、
# 単位か語かを字では見分けられないので、漏らさない側に倒す。括弧や +81 で始まる並びは電話番号の形がはっきりしているので、単位でも外さない。
# それより前からある形(区切りのある短い番号、カンマの決まり)も残す: 数字の途中の 0 から始まる並び
# (「年収3000000円」「3,000,000円」の 000000)は電話番号ではないので、前後が数字か「カンマ+数字」のものは数えない
# (「090-1234-5678,よろしく」のように、後ろのカンマに数字が続かなければ電話番号として捕まえる)。
# カンマの決まりは区切りの無い形(0\d{9,10})だけに効かせる。区切りのある形は桁の区切りと紛れないので、
# 「1,090-1234-5678」「090-1234-5678,1」のように数字とカンマに挟まれていても捕まえる。
# メールは @ の前後に英数字と . _ - + が続く並び(_MAIL)。かなや漢字は名前にもドメインにも含めない
# (「連絡はtaro@example.comで」のメールの所だけを伏せ、前後の文は残す)。ドメインに . の無いもの(「taro@example」)も当たる。
# 全角の数字・英字・ハイフン・括弧・＠(「０９０－１２３４－５６７８」「090ー1234ー5678」「090−1234−5678」「（０３）」)は、
# 照合の前に fold_pii で半角にそろえる(文字数は変えない)。探すときは has_pii / redact_pii を使う。
_NOT_PHONE_AFTER = "円万千億人名件回個本分秒時日月年歳才%"
_PHONE_SEP = r"[-().\x20\t]{0,3}"  # 数字の間の区切り(改行は越えない)
_PHONE_HEAD = r"(?<![\d+])(?<!\d,)(?<!\d[-.])"  # 数の途中からは始めない
_PHONE_TAIL = r"(?!\d|,\d)"
# 空白で区切った別の数のすぐ後ろが単位(Python の後読みは長さが決まっている必要があるので、桁数ごとに書く)
_PHONE_QTY = "(?!" + "|".join(rf"(?<=[\x20\t]\d{{{n}}})[{_NOT_PHONE_AFTER}]" for n in range(1, 5)) + ")"
_PHONE = (
    rf"{_PHONE_HEAD}\(\x20?0(?:{_PHONE_SEP}\d){{9,10}}?{_PHONE_TAIL}"
    rf"|{_PHONE_HEAD}0(?:{_PHONE_SEP}\d){{9,10}}?{_PHONE_TAIL}{_PHONE_QTY}"
    rf"|{_PHONE_HEAD}(?:\+81|\(\+?81\)?){_PHONE_SEP}(?:\(0\){_PHONE_SEP})?\d(?:{_PHONE_SEP}\d){{8,9}}?{_PHONE_TAIL}"
)
# 1 つの並びの中の当たり方をすべて拾う(先読みの中で取るので、当たった所が重なってもよい)
_PHONE_ALL = re.compile(rf"(?=({_PHONE}))")
_MAIL = r"(?<![A-Za-z0-9._+-])[A-Za-z0-9._+-]+@[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*"
PII_VALUE = re.compile(
    _PHONE
    + r"|(?<!\d)(?:0\d{1,4}[-(\s]\d{1,4}[-)\s]\d{3,4}|0\d{1,4}-\d{6,8})(?!\d)"
    r"|(?<!\d)(?<!\d,)0\d{9,10}(?!\d|,\d)"
    # メールは、名前の部分の先頭からだけ照合する(長い文で位置ごとに照合し直して時間がかからないように)
    rf"|{_MAIL}"
)
# 全角の数字・英字・記号と、ハイフンに見える文字(長音記号・マイナス・各種ダッシュ)を半角に。1文字を1文字に写す
_PII_FOLD = str.maketrans(
    {chr(0xFF10 + i): str(i) for i in range(10)}
    | dict.fromkeys("－ーｰ−‐‑‒–—―", "-")
    | {chr(0xFF21 + i): chr(0x41 + i) for i in range(26)} | {chr(0xFF41 + i): chr(0x61 + i) for i in range(26)}
    | {"（": "(", "）": ")", "＠": "@", "．": ".", "　": " ", "，": ",", "＋": "+", "＿": "_"}
)

# 住所らしい値(番地・丁目・「1-2-3」の形・地名の後ろの数の並び)。氏名はコードでは見分けられないので、Prompt で保存させない。
# 本人の文では、この形だけでは住所に数えない。同じ文に住んでいる所を言う言葉(RESIDENCE_WORDS)があるときだけ住所とみなす(_address_matches)。
# LLM が書く調べものの問いは、住む言葉が無くてもこの形で断る(looks_like_address)。
# 住む言葉と同じ文では、伏せ漏れしない側に倒す: 地名の字(漢字・カタカナ・ひらがな。「みほん町552」「大字みほん1234」
# 「あおば町12の3」「西新宿2-8」)の直後の数の並び(1〜4桁の数を「-」「の」「番」「番地」「号」「丁目」でつないだもの。数が 1 つでも)を数える。
# 外すのは、前が「週」「月」「給」「収」(「週3」「時給1200」「年収400」)か、最後の数のすぐ後ろが単位(万・円・か月・年・日・時・人・代・駅など。
# 「5-10人」「3-6か月」「20代」「2駅」「8h」)か、桁の区切り・時刻・幅の続き(「1,200円」「18:00」「4〜5時間」)のときだけ。
# 地名と数の間の空白 1 つ(「みほん町 552」)も越える。並びは 6 つの数まで(長い入力で照合し直す量を抑える)。
# 漢数字は「二番八号」の形と、「丁目」「番地」で数える(「一番近い」「三鷹」は数えない)。
# 3つ組(部屋番号まで続けた「1-2-3-405」も)は地名の後ろでなくても数える。日付(「2026-09-30」)は先頭が4桁なので数えない。
# 全角のハイフンは照合の前に fold_pii で半角にそろえる(personal_value / redact_personal はそうする)
_KANJI_NUM = "〇一二三四五六七八九十百千"
_NOT_LOT = "万千円かカヵヶケ年月日時分秒歳才%％人名件回個本社店倍点週枚代駅つ位度級kｋキhHｈ"  # 数のすぐ後ろにあれば番地ではない単位
_PLACE_CHARS = "一-鿿々ヶァ-ヺぁ-ゖ"  # 地名の字(漢字・カタカナ・ひらがな)
ADDRESS_LIKE = re.compile(
    r"(?<![\d-])\d{1,3}[-－]\d{1,4}[-－]\d{1,4}(?:[-－]\d{1,4})?(?![\d-])"
    rf"|(?:\d{{1,4}}|[{_KANJI_NUM}]{{1,4}})番(?:\d{{1,4}}|[{_KANJI_NUM}]{{1,4}})号"
    rf"|(?:(?<=[{_PLACE_CHARS}])(?<![週月給収])|(?<=[{_PLACE_CHARS}]\s)(?<![週月給収]\s))"
    rf"\d{{1,4}}(?:(?:[-－]|の|番地?の?|号の?|丁目)\d{{1,4}}){{0,5}}+(?:番地?|号)?+"
    rf"(?![\d{_NOT_LOT}]|[,，:：~〜～]\d)"
    r"|(?<![\d])\d{1,4}の\d{1,4}の\d{1,4}(?!\d)"
    r"|丁目|番地"
)
# 調べものの問い(looks_like_address)で、区切りの無い数 1 つを番地とみなす前の字: 町・村、丁目・番地の後ろ(「みほん町552」「みほん村 12」
# 「西新宿三丁目2」)と、大字・字の名前の後ろ(「大字みほん1234」「みほん町字あおば 12」)。空白 1 つを越える。
# 字は、前が「大」か市区町村郡の字か、文の始まり・空白のときだけ(「漢字」「数字」「赤字」は数えない)
_LOT_BEFORE = re.compile(r"(?:[町村]|丁目|番地|(?:^|(?<=[\s大市区町村郡]))字[一-鿿々ヶァ-ヺぁ-ゖ]{1,8})\s?$")
# 住んでいる所を言う言葉。ADDRESS_LIKE の当たりは、同じ文にこれがあるときだけ住所とみなす。
# 駅の名前(「新宿三丁目」)や求人の勤務地、数の幅(「5-10人」)は ADDRESS_LIKE と同じ形になることがあり、
# 形だけで住所と見分けると、求人の検索の条件(「新宿三丁目5分以内で探して」)が消えるため(docs/architecture/lld.md 1.8)。
# 「住み込み」は働き方なので数えない。「住宅」(住宅手当)も数えない。引っ越しの仕事(「引っ越しのバイト」「引越しスタッフ」)と、
# 職業の「家は」(「専門家は」「作家は」「画家は」「農家は」)も数えない。住まいの「一軒家は」「借家は」は数える
RESIDENCE_WORDS = re.compile(
    r"住所|住居|居住|在住|住ん|住む|住み(?!込)|住まい|すんで|すまい|自宅|実家|(?<![門作画築治業資楽論究真術農書語])家は|転居"
    r"|(?:引っ越|引越)(?!し?の?(?:バイト|スタッフ|作業|業者|屋|センター|仕事|会社))")
# 文の終わり(住む言葉を探す範囲の区切り)
_SENTENCE_END = re.compile(r"[。!?！？\n]")
# 見出しだけの行(「住所:」「【住所】」「自宅」)と、郵便番号だけの行。連絡先を貼るときは見出しと住所を別の行に書くので、
# 見出しだけの行の住む言葉は、改行の後ろの行にも効かせる(郵便番号だけの行、市区町村までの行、空の行は飛ばして、その次の行まで)。
# ふつうの文(「住所は言いたくないです」)は見出しではないので、次の行には効かせない
_LABEL_MARKS = r"\s【】\[\]「」『』()（）<>＜＞〈〉《》:：・*＊■□●○◆◇"
_LABEL_LINE = re.compile(rf"[{_LABEL_MARKS}]*(?:現|ご|お)?(?:{RESIDENCE_WORDS.pattern})+地?[{_LABEL_MARKS}]*")
_POSTAL_LINE = re.compile(rf"[{_LABEL_MARKS}]*〒?\s*\d{{3}}-\d{{4}}[{_LABEL_MARKS}]*")
# 都道府県・市区町村までの行(「東京都みほん市」)。見出しの後ろの住所を 2 行に分けて書くときは、この行も飛ばして次の行まで効かせる
_PLACE_LINE = re.compile(r"\s*[^\s。、!?！？:：]{1,30}[都道府県市区町村郡]\s*")
# 住所を左右に広げるときの決まり(address_spans)。住所の部分だけを伏せ、同じ文の検索の条件は残す
_PLACE_SUFFIX = set("都道府県市区町村郡")
# 住む言葉の後ろに続く「は」「:」など(「住所は」「自宅：」「引っ越し先は」「住んでいるのは」)。ここまでは伏せない
_RESIDENCE_TAIL = re.compile(r"(?:でい?る(?:の|所|ところ)?|し?(?:先|地)?)[はがもの:：\s]*")
# 住所が郵便番号から始まるとき(「〒100-0001 東京都…」)は、郵便番号も住所に含める
_POSTAL_BEFORE = re.compile(r"〒?\s*(?<!\d)\d{3}-\d{4}\s*$")
# 住所の前にあれば、そこで止める言葉(「時給1200円以上東京都…」)。地名には出てこないもの
_NOT_PLACE = ("以上", "以内", "時給", "日給", "月給", "年収")
# 住所の後ろのひらがなが、この字で始まれば建物の名前ではない(「に住む」「で働きたい」「の近く」「まで」「けど」「って」)。
# ただし後ろに建物の言葉(_BUILDING)が続くときは建物の名前とみなす(「のぞみハイツ」)
_PARTICLE_START = set("にでのはがをへともなかやよまだじぐくすあらけってほ")
_BUILDING = re.compile(r"\S{0,20}?(?:ハイツ|マンション|ビル|荘|コーポ|アパート|レジデンス|ハウス|ハイム|メゾン|コート|パレス|ヒルズ|"
                       r"タワー|号室|号棟|棟)")
# 住所の後ろにあれば、そこで止める言葉(「1-2-3近くのカフェ」「新宿三丁目5分以内」)
_NOT_PLACE_AFTER = re.compile(r"近く|付近|周辺|辺り|周り|駅(?!前)|徒歩|以上|以内|時給|日給|月給|年収|\d+分")
# redact_pii が置き換えをくり返す回数の上限(ふつうは 1〜2 回で当たる所が無くなる)
_REDACT_PASSES = 8
# 伏せた所に入れる文。トークの発言(line_runtime.turn)もこれで伏せ、履歴を返すときはこれで伏せたかを見る
REDACTED = "[個人情報を削除]"

# Resume Draft から消すもの(メール、電話番号、郵便番号)。プロンプトで「書かない」を指示した上で、
# 出力に混ざっても機械的に落とすセーフティネット。PII_VALUE より広く取る(区切りが空白1つの電話番号、郵便番号も消す)。
# 電話番号を郵便番号より先に消す(逆だと 090-1234-5678 の前半が郵便番号として食われ、-5678 が残る)。
# 電話番号とメールは PII_VALUE の形(_PHONE、_MAIL)も消す(has_pii が見つけるものより残るものが無いように)。
# メールは _MAIL だけにする(かなや漢字を含めると、前後の文まで消える)。電話番号は前からある形の後に当てる。
# 前からある電話番号の形は、前の括弧も消す(「(03)1234-5678」の「(」を残さない)
SCRUB_PATTERNS = [
    re.compile(_MAIL),
    re.compile(r"(?<!\d)\(?0\d{1,4}[-()\s]?\d{1,4}[-()\s]?\d{3,4}(?!\d)"),
    re.compile(_PHONE),
    re.compile(r"〒?\s*(?<!\d)\d{3}-\d{4}(?!\d)"),
]


def _address_matches(text: str) -> list[re.Match]:
    """ADDRESS_LIKE に当たった所のうち、住んでいる所を言う言葉(RESIDENCE_WORDS)と同じ文にあるもの。
    見出しだけの行(_LABEL_LINE)の後ろの行も、住む言葉と同じ文とみなす(「住所:\nみほん市みほん町1-2-3」)。
    文ごとに住む言葉があるかを先に決め、当たった所の文は二分探索で引く(長い文でも時間がかからない)。"""
    ends = [m.end() for m in _SENTENCE_END.finditer(text)] + [len(text)]
    lives: list[bool] = []
    carry = False  # 前の行が見出しだけの行(またはその後ろの郵便番号だけの行、空の行、市区町村までの行)
    for start, end in zip([0, *ends[:-1]], ends):
        line_end = text[end - 1:end] == "\n"
        body = text[start:end - 1] if line_end else text[start:end]
        live = carry or RESIDENCE_WORDS.search(body) is not None
        lives.append(live)
        carry = line_end and (
            (live and (_LABEL_LINE.fullmatch(body) is not None or _POSTAL_LINE.fullmatch(body) is not None))
            or (carry and (not body.strip() or _PLACE_LINE.fullmatch(body) is not None)))
    if not any(lives):
        return []
    return [m for m in ADDRESS_LIKE.finditer(text) if lives[bisect_right(ends, m.start())]]


def fold_pii(text: str) -> str:
    """個人情報の照合用に、全角の数字・ハイフンなどを半角にそろえる(文字数・位置は変えない)。"""
    return text.translate(_PII_FOLD)


def has_pii(text: str) -> bool:
    """電話番号・メールアドレスらしい値を含むか(全角・長音記号のハイフンも含めて)。"""
    return PII_VALUE.search(fold_pii(text)) is not None


def personal_value(text: str) -> bool:
    """本人の値として受け取らない、個人情報らしい値か(電話番号・メールアドレスか、住む言葉と一緒の住所らしい所を含む)。"""
    return has_pii(text) or bool(_address_matches(fold_pii(text)))


def looks_like_address(text: str) -> bool:
    """住む言葉が無くても、番地まである住所の形(ADDRESS_LIKE)を含むか。外へ出る調べものの問い(LLM が書く)を断るのに使う。
    住む言葉の決まりは本人の文の中で検索の条件を残すためのもので、LLM が書く問いには当てはまらない(docs/architecture/lld.md 1.8)。
    番地の無い地名は数えない: 「丁目」「番地」の言葉だけの当たり(「新宿三丁目のカフェ」)と、丁目までの数(「西新宿1丁目」)。
    区切りの無い数 1 つ(「みほん町552」)は、すぐ前が町名の終わり(_LOT_BEFORE)のときだけ数える。問いはキーワードを空白で区切るので、
    言葉の後ろの年や件数(「最低賃金 2026」「おすすめ 10選」「東京23区」「ローソンストア100」)と紛れるため。"""
    folded = fold_pii(text)
    for m in ADDRESS_LIKE.finditer(folded):
        if m.group() in ("丁目", "番地"):
            continue
        if m.group().isdigit() and (
                folded.startswith("丁目", m.end()) or not _LOT_BEFORE.search(folded, max(0, m.start() - 12), m.start())):
            continue
        return True
    return False


def address_spans(text: str) -> list[tuple[int, int]]:
    """文の中の住所らしい所の範囲: 住んでいる所を言う言葉と同じ文で ADDRESS_LIKE に当たった所を、住所の部分だけに広げたもの。
    左は地名(都道府県・市区町村・町名)まで、右は番地・号・建物まで(「渋谷区神南1丁目2-3に住む」なら「渋谷区神南1丁目2-3」。
    丁目だけ消して番地を残さない)。文の残り(「に住んでいて」「近くのカフェで働きたい」)は残す(docs/architecture/lld.md 1.8)。
    正規表現で前後の語まで取ると長い文で時間がかかるので、当たった所から 1 文字ずつ広げる。"""
    matches = _address_matches(text)
    if not matches:
        return []
    # 住む言葉(と後ろの「は」「:」など)の終わり。左に広げるのはここまで(「自宅はみほん市3-5」の「自宅は」は残す)
    lives = [_RESIDENCE_TAIL.match(text, m.end()).end() for m in RESIDENCE_WORDS.finditer(text)]
    spans: list[tuple[int, int]] = []
    for m in matches:
        start, end = m.span()
        if spans and start < spans[-1][1]:  # 前の住所に含まれている
            if end > spans[-1][1]:  # はみ出す(「4丁目2の8」の「2の8」): 前の住所の終わりを延ばす
                spans[-1] = (spans[-1][0], _address_end(text, end))
            continue
        i = bisect_right(lives, start)
        word = lives[i - 1] if i else -1
        floor = max(spans[-1][1] if spans else 0, word, 0)
        if start - 1 > floor and text[start - 1].isspace() and _is_name_char(text[start - 2]):
            start -= 1  # 地名と番地の間の空白(「大字みほん 1234」)。地名もさかのぼる
        spans.append((_address_start(text, start, floor, word), _address_end(text, end)))
    return spans


def _address_start(text: str, start: int, floor: int, word: int = -1) -> int:
    """住所の始まり: 漢字・カタカナ・数字の地名をさかのぼる。ひらがなは地名の一部のときだけ含める: 後ろが都道府県・市区町村の字
    (「みほん町」「さいたま市」)、助詞で終わらずに漢字が続く(「みほん台」)、前が都道府県・市区町村の字
    (「みほん市あおば台」「横浜市西区みなとみらい」)、漢字に挟まれた 2 字まで(「霞が関」)。
    前が都道府県・市区町村の字か、漢字に挟まれた 2 字まででなければ、そこで止める
    (「以上でさいたま市」の「以上」は残す。「で」は含まれる)。市区町村の後ろの空白か改行 1 つ(「東京都 みほん市」)も越える。
    word は住む言葉と後ろの「は」「:」の終わり。ひらがなの町名がそこからすぐ始まるときは、助詞の字で終わっても含める
    (「住所はみなと台3-5-1」「住所はひばりが丘3丁目」)。番地の直前のひらがなの町名(「大字みほん1234-5」)と、
    地名に続くひらがなの前の漢字(「大字」)も含める。前に郵便番号があれば、それも含める(「〒100-0001 東京都…」)。"""
    while start > floor:
        ch = text[start - 1]
        if _is_hiragana(ch):
            run = start
            while run > floor and _is_hiragana(text[run - 1]):
                run -= 1
            before = text[run - 1] if run > floor else ""
            between = start - run <= 2 and _is_kanji(before) and _is_kanji(text[start])
            goes_on = before in _PLACE_SUFFIX or between or (before.isspace() and text[run - 2:run - 1] in _PLACE_SUFFIX)
            after_word = run == word and word > 0 and (text[word - 1] in "はがもの:：" or text[word - 1].isspace())
            head = text[start + 1] if text[start].isspace() else text[start]  # 空白の後ろの番地(「みほん 1234」)
            name = head in _PLACE_SUFFIX or (
                (_is_kanji(head) or head.isdigit()) and (ch not in _PARTICLE_START or after_word))
            if not (goes_on or name):
                break  # 地名の一部ではない(「を新宿三丁目」の「を」)
            start = run
            if not (goes_on or _is_kanji(before)):
                break
            continue
        if ch.isspace() and start - 1 > floor and text[start - 2] in _PLACE_SUFFIX and not text[start - 2].isspace():
            start -= 1
            continue
        if not _is_place_char(ch) or text.endswith(_NOT_PLACE, 0, start):
            break
        start -= 1
    postal = _POSTAL_BEFORE.search(text, max(floor, start - 12), start)
    return postal.start() if postal else start


def _address_end(text: str, end: int) -> int:
    """住所の終わり: 番地・号・建物の名前(漢字・カタカナ・数字・英字)まで。助詞などのひらがな(「に住んで」「で働きたい」)、
    住む言葉、検索の条件の言葉(「近く」「時給」「5分」など)で止める。ひらがなは、後ろに漢字・カタカナ・数字が続き、
    助詞で始まらないか建物の言葉が続くとき(「1-2-3みほんビル101号室」「1-2-3のぞみハイツ」)だけ、建物の名前として含める。
    「123番地の4」の「の4」と、空白か改行 1 つの後ろの建物(「1-2-3 みほんハイツ101号室」)も含める。"""
    while end < len(text):
        ch = text[end]
        if ch == "の" and text[end + 1:end + 2].isdigit() and text[end - 1:end] in ("地", "目", "番"):
            end += 1
            continue
        if ch.isspace():
            if end + 1 < len(text) and not text[end + 1].isspace() and _BUILDING.match(text, end + 1):
                end += 1
                continue
            break
        if _is_hiragana(ch):
            run = end
            while run < len(text) and _is_hiragana(text[run]):
                run += 1
            if run == len(text) or not _is_place_char(text[run]):
                break
            if ch in _PARTICLE_START and not _BUILDING.match(text, run):
                break
            end = run
            continue
        if not _is_place_char(ch) or _NOT_PLACE_AFTER.match(text, end) or RESIDENCE_WORDS.match(text, end):
            break
        end += 1
    return end


def _is_hiragana(ch: str) -> bool:
    return "ぁ" <= ch <= "ゖ"


def _is_kanji(ch: str) -> bool:
    return ch != "" and ("一" <= ch <= "鿿" or ch in "々ヶ")  # 文の始まりの前("")は漢字ではない


def _is_name_char(ch: str) -> bool:
    """地名の字(ADDRESS_LIKE の _PLACE_CHARS と同じ。漢字、カタカナ、ひらがな)。"""
    return _is_kanji(ch) or _is_hiragana(ch) or "ァ" <= ch <= "ヺ"


def _is_place_char(ch: str) -> bool:
    """地名・番地・建物の名前に使う字(漢字、カタカナ、数字、英字。全角・半角とも)。ひらがなは呼ぶ側で別に見る。"""
    return ch.isalnum() or ch in "ー々〆ヶ-・"


def redact_pii(text: str, repl: str = REDACTED,
               extra: Callable[[str], list[tuple[int, int]]] | None = None) -> str:
    """電話番号・メールアドレス(と extra が返す範囲)を repl に置き換える。照合は fold_pii した文字で行い、
    置き換えは元の文字列の同じ位置に対して行う(全角のまま残ったりしない)。
    置き換えた後にまた当たる所が出なくなるまでくり返す(「090-1234-5678.taro@example.com」は、電話番号を先に取ると
    メールが前の文字に続いて見えて当たらず、置き換えた後に当たる)。くり返すたびに元の文字が減るので止まるが、
    repl そのものが当たる形でも止まるよう、回数に上限(_REDACT_PASSES)を置く。"""
    for _ in range(_REDACT_PASSES):
        once = _redact_once(text, repl, extra)
        if once == text:
            break
        text = once
    return text


def _redact_once(text: str, repl: str, extra: Callable[[str], list[tuple[int, int]]] | None) -> str:
    folded = fold_pii(text)
    spans = [m.span() for m in PII_VALUE.finditer(folded)] + [m.span(1) for m in _PHONE_ALL.finditer(folded)]
    if extra is not None:
        spans += extra(folded)
    out, last = [], 0
    for start, end in sorted(spans):
        if start < last:  # 重なった分は前の置き換えに含める
            last = max(last, end)
            continue
        out += [text[last:start], repl]
        last = end
    return "".join(out) + text[last:]


def redact_personal(text: str, repl: str = REDACTED) -> str:
    """電話番号・メールアドレス・住所らしい所(地名から番地までの住所の部分)を repl に置き換える(personal_value と同じ範囲)。"""
    return redact_pii(text, repl, extra=address_spans)


def scrub_pii(value):
    """文字列・リスト・辞書の中の電話番号/メール/郵便番号を機械的に消す。空になった文字列はリストから落とす。
    SCRUB_PATTERNS を順に当てて消すのを、元の文字列と fold_pii した文字の両方で行い、どちらかで消えた位置を
    元の文字列から消す(全角で書いたものも消え、元の文字列だけで照合したときより残るものが増えない)。
    元の文字列でも照合するのは、全角の「０１」などが fold_pii で電話番号の始まりに見え、すぐ後ろの半角の電話番号を
    途中までしか取れなくなる(「No.０１ 090-1234-5678」が「No.-5678」になる)のを防ぐため。"""
    if isinstance(value, str):
        removed = _scrubbed_positions(value) | _scrubbed_positions(fold_pii(value))
        return "".join(c for i, c in enumerate(value) if i not in removed).strip()
    if isinstance(value, list):
        cleaned = [scrub_pii(v) for v in value]
        return [v for v in cleaned if v != ""]
    if isinstance(value, dict):
        return {k: scrub_pii(v) for k, v in value.items()}
    return value


def _scrubbed_positions(text: str) -> set[int]:
    """SCRUB_PATTERNS を順に当てて消したとき(前のパターンで消した残りに次を当てる)、消える text の位置。"""
    kept = list(range(len(text)))
    for pat in SCRUB_PATTERNS:
        current = "".join(text[i] for i in kept)
        cut = {i for m in pat.finditer(current) for i in range(*m.span())}
        kept = [pos for i, pos in enumerate(kept) if i not in cut]
    return set(range(len(text))) - set(kept)
