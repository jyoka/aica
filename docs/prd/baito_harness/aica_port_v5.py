"""aica の v5(dip_mcp/baito/v5.py。会話の層に載せ替えた版)を、この参照資料のシミュレーターで回すための入口(aica task 360)。

    BAITO_HARNESS=aica_port_v5 python3 simulate.py v4   # results_v4.json と transcripts/ を上書きするので、写しの中で回す

v5 は v4 の名前(付録C)をそのまま持つ(v4 のモジュールの全部の名前に、v5 の State・new_state・start・resume・turn を重ねた
モジュールを返す)。シミュレーターのモックの LLM は v4 の抽出(extract)を返すので、ここで v5 の読む段(read。act の並び)の出力に
写してから渡す(ReadAdapter)。写し方: 抽出の値は v4 の validate_extract で直してから、今の問いの欄なら answer、ほかの欄なら add、
区分は slot "segment"、intent は clarify / show_result / restart / danger / aside(雑談)の act、review_ack の all は confirm。
壊れた出力(malformed のペルソナ)は、そのまま渡す(v5 の読む段の検証が落とし、1 回やり直す)。
単体チェック(unit_checks_v4.py)は v5 にかけない(turn() の形とプロンプトの原文を見る検査は v4 だけ。設計メモ §4.13、§6 の 9)。
"""
import json
import os
import sys
import types

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
if os.environ.get("AICA_ROOT"):
    _ROOT = os.environ["AICA_ROOT"]
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dip_mcp.baito import harness as _v4
from dip_mcp.baito import v5 as _v5

INTENT_ACTS = {"clarify": "clarify", "show_result": "show_result", "restart": "restart", "danger": "danger", "aside": "chitchat"}


def to_reading(raw, payload):
    """v4 の抽出の出力 -> v5 の読む段の出力(JSON の文)。使えない出力は、そのまま返す(v5 の検証で落ちる)。"""
    clean, _ = _v4.validate_extract(raw)
    if clean is None:
        return raw
    said = payload.get("user_text") or ""
    targets = set(payload.get("targets") or [])

    def text_of(evidence):
        e = _v4.nfkc(evidence) if isinstance(evidence, str) else ""
        return e if e and e in _v4.nfkc(said) else said
    acts = []
    for u in clean["slot_updates"]:
        if u["value"] is None:
            continue
        acts.append({"act": "answer" if u["slot"] in targets else "add", "slot": u["slot"], "text": text_of(u.get("evidence")),
                     "value": u["value"]})
    if clean.get("segment"):
        acts.append({"act": "answer" if "segment" in targets else "add", "slot": "segment", "text": said, "value": clean["segment"]})
    if clean["intent"] in INTENT_ACTS:
        acts.append({"act": INTENT_ACTS[clean["intent"]]})
    if clean.get("review_ack") == "all":
        acts.append({"act": "confirm"})
    return json.dumps({"acts": acts, "hook": None, "mentions": [], "ng": clean["ng_updates"]}, ensure_ascii=False)


class ReadAdapter:
    """シミュレーターのモックの LLM(extract / generate)を、v5 の llm(read / generate)にする。read を持つ LLM(実 LLM)は写さない。"""

    def __init__(self, llm):
        self.llm = llm

    def read(self, payload):
        if hasattr(self.llm, "read"):  # 実 LLM(line_runtime.baito_chat.SyncLLM。evals.baito_sim --real --version v5)は読む段をそのまま読む
            return self.llm.read(payload)
        return to_reading(self.llm.extract(payload), payload)

    def generate(self, view):
        return self.llm.generate(view)

    def __getattr__(self, name):
        return getattr(self.llm, name)


READ_EVENTS = {"read_fallback": "extract_fallback", "read_repaired": "extract_repaired", "read_retry_ok": "extract_retry_ok"}


def turn(state, user_text, bank, llm, catalog=None, search=None):
    """v5 の turn。読む段の失敗の記録(read_fallback など)には、シミュレーターの指標が読む抽出の名前(extract_fallback など)の
    記録を並べて足す(LLM の失敗を、v4 と同じく取りこぼし(dropped)に数えないため)。"""
    n = len(state.log)
    out = _v5.turn(state, user_text, bank, ReadAdapter(llm), catalog, search)
    for e in list(state.log[n:]):
        if e["kind"] in READ_EVENTS:
            state.log.append(dict(e, kind=READ_EVENTS[e["kind"]], alias_of=e["kind"]))
    return out


_port = types.ModuleType(__name__)
_port.__dict__.update({k: v for k, v in vars(_v4).items() if not k.startswith("__")})
_port.__dict__.update({"VERSION": _v5.VERSION, "State": _v5.State, "new_state": _v5.new_state, "start": _v5.start,
                       "resume": _v5.resume, "turn": turn, "advance": _v5.advance, "pick_question": _v5.pick_question,
                       "render_prompt": _v5.render_prompt, "to_reading": to_reading, "ReadAdapter": ReadAdapter})
sys.modules[__name__] = _port
