"""音声相談(会話・下書き・調べもの)と履歴書の読み取りが、既定で Anthropic に送る中身(task 236)。実 API は呼ばない。

偽の Anthropic のクライアントが受け取った引数で、モデル、思考(thinking と output_config.effort)、ツール、
PDF と画像のブロックを確かめる。Claude Sonnet 5.5 で 400 になる組み合わせ(thinking disabled、強制の tool_choice、
思考を切ったうえの xhigh 以上)を送らないことも見る。
"""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from resume_draft.schema import ResumeDraft, WorkExperience
from resume_intake import config as resume_config
from resume_intake import extract
from tests.test_resume_intake import make_pdf
from voice_resume import advisor as advisor_mod
from voice_resume import config as voice_config
from voice_resume import voice_llm
from voice_resume.advisor import PERSONAL_QUERY_REPLY, ResumeAdvisor
from voice_resume.drafter import build_draft

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "resume_intake" / "samples"
TRANSCRIPT = [
    {"role": "assistant", "text": "これまでの経験を教えてください。"},
    {"role": "user", "text": "コンビニで1年半、レジと品出しをしていました。電話は 090-1234-5678 です。"},
]
SONNET_OFF = {"thinking": {"type": "between_tools"}, "output_config": {"effort": "low"}}


def _usage(inp=100, out=20, read=0, write=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=read,
                           cache_creation_input_tokens=write)


class FakeAnthropic:
    """messages.parse と messages.create だけ持つ AsyncAnthropic の代役(responses を持たないので Anthropic 扱い)。"""

    def __init__(self, parsed=None, replies: list | None = None):
        self.calls: list[dict] = []
        self._parsed = parsed
        self._replies = list(replies or [])

        async def parse(**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(parsed_output=self._parsed, usage=_usage())

        async def create(**kwargs):
            self.calls.append(kwargs)
            return self._replies.pop(0)

        self.messages = SimpleNamespace(parse=parse, create=create)


def _assert_no_400_shapes(kwargs: dict) -> None:
    """Claude Sonnet 5.5 で 400 になる送り方をしていない。"""
    assert kwargs.get("thinking", {}).get("type") != "disabled"
    assert kwargs.get("tool_choice", {}).get("type") not in ("any", "tool")
    if kwargs.get("thinking", {}).get("type") == "between_tools":
        assert kwargs["thinking"] == {"type": "between_tools"}  # ほかの欄を付けると 400
        assert kwargs["output_config"]["effort"] in ("low", "medium", "high")
    for k in ("temperature", "top_p", "top_k"):
        assert k not in kwargs


# ----------------------------- 既定の設定 -----------------------------


def _config_in_subprocess(env: dict) -> list[str]:
    code = ("from voice_resume import config as v; from resume_intake import config as r; "
            "print(v.PROVIDER, v.LLM_MODEL, v.DRAFT_MODEL, v.RESEARCH_MODEL, r.PROVIDER, r.MODEL)")
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("LINE_SIM_", "VOICE_RESUME_", "RESUME_INTAKE_"))}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=clean | env, capture_output=True, text=True,
                         timeout=60, check=True)
    return out.stdout.split()


def test_default_provider_is_anthropic_and_openai_can_come_back():
    """既定は Anthropic の claude-sonnet-5-5。LINE_SIM_PROVIDER=openai で前の gpt-6-sol に戻り、音声と履歴書だけを
    別の設定(VOICE_RESUME_PROVIDER、RESUME_INTAKE_PROVIDER)で選ぶこともできる。"""
    sonnet = "claude-sonnet-5-5"
    assert _config_in_subprocess({}) == ["anthropic", sonnet, sonnet, sonnet, "anthropic", sonnet]
    gpt = "gpt-6-sol"
    assert _config_in_subprocess({"LINE_SIM_PROVIDER": "openai"}) == ["openai", gpt, gpt, gpt, "openai", gpt]
    assert _config_in_subprocess({"LINE_SIM_PROVIDER": "openai", "VOICE_RESUME_PROVIDER": "anthropic",
                                  "RESUME_INTAKE_PROVIDER": "anthropic"}) == [
        "anthropic", sonnet, sonnet, sonnet, "anthropic", sonnet]


