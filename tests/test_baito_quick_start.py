"""アルバイトの相談を 1 タップで始める(設計メモ docs/design/baito-quick-start.md §3〜§5・§9、task 265)。実 API なし。

- ランタイムを通して(偽のモデル、dip の MCP は本物): 新しい相談は入口のカード(絵 4 枚と「どれもピンと来ない」)で始まり、
  1 タップで職種の結果(3 つとタグ、[近くで探す] [ほかのタイプも見る])が出る。[ほかのタイプも見る] で入口に戻り、
  [近くで探す] で今の流れ(icebreak)に入る。LINE は Flex(暗い色、static の PNG)、Web のトークは文とボタン
- 職種(ハーネス): 年齢が不明なので未成年として扱い、18 歳からの職種と重い作業を出さない。「動いてたい」と「しゃべるの好き」は
  同じ 3 つにならない。タグは §5 の順で、カタログの値だけから選ぶ。「どれもピンと来ない」は「ちょっとだけ」の 3 つ
- 途中の相談(入口のカードの前に始めた State)は、今の流れのまま続く
- Flex は LINE の上限に収まる
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from dip_mcp import logic
from dip_mcp.baito import harness as H
from dip_mcp.baito import session
from dip_mcp.data import State
from line_runtime import baito_cards as C
from line_runtime import config
from line_runtime.line_channel import to_messages
from tests.test_baito_chat import GREET, START, UID, FakeBaitoLLM, _harness
from tests.test_consult import ConsultModel

QM = H.qmap(H.BANK)
ENTRY_CHOICES = [*H.QUICK_PICKS, H.QUICK_NONE]
BASE = "https://aica.example"
ICONS = config.ROOT / "line_runtime" / "static" / "baito"


@pytest.fixture(autouse=True)
def harness_on(monkeypatch):
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v4")


class Quiet:
    """ハーネスの llm(同期)。言い方は「いいね！」と質問の原文。抽出は呼ばれたら落とす(入口ではモデルに読ませない)。"""

    def __init__(self):
        self.calls = []

    def extract(self, payload):
        raise AssertionError("入口のカードと職種の結果では、抽出のモデルを呼ばない")

    def generate(self, v):
        self.calls.append(v["mode"])
        return json.dumps({"reply": "いいね！" + v["text"]}, ensure_ascii=False)


def picked(pick: str) -> tuple[H.State, dict]:
    st = H.new_state(quick=True)
    H.start(st, H.BANK)
    return st, H.turn(st, pick, H.BANK, Quiet())


def names(r: dict) -> list[str]:
    return [j["name"] for j in r["view"]["quick"]["jobs"]]


# ----------------------------- ランタイムを通して(偽のモデル) -----------------------------


def test_a_new_consultation_starts_with_the_entry_card_and_one_tap_shows_jobs(tmp_path):
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    baito = FakeBaitoLLM([{"reply": "黙々派ね。" + H.QUICK_RESULT_TEXT}])
    steps = [(UID, "visit", None), (UID, "message", "まず相談する"), (UID, "message", "ひとりで黙々"),
             (UID, "message", H.QUICK_OTHER), (UID, "message", "動いてたい"), (UID, "message", H.QUICK_NEAR)]

    async def go():
        async with LineRuntime(model=ConsultModel(script=[GREET, *START]), in_memory=True, baito_llm=baito) as rt:
            out = []
            for s in steps:
                out.append(await rt.handle(*s))
                if s[2] == H.QUICK_OTHER:
                    out.append(_harness())  # 選び直す前の状態(入口の答えを外した)
            return out, await rt.conversation(UID)

    (_, r1, r2, r3, again, r4, r5), conv = asyncio.run(go())
    # タップ 0: トークの AI が相談を始め、入口のカード(絵 4 枚と下のボタン)。選択肢はハーネスのもの
    card = r1["baito_card"]
    assert card["kind"] == "entry" and card["title"] == H.QUICK_TEXT
    assert [(p["label"], p["icon"]) for p in card["picks"]] == [("しゃべるの好き", "talk"), ("動いてたい", "move"),
                                                               ("ひとりで黙々", "solo"), ("座ってじっくり", "sit")]
    assert card["other"] == H.QUICK_NONE and r1["quick_replies"] == ENTRY_CHOICES
    assert r1["tools"][1]["result"]["note"] == session.QUICK_START_NOTE
    # タップ 1: 職種の結果。職種とタグはコードが決め、モデルはリアクションの 1 文(言い方の 1 回だけ。抽出は呼ばない)
    assert r2["bubbles"] == ["黙々派ね。" + H.QUICK_RESULT_TEXT]
    assert r2["baito"] == {"phase": H.QUICK_RESULT, "question": H.QUICK_RESULT}
    res = r2["baito_card"]
    assert res["kind"] == "result" and res["title"] == "ひとりで黙々" and res["icon"] == "solo"
    assert [(j["name"], j["tag"]) for j in res["jobs"]] == [("スーパーの品出し", "接客少なめ"), ("飲食店のキッチン", "接客少なめ"),
                                                           ("データ入力", "在宅")]
    assert r2["quick_replies"] == [H.QUICK_NEAR, H.QUICK_OTHER] and "progress" not in r2 and r2["cards"] == []
    # モデルは言い方だけ(黙々・動いてたいの結果と、近くで探すの最初の質問の 3 回。抽出と [ほかのタイプも見る] では呼ばない)
    assert [c[0] for c in baito.calls] == ["say"] * 3 and H.QUICK_RESULT_TEXT in baito.calls[0][1]
    # [ほかのタイプも見る]: 入口のカードに戻る(モデルを呼ばない)。入口の答えは外す
    assert r3["baito_card"]["kind"] == "entry" and r3["bubbles"] == [H.QUICK_AGAIN_TEXT]
    assert again.phase == H.QUICK_PICK and again.slots == {}
    assert r4["baito_card"]["title"] == "動いてたい" and len(r4["baito_card"]["jobs"]) == 3
    # [近くで探す]: 今の流れ(icebreak)の最初の質問。カードは付かない
    assert r5["baito"] == {"phase": "icebreak", "question": "q_money"} and r5["quick_replies"] == QM["q_money"]["choices"]
    assert "baito_card" not in r5
    h = _harness()
    assert h.slots == {"physical": {"value": "standing_ok", "confidence": "low", "from": "choice:quick_pick"}}
    assert h.turns == 0 and h.phase_turns == {}  # 入口の 4 タップは 15 ターンに数えない
    # 画面の描き直しにも、同じカードが残る
    assert [t["bot"].get("baito_card") for t in conv["turns"][1:]] == [r1["baito_card"], r2["baito_card"], r3["baito_card"],
                                                                      r4["baito_card"], None]
    # LINE: 吹き出しの後ろにカードの Flex(ボタンはカードの中。クイックリプライは付けない)。Web のトークは文の形
    for r in (r1, r2):
        msgs = to_messages(r, base_url=BASE, features=())
        assert [m["type"] for m in msgs] == ["text", "flex"] and all("quickReply" not in m for m in msgs)
        assert msgs[1] == C.flex(r["baito_card"], BASE) and r["baito_card"]["text"] == C.text(r["baito_card"])
    assert "・スーパーの品出し：接客少なめ" in r2["baito_card"]["text"]


def test_the_web_talk_draws_the_card_as_text_and_buttons():
    core = (config.ROOT / "line_runtime" / "static" / "talk-core.js").read_text(encoding="utf-8")
    assert "if (r.baito_card?.text) bubbles.push(r.baito_card.text);" in core
    _, r = picked(H.QUICK_NONE)
    text = C.text(r["view"]["quick"])
    assert text.splitlines() == [f"【{H.LIGHT_TITLE}】{H.LIGHT_LEAD}", "・イベントスタッフ（案内・誘導）：1日だけOK",
                                 "・データ入力：在宅", "・ポスティング：1日だけOK"]
    assert C.text(H.new_state(quick=True).current["card"]) == f"{H.QUICK_TEXT}\n{H.QUICK_LEAD}"


# ----------------------------- 職種とタグ(ハーネス) -----------------------------


@pytest.mark.parametrize("pick", ENTRY_CHOICES)
def test_with_the_age_unknown_no_adult_only_or_heavy_job_is_shown(pick):
    st, r = picked(pick)
    assert "age_band" not in st.slots and H.minor_mode(st)
    jobs = [H.CATALOG_BY_ID[j["job_id"]] for j in r["view"]["quick"]["jobs"]]
    assert len(jobs) == 3
    assert all(j["min_age"] < 18 and j["phys"] != "heavy" and not j["night_only"] for j in jobs), [j["id"] for j in jobs]


def test_the_rule_is_what_keeps_adult_only_and_heavy_jobs_out():
    """同じ入口でも、18 歳以上と分かっていれば出る職種がある(決まりが効いていることの確かめ)。"""
    st = H.new_state(quick=True)
    st.slots["age_band"] = {"value": "18_plus", "confidence": "high", "from": "choice:hf_age"}
    H.recompute_derived(st)
    H.apply_update(st, "customer_facing", "avoid", "low", "choice:quick_pick")
    adult = [H.CATALOG_BY_ID[j["job_id"]] for j in H.quick_jobs(st, H.CATALOG)]
    assert any(j["phys"] == "heavy" for j in adult)  # 倉庫の仕分け(人と話さない)
    st, r = picked("ひとりで黙々")
    assert "warehouse" not in [j["job_id"] for j in r["view"]["quick"]["jobs"]]


def test_moving_and_talking_do_not_give_the_same_three():
    _, move = picked("動いてたい")
    _, talk = picked("しゃべるの好き")
    assert set(names(move)) != set(names(talk))
    assert names(talk) == ["コンビニスタッフ", "スーパーのレジ", "カフェスタッフ"]  # 設計メモ §5 の表
    assert names(move) == ["カフェスタッフ", "ファミレスのホール", "イベントスタッフ（案内・誘導）"]
    assert names(picked("座ってじっくり")[1]) == ["データ入力", "書店スタッフ", "清掃スタッフ"]


def test_standing_ok_scores_standing_and_lively_jobs():
    st = H.new_state()
    H.apply_update(st, "physical", "standing_ok", "low", "choice:quick_pick")
    score = {c["job_id"]: c["score"] for c in H.eligible_jobs(st, H.CATALOG)}
    assert score["cafe"] == 1.5 and score["conv"] == 1 and score["bookstore"] == 0  # 立ち仕事 +1、にぎやか +0.5
    assert score["data_entry"] == 0


def memo_tag(j):
    """設計メモ §5 のタグの順(テストの側でもう一度書いたもの)。"""
    for hit, tag in ((j["remote"], "在宅"), (j["spot"], "1日だけOK"), (j["cf"] == "none", "人と話さない"),
                     (j["cf"] == "low", "接客少なめ"), (j["noise"] == "quiet", "静か"), (j["noise"] == "lively", "にぎやか"),
                     ("morning" in j["times"], "朝もOK")):
        if hit:
            return tag
    return None


def test_tags_follow_the_memo_order_and_come_only_from_the_catalog():
    assert [H.job_tag(j) for j in H.CATALOG] == [memo_tag(j) for j in H.CATALOG]
    assert {t for t, _ in H.JOB_TAGS} == {"在宅", "1日だけOK", "人と話さない", "接客少なめ", "静か", "にぎやか", "朝もOK"}
    assert H.job_tag({"remote": True, "spot": True, "cf": "none", "noise": "quiet", "times": {"morning"}}) == "在宅"
    assert H.job_tag({"remote": False, "spot": False, "cf": "high", "noise": "normal", "times": {"evening"}}) is None
    for pick in ENTRY_CHOICES:
        for j in picked(pick)[1]["view"]["quick"]["jobs"]:
            assert j["tag"] == memo_tag(H.CATALOG_BY_ID[j["job_id"]]) and j["name"] == H.CATALOG_BY_ID[j["job_id"]]["name"]


def test_none_of_them_shows_three_light_jobs():
    st, r = picked(H.QUICK_NONE)
    card = r["view"]["quick"]
    assert card["title"] == "ちょっとだけ" and card["icon"] == "light" and card["pick"] is None
    assert names(r) == ["イベントスタッフ（案内・誘導）", "データ入力", "ポスティング"]
    assert all(H.CATALOG_BY_ID[j["job_id"]]["remote"] or H.CATALOG_BY_ID[j["job_id"]]["spot"] for j in card["jobs"])
    assert r["reply"] == "いいね！" + H.LIGHT_TEXT and st.slots == {}


def test_entry_answers_are_low_and_not_direct():
    for pick, (_, values) in H.QUICK_PICKS.items():
        st, r = picked(pick)
        assert st.slots == {
            s: {"value": v, "confidence": "low", "from": "choice:quick_pick"} for s, v in values.items()}
        assert H.QUICK_PICK not in H.DIRECT and r["counted"] is False and st.turns == 0


def test_typed_text_and_crisis_words_on_the_entry_card():
    llm = Quiet()
    st = H.new_state(quick=True)
    r = H.turn(st, "うーん、わからない", H.BANK, llm)
    assert r["reply"] == H.QUICK_TAP_TEXT and r["view"]["quick"]["kind"] == "entry" and llm.calls == []
    r = H.turn(st, "もう消えたい", H.BANK, llm)
    assert r["reply"] == H.CRISIS_TEXT and "quick" not in r["view"] and r["view"]["choices"] == []
    r = H.turn(st, "続ける", H.BANK, llm)
    assert r["view"]["quick"]["kind"] == "entry" and r["view"]["choices"] == ENTRY_CHOICES and llm.calls == []


def test_a_model_reply_without_the_result_line_falls_back_to_it():
    class Bad(Quiet):
        def generate(self, v):
            self.calls.append(v["mode"])
            return json.dumps({"reply": "黙々派ね！データ入力が時給1500円でおすすめ"}, ensure_ascii=False)

    st = H.new_state(quick=True)
    llm = Bad()
    r = H.turn(st, "ひとりで黙々", H.BANK, llm)
    assert r["reply"] == H.QUICK_RESULT_TEXT and llm.calls == [H.QUICK_RESULT] * 2


# ----------------------------- 途中の相談 -----------------------------


@pytest.fixture
def state(tmp_path):
    s = State(tmp_path / "state.json")
    s.reset()
    return s


def test_a_consultation_started_before_the_entry_card_continues_as_it_was(state):
    """入口のカードの前に始めた相談(今の State。icebreak から)は、入口を出さずに今の質問から続く。"""
    h = H.new_state()
    H.start(h, H.BANK)
    session._save(state, UID, h, logic.search_track(state.user(UID)))
    r = session.start(state, UID)
    assert r["started"] is False and r["question"] == {"id": "q_money", "text": QM["q_money"]["text"],
                                                       "choices": QM["q_money"]["choices"]}
    assert r["note"] == session.START_NOTE
    r = session.turn(state, UID, "推し活・趣味")
    r = session.turn(state, UID, "推し活・趣味", [{"output": {"reply": "いいね！" + QM["q_segment"]["text"]}}])
    assert r["status"] == "reply" and r["question"] == "q_segment" and "card" not in r
    assert r["choices"] == QM["q_segment"]["choices"]


def test_a_new_consultation_starts_at_the_entry_and_restart_goes_back_there():
    class Restart(Quiet):
        def extract(self, payload):
            return json.dumps({"slot_updates": [], "intent": "restart"})

    st, _ = picked("しゃべるの好き")
    H.turn(st, H.QUICK_NEAR, H.BANK, Quiet())
    assert st.phase == "icebreak" and st.current["qid"] == "q_money"
    r = H.turn(st, "最初からやりなおしたい", H.BANK, Restart())
    assert st.phase == H.QUICK_PICK and r["view"]["quick"]["kind"] == "entry" and r["view"]["restarted"] is True
    assert st.slots == {} and st.restarts == 1
    old = H.new_state()  # 前の始まり方の相談は、今までどおり icebreak に戻る
    H.start(old, H.BANK)
    H.turn(old, "最初からやりなおしたい", H.BANK, Restart())
    assert old.phase == "icebreak" and old.current["qid"] == "q_money"


def test_session_start_gives_the_entry_card(state):
    r = session.start(state, UID)
    assert r["started"] is True and r["question"]["id"] == H.QUICK_PICK and r["question"]["choices"] == ENTRY_CHOICES
    assert r["question"]["card"]["kind"] == "entry" and r["note"] == session.QUICK_START_NOTE
    r = session.turn(state, UID, "座ってじっくり")
    assert r["status"] == "llm" and r["call"]["stage"] == "say"
    r = session.turn(state, UID, "座ってじっくり", [{"output": {"reply": "じっくり派ね。" + H.QUICK_RESULT_TEXT}}])
    assert r["status"] == "reply" and r["card"]["title"] == "座ってじっくり" and r["progress"] is None
    assert r["choices"] == [H.QUICK_NEAR, H.QUICK_OTHER] and r["counted"] is False


# ----------------------------- Flex と LINE の上限 -----------------------------


def cards() -> dict[str, dict]:
    out = {"entry": H.new_state(quick=True).current["card"]}
    for pick in ENTRY_CHOICES:
        out[pick] = picked(pick)[1]["view"]["quick"]
    return out


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


@pytest.mark.parametrize("base", [BASE, ""])
def test_every_card_flex_fits_the_line_limits(base):
    """LINE の Messaging API の上限: altText 400 字、bubble の JSON 30KB、アクションの label 20 字・text 300 字、
    画像の URL は https で 2000 字まで、text は空にしない。公開 URL が https でなければ絵を出さない。"""
    for name, card in cards().items():
        msg = C.flex(card, base)
        assert msg["type"] == "flex" and 0 < len(msg["altText"]) <= 400, name
        assert msg["contents"]["type"] == "bubble" and len(json.dumps(msg["contents"], ensure_ascii=False).encode()) < 30_000
        nodes = list(walk(msg["contents"]))
        for n in nodes:
            if n.get("type") == "text":
                assert n["text"].strip(), name
            if n.get("type") == "message":
                assert 0 < len(n["label"]) <= 20 and 0 < len(n["text"]) <= 300, n
        images = [n["url"] for n in nodes if n.get("type") == "image"]
        if base:
            assert images and all(u.startswith("https://") and len(u) <= 2000 for u in images), name
            for u in images:  # 絵は #264 の PNG(static/baito)
                assert (ICONS / u.removeprefix(BASE + "/static/baito/")).is_file()
        else:
            assert images == []
        sent = [n["text"] for n in nodes if n.get("type") == "message"]
        assert sent == ([p["label"] for p in card["picks"]] + [card["other"]] if card["kind"] == "entry" else card["buttons"])
        assert C.CARD_BG == msg["contents"]["styles"]["body"]["backgroundColor"] == "#1c1430"
        msgs = to_messages({"bubbles": ["いいね！"], "baito_card": card, "quick_replies": ["x"]}, base_url=base, features=())
        assert len(msgs) <= 5 and [m["type"] for m in msgs] == ["text", "flex"]


def test_entry_tiles_send_the_label_as_the_users_words():
    msg = C.flex(cards()["entry"], BASE)
    tiles = [n for n in walk(msg["contents"]) if n.get("type") == "box" and n.get("action")]
    assert [t["action"] for t in tiles] == [{"type": "message", "label": c, "text": c} for c in ENTRY_CHOICES]
    assert [i["url"] for t in tiles for i in t["contents"] if i["type"] == "image"] == [
        f"{BASE}/static/baito/{n}.png" for n in ("talk", "move", "solo", "sit")]


# ----------------------------- 自動の敵対レビューで足したもの -----------------------------


def test_a_dropped_entry_answer_is_not_shown_as_undecided_in_the_profile():
    """しゃべるの好き → ほかのタイプも見る → 動いてたい → 近くで探す: 外した「接客」を、プロフィールで「未定」にしない。"""
    from dip_mcp.baito import progress as P

    st, mem = H.new_state(quick=True), None
    for said in ("しゃべるの好き", H.QUICK_OTHER, "動いてたい", H.QUICK_NEAR):
        prev = P.before(st)
        r = H.turn(st, said, H.BANK, Quiet())
        p, mem = P.update(st, mem, prev, r)
        assert p is None
    assert mem["seen"] == ["physical"]
    items = [it for g in P.groups(st, set(mem["seen"])) for it in g["items"] if it.get("label") == "接客"]
    assert items == [{"label": "接客", "value": None, "inferred": False, "undecided": False}]


def test_a_junior_high_block_skips_the_entry_card_on_restart_and_on_a_new_start(state, monkeypatch):
    """中学生以下で止めた人には、職種のおすすめも出さない: 「最初から」も、印が残ったままの新しい相談も、年齢を聞く今の流れから。"""
    class Restart(Quiet):
        def extract(self, payload):
            return json.dumps({"slot_updates": [], "intent": "restart"})

    st, _ = picked(H.QUICK_NONE)
    H.turn(st, H.QUICK_NEAR, H.BANK, Quiet())
    st.slots["age_band"] = {"value": "under_15", "confidence": "high", "from": "choice:hf_age"}
    H.update_block(st)
    assert st.blocked == "under_15"
    H.turn(st, "最初からやりなおしたい", H.BANK, Restart())
    assert st.phase == "icebreak" and st.current["qid"] == "q_money" and H.QUICK_FLAG not in st.flags
    state.update(UID, lambda u: u.__setitem__(session.BLOCK_KEY, {"blocked": "under_15"}))
    r = session.start(state, UID)
    assert r["started"] is True and r["question"]["id"] == "q_money" and "card" not in r["question"]


def test_the_reaction_may_not_name_a_job_that_is_not_on_the_card():
    class Says(Quiet):
        def __init__(self, reaction):
            super().__init__()
            self.reaction = reaction

        def generate(self, v):
            self.calls.append(v["mode"])
            return json.dumps({"reply": self.reaction + v["text"]}, ensure_ascii=False)

    st = H.new_state(quick=True)
    r = H.turn(st, "しゃべるの好き", H.BANK, Says("居酒屋もいいかも！"))  # 18 歳から(カードに無い)
    assert r["reply"] == H.QUICK_RESULT_TEXT
    st = H.new_state(quick=True)
    r = H.turn(st, "しゃべるの好き", H.BANK, Says("カフェスタッフ、似合いそう！"))  # カードにある職種はよい
    assert r["reply"] == "カフェスタッフ、似合いそう！" + H.QUICK_RESULT_TEXT


def test_a_danger_message_on_the_entry_card_is_not_counted():
    st = H.new_state(quick=True)
    for _ in range(H.MAX_TURNS + 2):
        r = H.turn(st, "SNSで高額の受け取るだけのバイトある？", H.BANK, Quiet())
    assert r["reply"] == H.NOTICE["danger"] and r["view"]["quick"]["kind"] == "entry" and r["done"] is False
    assert st.turns == 0 and st.phase == H.QUICK_PICK and "danger_warned" in st.flags
