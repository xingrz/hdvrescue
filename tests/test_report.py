"""Tests for planreport: Markdown rendering of a plan, deterministic + plan-only."""

import os
import shutil
import tempfile
import unittest

from hdvrescue import planreport, model


def member(span, treatment=None, reason="", date=(2007, 7, 18), time=(10, 54, 0),
           b0=0, b1=2_500_000, dur=10.0):
    join = None
    if treatment is not None:
        join = model.Join(treatment=treatment, provenance="p", confidence=0.9,
                          reason=reason)
    return model.Member(span=span, join=join, source_file="/abs/CLIP001.mpeg",
                        aux={"date": list(date), "time": list(time)},
                        pcr_range_sec=[0.0, dur], bytes=[b0, b1])


def plan_of(outputs, splits=None, unplaced=None, sources=None):
    return model.Plan(
        model.PLAN_VERSION,
        sources or [{"id": 0, "path": "/abs/CLIP001.mpeg", "size": 48_000_000}],
        outputs, splits=splits or [], unplaced=unplaced or [])


class TestFormatters(unittest.TestCase):
    def test_size(self):
        self.assertEqual(planreport._fmt_size(2_500_000), "2.38 MB")
        self.assertEqual(planreport._fmt_size(2 << 30), "2.00 GB")
        self.assertEqual(planreport._fmt_size(512), "512 B")

    def test_dur(self):
        self.assertEqual(planreport._fmt_dur(29), "0:29")
        self.assertEqual(planreport._fmt_dur(65), "1:05")
        self.assertEqual(planreport._fmt_dur(3661), "1:01:01")
        self.assertEqual(planreport._fmt_dur(None), "?")

    def test_source_id_of(self):
        self.assertEqual(planreport._source_id_of("3:017"), 3)
        self.assertIsNone(planreport._source_id_of("weird"))

    def test_pipe_is_escaped(self):
        self.assertEqual(planreport._esc("a|b"), "a\\|b")


class TestRender(unittest.TestCase):
    def _one_output_plan(self):
        out = model.Output(name="2007-07-18_10-54-00.m2t", enabled=True, members=[
            member("0:000", None, b0=0, b1=2_500_000, time=(10, 54, 0)),
            member("0:001", "verbatim", b0=2_500_000, b1=5_000_000, time=(10, 54, 10)),
            member("0:002", "discontinuity-marker", "same-source same-day, PCR +2.00s",
                   b0=5_000_000, b1=7_600_000, time=(10, 54, 20)),
        ])
        return plan_of([out])

    def test_pure_function_no_io(self):
        # Rendering twice yields byte-identical output (no clock, no randomness).
        plan = self._one_output_plan()
        self.assertEqual(planreport.render_markdown(plan),
                         planreport.render_markdown(plan))

    def test_output_section_lists_members_and_joins(self):
        md = planreport.render_markdown(self._one_output_plan())
        self.assertIn("# hdvrescue", md)
        self.assertIn("### 2007-07-18_10-54-00.m2t", md)
        self.assertIn("`0:000`", md)
        self.assertIn("`0:002`", md)
        self.assertIn("首段", md)           # first member
        self.assertIn("verbatim", md)
        self.assertIn("marker — same-source same-day, PCR +2.00s", md)
        self.assertIn("10:54:20", md)        # timecode column

    def test_splits_render_with_legend(self):
        plan = plan_of(
            [model.Output("a.m2t", True, [member("0:000")])],
            splits=[model.Split("0:004", "0:005", "pcr-discontinuity",
                                "PCR jumps +300s")])
        md = planreport.render_markdown(plan)
        self.assertIn("## 拆分点", md)
        self.assertIn("`pcr-discontinuity`", md)
        self.assertIn("PCR jumps +300s", md)
        self.assertIn("进度条无法拖动", md)   # legend for the code that appeared

    def test_unplaced_section(self):
        plan = plan_of(
            [model.Output("a.m2t", True, [member("0:000")])],
            unplaced=[model.Unplaced("2:007", "low", "below medium confidence")])
        md = planreport.render_markdown(plan)
        self.assertIn("## 未放置片段", md)
        self.assertIn("`2:007`", md)
        self.assertIn("below medium confidence", md)

    def test_duplicate_hint_when_two_outputs_share_timecode(self):
        a = model.Output("2007-07-18_10-54-00.m2t", True, [member("0:000")])
        b = model.Output("2007-07-18_10-54-00_a.m2t", True, [member("1:000")])
        md = planreport.render_markdown(plan_of([a, b]))
        self.assertIn("## 可能的重复", md)
        self.assertIn("hdvrescue dedup", md)

    def test_disabled_output_is_separated(self):
        a = model.Output("keep.m2t", True, [member("0:000")])
        b = model.Output("drop.m2t", False, [member("1:000")])
        md = planreport.render_markdown(plan_of([a, b]))
        self.assertIn("## 已禁用", md)
        self.assertIn("`drop.m2t`", md)

    def test_multi_source_adds_source_column(self):
        out = model.Output("x.m2t", True, [
            member("0:000", None), member("1:000", "discontinuity-marker")])
        md = planreport.render_markdown(plan_of(
            [out],
            sources=[{"id": 0, "path": "/abs/A.mpeg", "size": 10},
                     {"id": 1, "path": "/abs/B.mpeg", "size": 10}]))
        self.assertIn("来源", md)
        self.assertIn("`B.mpeg`", md)

    def test_member_without_aux_renders_dash(self):
        out = model.Output("x.m2t", True, [
            model.Member(span="0:000", join=None, source_file="/a", aux=None,
                         pcr_range_sec=None, bytes=[0, 100])])
        md = planreport.render_markdown(plan_of([out]))
        self.assertIn("—", md)               # no timecode -> dash, no crash

    def test_trailing_newline_and_no_blank_pileup(self):
        md = planreport.render_markdown(self._one_output_plan())
        self.assertTrue(md.endswith("\n"))
        self.assertNotIn("\n\n\n", md)


class TestMain(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan_path = os.path.join(self.d, "plan.json")
        out = model.Output("2007-07-18_10-54-00.m2t", True, [member("0:000")])
        model.save_plan(plan_of([out]), self.plan_path)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_default_writes_sibling_md(self):
        rc = planreport.main([self.plan_path])
        self.assertEqual(rc, 0)
        md_path = os.path.join(self.d, "plan.md")
        self.assertTrue(os.path.isfile(md_path))
        self.assertIn("# hdvrescue", open(md_path, encoding="utf-8").read())

    def test_missing_plan_returns_2(self):
        self.assertEqual(planreport.main([os.path.join(self.d, "nope.json")]), 2)


if __name__ == "__main__":
    unittest.main()