# ----------------------------- 音声の会話の LLM -----------------------------


def _event_stream(text: str):
    """プラグインが読むストリームの代役(返事の文 1 つ)。"""
    events = [
        SimpleNamespace(type="message_start", message=SimpleNamespace(id="msg_1", usage=_usage(10, 0, 0, 0))),
        SimpleNamespace(type="content_block_start", index=0, content_block=SimpleNamespace(type="text", text="")),
        SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text=text)),
        SimpleNamespace(type="content_block_stop", index=0),
        SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=5)),
    ]

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for e in events:
                yield e

    return Stream()


def test_voice_llm_is_claude_with_thinking_off_and_cache(monkeypatch):
    """会話の LLM は LiveKit の Anthropic のプラグイン。思考は切る(Sonnet 5.5 は between_tools + effort low)。
    キャッシュの区切りを付け、ツールの選び方を強制しない。返事の文は読み上げに渡る。"""
    from livekit.agents import function_tool, llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    built = voice_llm.create_llm()
    assert isinstance(built, voice_llm.ClaudeLLM) and built.model == "claude-sonnet-5-5"
    assert built.thinking_kwargs == SONNET_OFF and built._opts.caching == "ephemeral"
    assert built._opts.max_tokens == voice_config.LLM_MAX_TOKENS
    # strict の形は省ける引数を null との union にし、アドバイザーのツールでは上限(16 個)を超えて 400 になる(実 API で確かめた)
    assert built._opts.strict_tool_schema is False

    sent: list[dict] = []

    async def create(**kwargs):
        sent.append(kwargs)
        return _event_stream("どんなお仕事を探していますか?")

    fake = SimpleNamespace(messages=SimpleNamespace(create=create), _base_url=SimpleNamespace(netloc=b"api.anthropic.com"))
    model = voice_llm.ClaudeLLM(model="claude-sonnet-5-5", api_key="test-key", client=fake, caching="ephemeral",
                                max_tokens=4096, thinking=built.thinking_kwargs, _strict_tool_schema=False)

    @function_tool
    async def propose_jobs(area: str) -> str:
        """求人を探す。"""
        return area

    ctx = llm.ChatContext()
    ctx.add_message(role="system", content="あなたは仕事探しのアドバイザーです。")
    ctx.add_message(role="user", content="アルバイトを探しています")

    async def run() -> str:
        out = []
        async with model.chat(chat_ctx=ctx, tools=[propose_jobs]) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    out.append(chunk.delta.content)
        return "".join(out)

    assert asyncio.run(run()) == "どんなお仕事を探していますか?"
    (kwargs,) = sent
    assert kwargs["model"] == "claude-sonnet-5-5" and kwargs["stream"] is True and kwargs["max_tokens"] == 4096
    assert kwargs["thinking"] == {"type": "between_tools"} and kwargs["output_config"] == {"effort": "low"}
    assert [t["name"] for t in kwargs["tools"]] == ["propose_jobs"] and kwargs["tools"][-1]["cache_control"]
    assert "strict" not in kwargs["tools"][0]
    assert kwargs["system"][-1]["cache_control"] == {"type": "ephemeral"}
    _assert_no_400_shapes(kwargs)


def test_advisor_tools_are_sent_without_strict_unions(monkeypatch):
    """アドバイザーの本物のツールを、strict ではない形で送る(strict だと union の引数が 47 個で 400 だった)。"""
    from livekit.agents import llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    built = voice_llm.create_llm()
    schemas = llm.ToolContext(ResumeAdvisor().tools).parse_function_tools("anthropic", strict=built._opts.strict_tool_schema)
    assert len(schemas) == 7 and not any(sc.get("strict") for sc in schemas)


