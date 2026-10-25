"""個人情報の決まり。読み取り(resume_intake)、下書き(voice_resume)、dip(dip_mcp)が同じものを使う。

- 探す: has_pii / redact_pii(PII_VALUE。電話番号とメールアドレス)。トークの発言、相談の記録、保存する値の照合に使う
- 住所: ADDRESS_LIKE / address_spans(住所らしい所)。本人の値を保存・反映するかは personal_value(電話、メール、住所)で、
  文の中で伏せるのは redact_personal(同じ 3 つ)で決める。dip の希望の保存、まとめ、フォームの続き、雇用区分の根拠が使う
- 消す: scrub_pii(SCRUB_PATTERNS。電話番号、メール、郵便番号)。Resume Draft の文字列から機械的に落とす
どちらも照合の前に fold_pii で全角の数字・ハイフンなどを半角にそろえる(1文字を1文字に写すので、位置は元の文字列と同じ)。
"""

from __future__ import annotations

import re
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
# 2つ組は市区町村名の直後で、後ろに単位(万・円・か月・年・日・時など)が続かないものだけを数える。
# 全角のハイフンは照合の前に fold_pii で半角にそろえる(personal_value / redact_personal はそうする)
ADDRESS_LIKE = re.compile(
    r"(?<![\d-])\d{1,3}[-－]\d{1,4}[-－]\d{1,4}(?![\d-])"
    r"|[市区町村]\d{1,4}[-－]\d{1,4}(?![\d万千円かカヵヶケ年月日時分歳%％])"
    r"|丁目|番地"
)
_WORD_SEP = set("、。,.!?！？「」()（）")

# Resume Draft から消すもの(メール、電話番号、郵便番号)。プロンプトで「書かない」を指示した上で、
# 出力に混ざっても機械的に落とすセーフティネット。PII_VALUE より広く取る(区切りが空白1つの電話番号、郵便番号も消す)。
# 電話番号を郵便番号より先に消す(逆だと 090-1234-5678 の前半が郵便番号として食われ、-5678 が残る)。
SCRUB_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"(?<!\d)0\d{1,4}[-()\s]?\d{1,4}[-()\s]?\d{3,4}(?!\d)"),
    re.compile(r"〒?\s*(?<!\d)\d{3}-\d{4}(?!\d)"),
]


def fold_pii(text: str) -> str:
    """個人情報の照合用に、全角の数字・ハイフンなどを半角にそろえる(文字数・位置は変えない)。"""
    return text.translate(_PII_FOLD)


def has_pii(text: str) -> bool:
    """電話番号・メールアドレスらしい値を含むか(全角・長音記号のハイフンも含めて)。"""
    return PII_VALUE.search(fold_pii(text)) is not None


def personal_value(text: str) -> bool:
    """本人の値として受け取らない、個人情報らしい値か(電話番号・メールアドレス・住所らしい所を含む)。"""
    return has_pii(text) or ADDRESS_LIKE.search(fold_pii(text)) is not None


def address_spans(text: str) -> list[tuple[int, int]]:
    """文の中の住所らしい所の範囲: ADDRESS_LIKE に当たった所を含む、区切り(空白・句読点・括弧)までの語
    (「渋谷区神南1丁目2-3に住む」なら全体。丁目だけ消して番地を残さない)。正規表現で前後の語まで取ると長い文で
    時間がかかるので、当たった所から区切りまで広げる。"""
    def sep(ch: str) -> bool:
        return ch.isspace() or ch in _WORD_SEP

    spans: list[tuple[int, int]] = []
    for m in ADDRESS_LIKE.finditer(text):
        start, end = m.span()
        if spans and start < spans[-1][1]:  # 前の語に含まれている
            continue
        while start > 0 and not sep(text[start - 1]):
            start -= 1
        while end < len(text) and not sep(text[end]):
            end += 1
        spans.append((start, end))
    return spans


def redact_pii(text: str, repl: str = "[個人情報を削除]",
               extra: Callable[[str], list[tuple[int, int]]] | None = None) -> str:
    """電話番号・メールアドレス(と extra が返す範囲)を repl に置き換える。照合は fold_pii した文字で行い、
    置き換えは元の文字列の同じ位置に対して行う(全角のまま残ったりしない)。"""
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


def redact_personal(text: str, repl: str = "[個人情報を削除]") -> str:
    """電話番号・メールアドレス・住所らしい所(区切りまでの語ごと)を repl に置き換える(personal_value と同じ範囲)。"""
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
