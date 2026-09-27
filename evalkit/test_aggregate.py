"""python3 -m unittest discover -s evalkit(リポジトリ直下で実行)"""

from __future__ import annotations

import unittest

from aggregate import diff, diff_md, stat, summarize, summary_md


def run(case, rep, **metrics):
    return {"case": case, "rep": rep, "status": "ok", "metrics": metrics}


def doc(runs, **config):
    return {"harness": "t", "config": config, "directions": {"pass": "up", "cost": "down"}, "runs": runs}


class StatTest(unittest.TestCase):
    def test_rate_interval_does_not_collapse_at_100_percent(self):
        s = stat([1.0, 1.0, 1.0], rate=True)
        self.assertEqual(s["mean"], 1.0)
        self.assertLess(s["lo"], 0.6)  # n=3 で全勝でも下限はかなり低い
        self.assertGreater(s["se"], 0)

    def test_numeric_single_value_has_zero_se(self):
        self.assertEqual(stat([2.0], rate=False)["se"], 0.0)


class SummarizeTest(unittest.TestCase):
    def test_errors_excluded_and_none_skipped(self):
        d = doc([
            run("a", 0, **{"pass": True, "cost": 1.0, "secs": None}),
            run("a", 1, **{"pass": False, "cost": 3.0, "secs": 5.0}),
            {"case": "b", "rep": 0, "status": "error", "error": "boom"},
        ])
        s = summarize(d)
        self.assertEqual(s["reps"], 2)
        self.assertEqual(len(s["errors"]), 1)
        self.assertEqual(s["overall"]["pass"]["mean"], 0.5)
        self.assertTrue(s["rate"]["pass"])
        self.assertFalse(s["rate"]["cost"])
        self.assertEqual(s["overall"]["secs"]["n"], 1)
        self.assertEqual(s["by_case"]["b"]["pass"]["n"], 0)  # エラーだけのケースも表に残す
        md = summary_md(d)
        self.assertIn("実行エラー b#0", md)
        self.assertIn("| a |", md)


class DiffTest(unittest.TestCase):
    def test_large_consistent_change_is_flagged(self):
        before = doc([run(c, r, **{"pass": False, "cost": 1.0 + 0.01 * r}) for c in "abcdef" for r in range(3)])
        after = doc([run(c, r, **{"pass": True, "cost": 2.0 + 0.01 * r}) for c in "abcdef" for r in range(3)])
        rows = {r["metric"]: r for r in diff(before, after)["rows"]}
        self.assertEqual(rows["pass"]["verdict"], "改善")
        self.assertEqual(rows["cost"]["verdict"], "悪化")

    def test_small_change_is_within_noise(self):
        before = doc([run("a", r, **{"pass": r != 0}) for r in range(3)])
        after = doc([run("a", r, **{"pass": r not in (0, 1)}) for r in range(3)])
        d = diff(before, after)
        self.assertEqual(d["rows"][0]["verdict"], "-")
        self.assertEqual([(w["case"], w["metric"]) for w in d["worse_cases"]], [("a", "pass")])

    def test_overall_compares_only_shared_cases(self):
        before = doc([run(c, r, **{"pass": c == "easy", "cost": 1.0 if c == "easy" else 9.0 + r})
                      for c in ("easy", "hard") for r in range(3)])
        after = doc([run("easy", r, **{"pass": True, "cost": 1.0 + r * 0.01}) for r in range(3)])
        d = diff(before, after)
        rows = {r["metric"]: r for r in d["rows"]}
        self.assertEqual(rows["pass"]["delta"], 0)  # hard が抜けただけで改善に見えない
        self.assertEqual(rows["cost"]["verdict"], "-")
        self.assertEqual(d["only_before"], ["hard"])
        self.assertIn("前だけにあるケース", diff_md(before, after))

    def test_no_verdict_without_measured_variance(self):
        before = doc([run("a", 0, cost=5.0)])
        after = doc([run("a", 0, cost=5.1)])
        self.assertEqual(diff(before, after)["rows"][0]["verdict"], "判定不可")

    def test_metric_without_direction_is_never_judged(self):
        before = doc([run("a", r, turns=2.0 + r * 0.01) for r in range(3)])
        after = doc([run("a", r, turns=9.0 + r * 0.01) for r in range(3)])
        self.assertEqual(diff(before, after)["rows"][0]["verdict"], "-")
        self.assertIn("| turns |", diff_md(before, after))


if __name__ == "__main__":
    unittest.main()
