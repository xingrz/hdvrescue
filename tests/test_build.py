"""Tests for the build stage: byte-fidelity, markers, CC, self-verify gating."""

import os
import tempfile
import unittest

import tests.fixtures as fx
from hdvrescue import scan, plan, build, model, TS


def write_tmp(data, suffix=".mpeg"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.write(fd, data)
    os.close(fd)
    return path


class BuildCase(unittest.TestCase):
    def setUp(self):
        self._paths = []
        self._dirs = []

    def tearDown(self):
        for p in self._paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        for d in self._dirs:
            for f in os.listdir(d):
                os.unlink(os.path.join(d, f))
            os.rmdir(d)

    def src(self, data):
        p = write_tmp(data)
        self._paths.append(p)
        return p

    def outdir(self):
        d = tempfile.mkdtemp()
        self._dirs.append(d)
        return d


class TestBuildFidelity(BuildCase):
    def test_single_span_is_byte_identical(self):
        data = fx.recording()
        p = self.src(data)
        rep = scan.scan([p])
        pl = plan.make_plan(rep)
        outd = self.outdir()
        res = build.build(pl, rep, outd)
        out = os.path.join(outd, res[0]["name"])
        with open(out, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(res[0]["markers"], 0)

    def test_cross_source_one_marker_cc_not_forged(self):
        a = fx.recording(date=(2008, 1, 1), start_time=(10, 0, 0), n_frames=8,
                         pcr_step=90000)
        b = fx.recording(date=(2008, 1, 1), start_time=(10, 0, 8), n_frames=8,
                         pcr_start=5000, pcr_step=90000)
        pa, pb = self.src(a), self.src(b)
        rep = scan.scan([pa, pb])
        pl = plan.make_plan(rep)
        self.assertEqual(len(pl.outputs), 1)
        self.assertEqual(len(pl.outputs[0].members), 2)
        outd = self.outdir()
        res = build.build(pl, rep, outd)
        out = os.path.join(outd, res[0]["name"])
        with open(out, "rb") as f:
            built = f.read()
        # output = A verbatim + ONE marker + B verbatim (CC never forged).
        self.assertEqual(res[0]["markers"], 1)
        self.assertEqual(len(built), len(a) + TS + len(b))
        self.assertEqual(built[:len(a)], a)
        self.assertEqual(built[len(a) + TS:], b)

    def test_marker_repeats_dedicated_pcr_cc(self):
        # A dedicated PCR-only PID carries a constant CC; the marker must repeat
        # it, not force (15+1)&0xF = 0.
        a = fx.recording(n_frames=6, pcr_cc=11)
        b = fx.recording(start_time=(9, 14, 9), n_frames=6, pcr_start=5000,
                         pcr_cc=11)
        pa, pb = self.src(a), self.src(b)
        rep = scan.scan([pa, pb])
        pl = plan.make_plan(rep)
        outd = self.outdir()
        res = build.build(pl, rep, outd)
        with open(os.path.join(outd, res[0]["name"]), "rb") as f:
            built = f.read()
        marker = built[len(a):len(a) + TS]
        from hdvrescue import ts
        self.assertEqual(ts.packet_pid(marker), fx.PCR_PID)
        self.assertEqual(ts.packet_cc(marker), 11)
        self.assertTrue(ts.disc_indicator(marker))


class TestBuildSafety(BuildCase):
    def test_aborts_on_mid_span_corruption(self):
        data = fx.recording()
        p = self.src(data)
        rep = scan.scan([p])
        pl = plan.make_plan(rep)
        # Corrupt a sync byte deep inside the (single) span, after scanning.
        with open(p, "r+b") as f:
            f.seek(10 * TS)          # a packet boundary well inside the span
            f.write(b"\x00")
        outd = self.outdir()
        with self.assertRaises(build.BuildError):
            build.build(pl, rep, outd)

    def test_refuses_low_confidence_span(self):
        # Hand-build a minimal report+plan with a low-confidence span.
        sp = model.Span(
            span_id="0:000", source_id=0, byte_start=0, byte_end=TS,
            packet_count=1, pmt=None, pcr=None, pcr_pid_last_cc=None,
            aux=model.Aux(), first_pusi_offset_by_pid={}, confidence="low",
            reasons=["pcr-only"], terminated_by="eof")
        p = self.src(fx.recording())
        sr = model.SourceReport(id=0, path=p, size=os.path.getsize(p), hash=None,
                                framing=None, needs_attention=False, spans=[sp],
                                gaps=[], summary={})
        rep = model.Report(model.REPORT_VERSION, {}, [sr])
        pl = model.Plan(model.PLAN_VERSION, [{"id": 0, "path": p, "size": sr.size}],
                        [model.Output("x.m2t", True,
                                      [model.Member("0:000", None)])])
        outd = self.outdir()
        with self.assertRaises(build.BuildError):
            build.build(pl, rep, outd)

    def test_on_exist_skip_and_error(self):
        data = fx.recording()
        p = self.src(data)
        rep = scan.scan([p])
        pl = plan.make_plan(rep)
        outd = self.outdir()
        build.build(pl, rep, outd)
        # Second build: error by default, skip when asked.
        with self.assertRaises(build.BuildError):
            build.build(pl, rep, outd, on_exist="error")
        res = build.build(pl, rep, outd, on_exist="skip")
        self.assertTrue(res[0].get("skipped"))


if __name__ == "__main__":
    unittest.main()