def test_voice_llm_haiku_and_openai(monkeypatch):
    """Claude Haiku 5.5 にすれば思考は disabled + low(Haiku 5.5 は disabled を受ける)。openai なら前の gpt-6-sol。"""
    from livekit.plugins import openai as lk_openai

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(voice_config, "LLM_MODEL", "claude-haiku-5-5")
    assert voice_llm.create_llm().thinking_kwargs == {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(voice_config, "PROVIDER", "openai")
    monkeypatch.setattr(voice_config, "LLM_MODEL", "gpt-6-sol")
    assert isinstance(voice_llm.create_llm(), lk_openai.LLM)


def test_voice_llm_needs_the_anthropic_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Anthropic API key"):
        voice_llm.create_llm()


# ----------------------------- 下書き -----------------------------


def test_draft_goes_to_claude_with_low_effort_and_no_cache():
    """下書きは Claude Sonnet 5.5 の構造化出力。思考は任せて effort low、1 回しか送らないのでキャッシュの区切りは無い。
    出力の電話番号などは消す(scrub_pii)。"""
    draft = ResumeDraft(work_history=[WorkExperience(organization="コンビニ", duties=["レジ 090-1234-5678"])])
    client = FakeAnthropic(parsed=draft)
    got = asyncio.run(build_draft(TRANSCRIPT, client))
    (kwargs,) = client.calls
    assert kwargs["model"] == "claude-sonnet-5-5" and kwargs["output_format"] is ResumeDraft
    assert kwargs["output_config"] == {"effort": "low"} and "thinking" not in kwargs  # adaptive(送らないのと同じ)
    assert isinstance(kwargs["system"], str) and "cache_control" not in kwargs and "extra_body" not in kwargs
    assert "本人: コンビニで1年半" in kwargs["messages"][0]["content"]
    assert "090-1234-5678" not in got.work_history[0].duties[0]
    _assert_no_400_shapes(kwargs)


def test_draft_falls_back_to_empty_when_claude_returns_nothing():
    assert asyncio.run(build_draft(TRANSCRIPT, FakeAnthropic(parsed=None))) == ResumeDraft()


def test_finalize_interview_uses_the_anthropic_client_by_default(monkeypatch):
    """advisor の既定の client は config.PROVIDER(anthropic)のもの。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from anthropic import AsyncAnthropic

    assert isinstance(advisor_mod._default_client(), AsyncAnthropic)


# ----------------------------- 調べもの -----------------------------


def _search_reply(*blocks, stop="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=_usage())


TEXT = lambda t: SimpleNamespace(type="text", text=t)
TOOL_USE = SimpleNamespace(type="server_tool_use", id="srvtoolu_1", name="web_search", input={"query": "品出し 仕事内容"})
TOOL_RESULT = SimpleNamespace(type="web_search_tool_result", tool_use_id="srvtoolu_1", content=[])


def test_research_uses_web_search_with_a_small_max_uses():
    """調べものは web 検索のサーバーツール(web_search_20260209。dynamic filtering なし)。1 回の調べもので検索は 2 回まで、日本向け。
    思考は切る。読み上げるのは検索の後の答えだけ。"""
    client = FakeAnthropic(replies=[_search_reply(TEXT("調べますね。"), TOOL_USE, TOOL_RESULT,
                                                  TEXT("品出しは商品を棚に並べる仕事です。"), TEXT("主な情報源はタウンワークです。"))])
    advisor = ResumeAdvisor(research_client_factory=lambda: client)
    reply = asyncio.run(advisor._research("調べもの", "品出しの仕事内容"))
    (kwargs,) = client.calls
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert kwargs["tools"] == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 2,
                                "allowed_callers": ["direct"],  # dynamic filtering は切る(時間と費用が半分だった)
                                "user_location": {"type": "approximate", "country": "JP", "timezone": "Asia/Tokyo"}}]
    assert {k: kwargs[k] for k in ("thinking", "output_config")} == SONNET_OFF
    assert kwargs["messages"][0]["content"].endswith("品出しの仕事内容")
    assert "品出しは商品を棚に並べる仕事です。主な情報源はタウンワークです。" in reply and "調べますね" not in reply
    assert advisor.web_searches == 1
    _assert_no_400_shapes(kwargs)


def test_research_continues_after_pause_turn():
    """長い検索で返事が止まったら(pause_turn)、止まった返事を送り返して続きを受け取る。"""
    paused = _search_reply(TOOL_USE, stop="pause_turn")
    client = FakeAnthropic(replies=[paused, _search_reply(TOOL_RESULT, TEXT("答えです。"))])
    advisor = ResumeAdvisor(research_client_factory=lambda: client)
    reply = asyncio.run(advisor._research("調べもの", "品出し"))
    assert len(client.calls) == 2 and "答えです。" in reply
    assert client.calls[1]["messages"][1] == {"role": "assistant", "content": paused.content}


def test_research_that_stays_paused_is_reported_as_a_failure():
    """続きを頼んでも止まったままなら、答えが無いので調べものの失敗として伝える(続きは 1 回まで)。"""
    paused = _search_reply(TOOL_USE, stop="pause_turn")
    client = FakeAnthropic(replies=[paused, paused, paused])
    reply = asyncio.run(ResumeAdvisor(research_client_factory=lambda: client)._research("調べもの", "品出し"))
    assert len(client.calls) == 1 + advisor_mod.MAX_RESEARCH_PAUSES == 2 and "失敗" in reply


def test_research_with_personal_values_is_refused_before_claude():
    """住所や電話番号の形がある問いは、Anthropic にも送らずに断る(#211 の決まりのまま)。"""
    client = FakeAnthropic(replies=[])
    advisor = ResumeAdvisor(research_client_factory=lambda: client)
    for subject in ("東京都新宿区西新宿2-8-1 の近くの求人", "090-1234-5678 の会社"):
        assert asyncio.run(advisor._research("調べもの", subject)) == PERSONAL_QUERY_REPLY
    assert client.calls == [] and advisor.web_searches == 0


def test_research_failure_keeps_the_talk_going():
    async def boom(**_kw):
        raise RuntimeError("overloaded")

    client = SimpleNamespace(messages=SimpleNamespace(create=boom))
    reply = asyncio.run(ResumeAdvisor(research_client_factory=lambda: client)._research("調べもの", "品出し"))
    assert "失敗" in reply


# ----------------------------- 履歴書の読み取り -----------------------------


def _read(name: str, data: bytes) -> tuple[dict, extract.Usage]:
    client = FakeAnthropic(parsed=ResumeDraft(skills=["接客"]))
    _draft, usage = asyncio.run(extract.extract_bytes(name, data, client))
    (kwargs,) = client.calls
    return kwargs, usage


def test_image_resume_is_sent_as_an_image_block():
    data = (SAMPLES / "02_photo.jpg").read_bytes()
    kwargs, usage = _read("山田太郎の履歴書.jpg", data)
    image, ask = kwargs["messages"][0]["content"]
    assert image == {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.b64encode(data).decode("ascii")}}
    assert ask["type"] == "text" and "スキーマどおり" in ask["text"]
    assert "山田" not in str(kwargs)  # ファイル名は送らない
    assert kwargs["model"] == "claude-sonnet-5-5" and kwargs["output_format"] is ResumeDraft
    assert {k: kwargs[k] for k in ("thinking", "output_config")} == SONNET_OFF  # 写すだけなので思考を切る
    assert isinstance(kwargs["system"], str) and "extra_body" not in kwargs  # キャッシュの区切りは無い
    assert (usage.input_tokens, usage.output_tokens) == (100, 20)
    _assert_no_400_shapes(kwargs)


def test_pdf_without_text_is_sent_as_a_document_block():
    data = make_pdf("", image=(595, 842))  # スキャンした紙(テキストが無く、画像がページを覆う)
    kwargs, _ = _read("resume.pdf", data)
    doc = kwargs["messages"][0]["content"][0]
    assert doc == {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                  "data": base64.b64encode(data).decode("ascii")}}


