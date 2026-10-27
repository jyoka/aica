"""aica の移植版(dip_mcp/baito/harness.py)を、この参照資料の検査で回すための入口(PRD §11-4)。

    BAITO_HARNESS=aica_port python3 unit_checks_v4.py
    BAITO_HARNESS=aica_port python3 simulate.py v4   # results_v4.json と transcripts/ を上書きするので、写しの中で回す

import した人が本体のモジュールそのものを受け取る(単体チェックの節27は、モジュールの _apply_ext を差し替える)。
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
if os.environ.get("AICA_ROOT"):
    _ROOT = os.environ["AICA_ROOT"]
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dip_mcp.baito import harness as _harness

sys.modules[__name__] = _harness
