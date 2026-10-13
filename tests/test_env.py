""".env の読み込み(aica_env.load_env)。実 LLM なし。"""

from __future__ import annotations

import os

import aica_env
from line_runtime import config as line_config
from voice_resume import config as voice_config


def test_load_env_sets_missing_values_and_keeps_existing_ones(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# コメント\n\nAICA_T_A=1\nAICA_T_B = "two"\nAICA_T_C=\'3\'\nAICA_T_KEEP=new\nnot a pair\n', encoding="utf-8")
    for k in ("AICA_T_A", "AICA_T_B", "AICA_T_C"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AICA_T_KEEP", "old")

    aica_env.load_env(env)

    assert (os.environ["AICA_T_A"], os.environ["AICA_T_B"], os.environ["AICA_T_C"]) == ("1", "two", "3")
    assert os.environ["AICA_T_KEEP"] == "old"  # 既存の値は上書きしない


def test_load_env_without_a_file_does_nothing(tmp_path):
    aica_env.load_env(tmp_path / "missing.env")


def test_there_is_one_env_loader():
    """line_runtime と voice_resume の config.load_env は、同じ 1 つの関数(R13)。"""
    assert line_config.load_env is aica_env.load_env
    assert voice_config.load_env is aica_env.load_env
