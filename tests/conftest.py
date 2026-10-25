"""テスト全体の共通の準備。

本人の同意(line_runtime.consent。docs/design/privacy.md の 3.6)は、ほとんどのテストの主題ではないので、既定では
「同意している」扱いにする(同意の前は LLM を呼ばないので、そのままでは会話のテストが同意の手順から始まる)。
同意そのものを確かめるテストは `@pytest.mark.consent` を付けて、本物の判定(記録を読む)で動かす(tests/test_consent.py)。
"""

from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "consent: 本人の同意の判定を本物のまま使う(既定では同意している扱い)")


@pytest.fixture(autouse=True)
def _consented(request, monkeypatch):
    if request.node.get_closest_marker("consent"):
        return
    from line_runtime import consent

    monkeypatch.setattr(consent, "needed", lambda *_a, **_k: False)
