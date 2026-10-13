"""パッケージの間の import(HLD の 3.2)。循環が無いことと、循環を切った 2 か所が戻らないことを確かめる。実 LLM なし。

関数の中の import も数える(読み込みの時点では動かないが、依存していることは同じ)。テストのファイルは数えない。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = sorted(p.name for p in ROOT.iterdir() if (p / "__init__.py").is_file() and p.name != "tests")


def _imports() -> dict[str, set[str]]:
    """パッケージ → それが import するほかのパッケージ。"""
    deps = {p: set() for p in PACKAGES}
    for pkg in PACKAGES:
        for f in (ROOT / pkg).rglob("*.py"):
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    names = [node.module]
                else:
                    continue
                deps[pkg] |= {n.split(".")[0] for n in names} & set(PACKAGES) - {pkg}
    return deps


def _cycle(deps: dict[str, set[str]]) -> list[str] | None:
    """循環があれば 1 つ(a → b → … → a)、無ければ None。"""
    state: dict[str, str] = {}

    def visit(p: str, path: list[str]) -> list[str] | None:
        state[p] = "open"
        for q in sorted(deps[p]):
            if state.get(q) == "open":
                return path[path.index(q):] + [q]
            if q not in state and (found := visit(q, path + [q])):
                return found
        state[p] = "done"
        return None

    for p in PACKAGES:
        if p not in state and (found := visit(p, [p])):
            return found
    return None


def test_packages_do_not_import_each_other_in_a_cycle():
    assert _cycle(_imports()) is None


def test_the_web_server_does_not_import_the_eval_package():
    """シナリオは evals の外(scenarios)にある。line_runtime から evals への矢印が戻ると循環になる(R10)。"""
    assert "evals" not in _imports()["line_runtime"]


def test_resume_intake_reads_env_without_line_runtime():
    """.env の読み込みは aica_env にある。resume_intake から line_runtime への矢印が戻ると循環になる(R13)。"""
    assert "line_runtime" not in _imports()["resume_intake"]


def test_shared_packages_stay_at_the_bottom():
    """aica_env はどこにも依存しない。scenarios は dip の関数だけに依存する。"""
    deps = _imports()
    assert deps["aica_env"] == set()
    assert deps["scenarios"] == {"dip_mcp"}