def test_text_pdf_is_sent_as_text_only():
    """テキストのある PDF は、取り出したテキストだけを送る(#217 の決まりのまま。ファイルも画像も送らない)。"""
    kwargs, _ = _read("resume.pdf", make_pdf())
    blocks = kwargs["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["text", "text"]
    assert blocks[0]["text"].startswith(extract.PDF_TEXT_HEADER) and "Miho Systems" in blocks[0]["text"]


def _png(w: int, h: int, noise: bool = False) -> bytes:
    import io
    import random

    from PIL import Image

    img = Image.new("RGB", (w, h), "white")
    if noise:  # 圧縮の効かない写真の代わり
        img = Image.frombytes("RGB", (w, h), random.Random(0).randbytes(w * h * 3))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def test_large_photo_is_shrunk_under_the_anthropic_limit():
    """アップロードは 10MB まで受けるが、Anthropic は 1 枚を base64 にして 10MB まで(4/3 倍になる)。超える写真は縮めて
    JPEG にし直して送る。"""
    from PIL import Image

    data = _png(1700, 1600, noise=True)
    assert len(base64.b64encode(data)) > extract.ANTHROPIC_MAX_IMAGE_B64_BYTES
    assert len(data) <= resume_config.MAX_UPLOAD_BYTES
    kwargs, _ = _read("photo.png", data)
    src = kwargs["messages"][0]["content"][0]["source"]
    sent = base64.b64decode(src["data"])
    assert src["media_type"] == "image/jpeg" and sent.startswith(b"\xff\xd8\xff")
    assert len(src["data"]) <= extract.ANTHROPIC_MAX_IMAGE_B64_BYTES
    import io

    assert max(Image.open(io.BytesIO(sent)).size) <= extract.SHRUNK_IMAGE_SIDE
    # 縦横の画素が上限を超える画像も縮める(小さいファイルでも)
    tall = _png(200, extract.ANTHROPIC_MAX_IMAGE_SIDE + 1)
    raw, mt = extract.fit_image(tall, "image/png")
    assert mt == "image/jpeg" and max(Image.open(io.BytesIO(raw)).size) == extract.SHRUNK_IMAGE_SIDE


def _spy_loads(monkeypatch) -> list[tuple[int, int]]:
    """Pillow が画素を展開した画像の大きさ(ImageFile.load を呼んだときの size)。"""
    from PIL import ImageFile

    loaded: list[tuple[int, int]] = []
    real = ImageFile.ImageFile.load

    def load(self):
        loaded.append(self.size)
        return real(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", load)
    return loaded


def test_image_too_many_pixels_is_refused_before_decoding(monkeypatch):
    """圧縮の効いた小さな PNG(13000×13000 の白黒で 47KB ほど)は、展開すると 169MB になる。Web と音声ワーカーが
    512MB を分け合うので、縮めるのに展開する画素が上限を超えるなら、展開する前に断る。"""
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("1", (13000, 13000), 1).save(out, format="PNG")
    data = out.getvalue()
    assert len(data) < 100_000
    loaded = _spy_loads(monkeypatch)
    assert extract.oversized_image(data)
    with pytest.raises(extract.ImageTooLarge):
        extract.fit_image(data, "image/png")
    assert loaded == []  # 画素は読んでいない
    # 縮めずに送れる画像と、上限の中の画像は断らない
    assert not extract.oversized_image(_png(40, 30))
    assert not extract.oversized_image(_png(200, extract.ANTHROPIC_MAX_IMAGE_SIDE + 1))


def test_image_pillow_refuses_to_open_is_refused_too(monkeypatch):
    """Pillow が開くのを断るほど画素の多い画像(178,956,970 画素を超える。14000×14000 の白黒の PNG は 24KB ほど)も、
    縮めずに送る画像と見なさず、ImageTooLarge にする(縦横 8000 画素を超えたまま Anthropic に送らない)。"""
    import io

    from PIL import Image

    side = 14000
    assert side * side > 2 * Image.MAX_IMAGE_PIXELS
    out = io.BytesIO()
    Image.new("1", (side, side), 1).save(out, format="PNG")
    data = out.getvalue()
    assert len(data) < 100_000
    loaded = _spy_loads(monkeypatch)
    assert extract.oversized_image(data)
    with pytest.raises(extract.ImageTooLarge):
        extract.fit_image(data, "image/png")
    assert loaded == []
    # 読めない画像は今までどおり、縮めずに API へ送る(答えは API に任せる)
    assert not extract.oversized_image(b"\x89PNG\r\n\x1a\n broken")
    assert extract.fit_image(b"\x89PNG\r\n\x1a\n broken", "image/png") == (b"\x89PNG\r\n\x1a\n broken", "image/png")


def test_large_jpeg_is_read_shrunk_and_turned_upright(monkeypatch):
    """JPEG は縮めて読む(48MP の写真でも、全体を展開しない)。写真の向き(EXIF)は縮めた後で直す。"""
    import io

    from PIL import Image

    w, h = extract.ANTHROPIC_MAX_IMAGE_SIDE + 200, 1000
    exif = Image.Exif()
    exif[0x0112] = 6  # 右に 90 度回して見る写真
    out = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(out, format="JPEG", exif=exif)
    loaded = _spy_loads(monkeypatch)
    raw, mt = extract.fit_image(out.getvalue(), "image/jpeg")
    assert loaded and max(max(s) for s in loaded) <= w // 2  # 1/2 以下に縮めて読んだ
    sent = Image.open(io.BytesIO(raw))
    assert mt == "image/jpeg" and sent.size[1] == extract.SHRUNK_IMAGE_SIDE and sent.size[0] < sent.size[1]


def test_shrunk_transparent_png_gets_a_white_background():
    """透けた所のある PNG を縮めるときは、白で埋めて JPEG にする(黒くしない)。"""
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGBA", (200, extract.ANTHROPIC_MAX_IMAGE_SIDE + 1), (0, 0, 0, 0)).save(out, format="PNG")
    raw, mt = extract.fit_image(out.getvalue(), "image/png")
    sent = Image.open(io.BytesIO(raw)).convert("RGB")
    assert mt == "image/jpeg" and min(sent.getpixel((sent.width // 2, sent.height // 2))) > 240


def test_image_type_comes_from_the_content_not_the_name():
    """名前を .jpg に付け替えた PNG は image/png で送る(拡張子のまま送ると 400)。小さい画像はそのまま送る。"""
    data = _png(40, 30)
    kwargs, _ = _read("resume.jpg", data)
    src = kwargs["messages"][0]["content"][0]["source"]
    assert src["media_type"] == "image/png" and base64.b64decode(src["data"]) == data


def test_resume_clients_follow_the_provider(monkeypatch):
    """トークのアップロードと CLI の client は RESUME_INTAKE_PROVIDER(既定 anthropic)のもの。"""
    from anthropic import AsyncAnthropic
    from openai import AsyncOpenAI

    from line_runtime import server

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert isinstance(server.resume_client(), AsyncAnthropic)
    monkeypatch.setattr(resume_config, "PROVIDER", "openai")
    assert isinstance(server.resume_client(), AsyncOpenAI)


# ----------------------------- 同意の文言 -----------------------------


def test_consent_names_where_the_resume_and_the_voice_go(monkeypatch):
    """履歴書と音声相談の同意の文言の送り先は、それぞれのプロバイダに合わせる(既定は Anthropic)。"""
    from line_runtime import consent

    def sent() -> tuple[str, str]:
        t = consent.texts()
        return t["resume"]["paragraphs"][0], t["voice"]["paragraphs"][1]

    resume, voice = sent()
    assert "米国の Anthropic に送ります" in resume and "米国の Anthropic に送り" in voice
    monkeypatch.setattr(resume_config, "PROVIDER", "openai")
    monkeypatch.setattr(voice_config, "PROVIDER", "openai")
    resume, voice = sent()
    assert "米国の OpenAI に送ります" in resume and "米国の OpenAI に送り" in voice
