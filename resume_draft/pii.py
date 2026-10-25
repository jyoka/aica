"""個人情報の決まり。読み取り(resume_intake)、下書き(voice_resume)、dip(dip_mcp)が同じものを使う。

- 探す: has_pii / redact_pii(PII_VALUE。電話番号とメールアドレス)。トークの発言、相談の記録、保存する値の照合に使う
- 住所: ADDRESS_LIKE / address_spans(住所らしい所。住む言葉 RESIDENCE_WORDS と同じ文のときだけ)。本人の値を保存・反映するかは personal_value(電話、メール、住所)で、
  文の中で伏せるのは redact_personal(同じ 3 つ)で決める。dip の希望の保存、まとめ、フォームの続き、雇用区分の根拠が使う
  トークの発言(line_runtime.turn.accept)と、音声相談の調べものの問い(voice_resume.advisor)も同じ 3 つを伏せる・断る
- 消す: scrub_pii(SCRUB_PATTERNS。電話番号、メール、郵便番号)。Resume Draft の文字列から機械的に落とす
どちらも照合の前に fold_pii で全角の数字・ハイフンなどを半角にそろえる(1文字を1文字に写すので、位置は元の文字列と同じ)。
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable

# 項目名に関係なく、値に含まれていたら個人情報とみなすもの(電話番号・メールアドレス)。
# 電話番号は、区切り(- や空白・括弧)のある形か、0 で始まる10〜11桁だけ。数字の途中の 0 から始まる並び
# (「年収3000000円」「3,000,000円」の 000000)は電話番号ではないので、前後が数字か「カンマ+数字」のものは数えない
# (「090-1234-5678,よろしく」のように、後ろのカンマに数字が続かなければ電話番号として捕まえる)。
# カンマの決まりは区切りの無い形(0\d{9,10})だけに効かせる。区切りのある形は桁の区切りと紛れないので、
# 「1,090-1234-5678」「090-1234-5678,1」のように数字とカンマに挟まれていても捕まえる。
# 全角の数字・ハイフン(「０９０－１２３４－５６７８」「090ー1234ー5678」「090−1234−5678」)は、
# 照合の前に fold_pii で半角にそろえる(文字数は変えない)。探すときは has_pii / redact_pii を使う。
PII_VALUE = re.compile(
    r"(?<!\d)(?:0\d{1,4}[-(\s]\d{1,4}[-)\s]\d{3,4}|0\d{1,4}-\d{6,8})(?!\d)"
    r"|(?<!\d)(?<!\d,)0\d{9,10}(?!\d|,\d)"
    # メールは、名前の部分の先頭からだけ照合する(長い文で位置ごとに照合し直して時間がかからないように)
    r"|(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
)
# 全角の数字・記号と、ハイフンに見える文字(長音記号・マイナス・各種ダッシュ)を半角に。1文字を1文字に写す
_PII_FOLD = str.maketrans(
    {chr(0xFF10 + i): str(i) for i in range(10)}
    | dict.fromkeys("－ーｰ−‐‑‒–—―", "-")
    | {"（": "(", "）": ")", "＠": "@", "．": ".", "　": " ", "，": ","}
)

# 住所らしい値(番地・丁目・「1-2-3」の形・「みほん町3-5」の形)。氏名はコードでは見分けられないので、Prompt で保存させない。
# 幅や日付(「年収400-500万円」「3-6か月以内」「2026-09-30に入社」)は住所ではない: 3つ組は先頭が1〜3桁のものだけ、
# 2つ組は地名の字(漢字・カタカナ・ひらがな。「西新宿2-8」「大字みほん1234-5」)の直後で、前が「週」「月」「給」「収」でなく、
# 後ろに単位(万・円・か月・年・日・時・人など)が続かないものだけを数える。
# 部屋番号まで続けた形(「1-2-3-405」)と、「2番3号」「4の5の6」の形も数える。
# 全角のハイフンは照合の前に fold_pii で半角にそろえる(personal_value / redact_personal はそうする)。
# この形だけでは住所に数えない。同じ文に住んでいる所を言う言葉(RESIDENCE_WORDS)があるときだけ住所とみなす(_address_matches)
ADDRESS_LIKE = re.compile(
    r"(?<![\d-])\d{1,3}[-－]\d{1,4}[-－]\d{1,4}(?:[-－]\d{1,4})?(?![\d-])"
    r"|\d{1,4}番\d{1,4}号"
    r"|(?<=[一-鿿々ヶァ-ヺぁ-ゖ])(?<![週月給収])\d{1,4}[-－]\d{1,4}(?![\d万千円かカヵヶケ年月日時分秒歳才%％人名件回個本社店倍点週枚])"
    r"|(?<![\d])\d{1,4}の\d{1,4}の\d{1,4}(?!\d)"
    r"|丁目|番地"
)
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
SCRUB_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"(?<!\d)0\d{1,4}[-()\s]?\d{1,4}[-()\s]?\d{3,4}(?!\d)"),
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
            continue
        i = bisect_right(lives, start)
        word = lives[i - 1] if i else -1
        floor = max(spans[-1][1] if spans else 0, word, 0)
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
            name = text[start] in _PLACE_SUFFIX or (
                (_is_kanji(text[start]) or text[start].isdigit()) and (ch not in _PARTICLE_START or after_word))
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
    spans = [m.span() for m in PII_VALUE.finditer(folded)]
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
