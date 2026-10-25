"""STT 評価の台本・採点と、STT の作り分け(create_stt)のテスト。実 API は呼ばない。

タスク 37: 2026-09-26 の実会話の聞き間違い(「合うと思う」→「アウトも」、
「時給の下限」→「地球の加減」)を採点が検出できること、正しい文字起こしなら
満点になること、既定の STT 設定(nova-3 + keyterm)で聞き間違いの語句が
用語登録されていることを守る。
"""

from voice_resume import config
from voice_resume.stt_eval import (
    MISHEARD_2026_09_26,
    SCRIPT,
    normalize,
    score_line,
    score_transcripts,
)


def test_script_contains_the_misheard_phrases():
    """受け入れ条件の台本: 「時給の下限」「合うと思う」を含む。"""
    all_text = "".join(item["text"] for item in SCRIPT)
    assert "時給の下限" in all_text
    assert "合うと思う" in all_text


def test_correct_transcripts_score_full():
    """台本どおりの文字起こしなら満点(句読点や空白の違いは正規化で吸収)。"""
    got = [item["text"].replace("。", " ") for item in SCRIPT]
    result = score_transcripts(got)
    assert result["hits"] == result["total"]
    assert result["misses"] == []


def test_misheard_2026_09_26_is_detected():
    """実会話の聞き間違いを入れると、「合うと思う」「時給の下限」の miss として出る。"""
    got = [item["text"] for item in SCRIPT]
    for i, misheard in MISHEARD_2026_09_26.items():
        got[i] = misheard
    result = score_transcripts(got)
    missed_phrases = {p for _, p in result["misses"]}
    assert "合うと思う" in missed_phrases  # 「アウトも」
    assert "時給の下限" in missed_phrases  # 「地球の加減」
    assert "全然出ない" in missed_phrases  # 「全然気が出ないか」


def test_normalize_and_variants():
    assert normalize("時給の 下限。") == "時給の下限"
    assert normalize("1,200円") == "1200円"  # NFKC + 記号除去
    # 表記ゆれ(週三日)も正解になる
    assert score_line("週三日くらい入りたい", [["週3日", "週三日"]]) == [True]


def test_default_stt_is_nova3_with_keyterms():
    """既定: Deepgram nova-3(ja)+ 用語登録。聞き間違いのあった語句を含む。"""
    assert config.STT_PROVIDER == "deepgram"
    assert config.STT_MODEL == "nova-3"
    assert config.STT_LANGUAGE == "ja"
    for term in ("時給", "下限", "品出し", "ホール", "キッチン", "シフト"):
        assert term in config.STT_KEYTERMS


def test_create_stt_deepgram_default(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dummy")
    from voice_resume.stt import create_stt

    stt = create_stt()
    assert type(stt).__module__.startswith("livekit.plugins.deepgram")
    assert stt._opts.model == "nova-3"
    assert stt._opts.language == "ja"
    assert list(stt._opts.keyterm) == list(config.STT_KEYTERMS)
    assert stt._opts.mip_opt_out is True  # 声を Deepgram の改善プログラムに使わせない(docs/design/privacy.md の 3.2)


def test_create_stt_nova2_fallback_drops_keyterm(monkeypatch):
    """nova-2 に戻したとき keyterm(nova-3 専用)を渡さない = 起動できる。"""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dummy")
    monkeypatch.setattr(config, "STT_MODEL", "nova-2")
    from voice_resume.stt import create_stt

    stt = create_stt()
    assert stt._opts.model == "nova-2"
    assert not stt._opts.keyterm
    assert stt._opts.mip_opt_out is True


def test_create_stt_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(config, "STT_PROVIDER", "openai")
    monkeypatch.setattr(config, "STT_MODEL", "gpt-4o-transcribe")
    from voice_resume.stt import create_stt

    stt = create_stt()
    assert type(stt).__module__.startswith("livekit.plugins.openai")
    assert stt._opts.model == "gpt-4o-transcribe"
