"""本人の同意(docs/design/privacy.md の 3.6。task 214)。

同意は 3 つ。
- terms: 使い始める(利用規約とプライバシーポリシー、外国にある事業者への提供)。Web は最初にトークを開いたとき、LINE は最初の発言の前か、
  LIFF の画面(音声相談、履歴書の読み込み、ゲーム)を最初に開いたとき(task 239。音声と履歴書は、それぞれの同意と 1 つの画面で示す)
- resume: 履歴書のファイルを送る直前
- voice: 音声相談を始める直前

記録は dip の状態(state.json)の本人の行の consents に、{kind: {"version": {"terms": "1.0", "privacy": "1.0"}, "at": ISO の時刻}}。
版は line_runtime.legal の TERMS_VERSION と PRIVACY_VERSION。どちらかが上がったら 3 つとも取り直す(記録の版が今の版と違えば、
同意していない扱い)。同意は人(匿名ID、LINE のユーザーID)ごとに 1 つで、/dev のシナリオユーザー(「匿名ID:シナリオユーザー」)は
匿名IDの行の同意を見る。画面と LINE は版を tag()(例 "terms=1.0;privacy=1.0")で送り返す。示した版が今の版と違えば記録しない
(示していない文言への同意を残さない)。
同意する前は、LLM を呼ばず、本人のデータを足さない(この記録が、その人の行に最初に書くもの。見る口と消す口は同意の前も使える)。
「すべて消す」と、使われなくなった人を
期間で消す掃除では、行と一緒に消える(次に使うときに取り直す)。
画面と LINE に出す文言は、ここに 1 か所だけ置く。FastAPI に依存しない。
"""

from __future__ import annotations

from dip_mcp import retention
from dip_mcp.data import State
from line_runtime import config, legal
from resume_intake import config as resume_config
from voice_resume import config as voice_config

KINDS = ("terms", "resume", "voice")
KEY = "consents"

# ----------------------------- 文言(設計の 3.6 の下書きを、版 1.0 の規約とポリシーに合わせたもの) -----------------------------

TERMS_INTRO = "aica は、AI がお仕事探しの相談にのるサービスです。始める前に、次のことをご確認ください。"
# 消し方の 1 行だけ、Web と LINE で変える(LINE の人はプロフィール画面を開けないため。設計の 3.7)
FORGET_POINT = {"web": "プロフィール画面の「すべて消す」で、いつでも消せます",
                "line": "トークで「データを消す」と送れば、いつでも消せます"}


# 書いた内容の送り先(LINE_SIM_PROVIDER。プライバシーポリシーの 5 の表は両方を書いている)
SENT_TO = {"anthropic": ("aica の AI には、米国の Anthropic が作る Claude を使っています。書いた内容は、返事を作るために Anthropic に送ります。求人を探す言葉は、米国の OpenAI にも送ります。"
                         "どちらも学習には使わないとされています"),
           "openai": "書いた内容は、返事を作るために米国の OpenAI に送ります。学習には使わないとされています"}


# 履歴書と音声相談の送り先(RESUME_INTAKE_PROVIDER、VOICE_RESUME_PROVIDER。どちらも既定は anthropic。task 236)
COMPANY = {"anthropic": "Anthropic", "openai": "OpenAI"}


def _company(provider: str) -> str:
    return COMPANY.get(provider, COMPANY["anthropic"])


def _terms_points(channel: str) -> list[str]:
    return [
        legal.DEMO_NOTE.removesuffix("。"),
        SENT_TO.get(config.PROVIDER, SENT_TO["anthropic"]),
        # Render の Singapore のリージョンで動かす(render.yaml。プライバシーポリシー 1.4)
        "aica は米国の Render のサーバー(シンガポールにあります)で動き、情報もそこに保存します",
        ("米国には連邦の包括的な個人情報保護法がなく、分野ごと、州ごとの法律があります。シンガポールには個人データ保護法があります。"
         "各事業者の学習への利用と保持は、"
         "プライバシーポリシーの「5」に書いています"),
        "会話は最後のやりとりから 30 日、希望などのプロフィールは最後のご利用から 180 日で消えます",
        FORGET_POINT[channel],
        "名前、住所、電話番号、病気のことなどは書かないでください。トークに書いた電話番号などは、送る前に自動で伏せます",
        "18 歳未満の方は、保護者の同意を得てから使ってください",
    ]


def texts(channel: str = "web") -> dict[str, dict]:
    """同意の画面の文言。kind → {title, paragraphs, points, strong, button}。channel は web か line。"""
    return {
        "terms": {"title": "aica を始める前に", "paragraphs": [TERMS_INTRO], "points": _terms_points(channel),
                  "strong": "", "button": "同意して始める"},
        "resume": {"title": "履歴書・職務経歴書を送る前に", "paragraphs": [
            (f"履歴書・職務経歴書は、AI が読み取ります。そのために、ファイルを米国の {_company(resume_config.PROVIDER)} に送ります。"
             f"{_company(resume_config.PROVIDER)} は学習には使わないとされています。ファイルそのものは aica に保存しません。"),
            "読み取った経歴(学歴、職歴、資格など)は、プロフィールに保存し、相談に使います。"],
            "points": [], "strong": "名前、住所、写真の欄は、隠すか消してからアップロードしてください。",
            "button": "同意してファイルを選ぶ"},
        "voice": {"title": "音声相談を始める前に", "paragraphs": [
            "声は、米国の LiveKit を通り、米国の Deepgram で文字にします。AI の声は米国の Cartesia で作ります。",
            (f"AI が返事と下書きを作るために、文字にした内容を米国の {_company(voice_config.PROVIDER)} に送ります。"
             f"{_company(voice_config.PROVIDER)} は学習には使わないとされています。録音は保存しません。"),
            "名前、住所などは話さないでください。話した電話番号などは、自動では伏せません。"],
            "points": [], "strong": "", "button": "同意して始める"},
    }


def links() -> list[dict]:
    """同意の画面に出す、規約とポリシーへのリンク(画面の URL)。"""
    return [{"label": title, "url": legal.URLS[name]} for name, (title, _, _) in legal.DOCS.items()]


# 同意がまだなのに使おうとしたとき(API の 403 の文)
MISSING = {
    "terms": "始める前の同意がまだです。トーク画面を開き直して、「同意して始める」を押してください。",
    "resume": "履歴書を送る前の同意がまだです。「同意してファイルを選ぶ」を押してから選んでください。",
    "voice": "音声相談の前の同意がまだです。「同意して始める」を押してから始めてください。",
}
MISSING_LINE_TERMS = "始める前の同意がまだです。この画面を開き直すか、LINE のトークで「同意して始める」を押してから使ってください。"
STALE = "同意の内容が新しくなりました。もう一度ご確認ください。"


# ----------------------------- 版と記録 -----------------------------


def versions() -> dict[str, str]:
    """今の版(記録に残す値)。"""
    return legal.versions()


def tag(v: dict[str, str] | None = None) -> str:
    """画面と LINE に渡し、同意のときに送り返してもらう版の文字列(例 "terms=1.0;privacy=1.0")。"""
    return ";".join(f"{k}={x}" for k, x in sorted((v or versions()).items()))


def _row(user_id: str) -> str:
    """同意を記録する行(人ごとに 1 つ。シナリオユーザーは匿名IDの行)。"""
    return retention.person(user_id)


def needed(state: State, user_id: str, kind: str = "terms") -> bool:
    """その人が kind に、今の版でまだ同意していないか。"""
    rec = (state.user(_row(user_id)).get(KEY) or {}).get(kind)
    return not (isinstance(rec, dict) and rec.get("version") == versions())


def missing(state: State, user_id: str, *kinds: str) -> str | None:
    """kinds のうち、まだ同意していない最初のもの(無ければ None)。"""
    return next((k for k in kinds if needed(state, user_id, k)), None)


def record(state: State, user_id: str, kind: str, shown: str) -> bool:
    """本人が「同意して…」を押した: shown(画面が示した版の tag)が今の版なら記録して True。違えば記録せずに False。"""
    if kind not in KINDS:
        raise ValueError(f"kind は {list(KINDS)} のいずれか")
    if shown != tag():
        return False
    entry = {"version": versions(), "at": retention.clock().isoformat(timespec="seconds")}
    state.update(_row(user_id), lambda u: u.setdefault(KEY, {}).__setitem__(kind, entry))
    retention.touch(state, _row(user_id))  # 同意したのも最後の利用(行ができたので、期間で消す掃除が数える)
    return True


def view(state: State, user_id: str, channel: str = "web") -> dict:
    """画面に渡す同意の状態と文言: {version, needed: [まだの kind…], texts, links}。"""
    return {"version": tag(), "needed": [k for k in KINDS if needed(state, user_id, k)], "texts": texts(channel),
            "links": links()}


def line_text(base_url: str) -> str:
    """LINE の友だち追加と、同意の前の発言に返す文(要点と、規約とポリシーの URL)。base_url が無ければ URL を付けない。"""
    t = texts("line")["terms"]
    lines = [*t["paragraphs"], "", *(f"・{p}" for p in t["points"])]
    if base_url:
        lines += ["", *(f"{x['label']}: {base_url}{x['url']}" for x in links())]
    lines += ["", f"よろしければ、下の「{t['button']}」を押してください。"]
    return "\n".join(lines)
