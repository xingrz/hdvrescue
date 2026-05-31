"""Tests for plan: same-day merging, seek-safety, and join classification."""

import unittest

from hdvrescue import plan, model
from hdvrescue.plan import DEFAULT_PARAMS

PMT = model.Pmt(pmt_pid=129, pcr_pid=308, streams=[[2, 2064], [3, 2068]],
                stream_type_set=[2, 3, 160, 161], aux_pid=2065, aux_type=161,
                video_pid=2064, version=0, signature="st={...};aux=0xa1")


def span(sid, src, b0, b1, date, time, pcr0, pcr1, term="corruption",
         pmt=PMT, conf="high"):
    pcr = model.Pcr(pid=308, first=pcr0, last=pcr1, sample_count=10,
                    duration_sec=(pcr1 - pcr0) / 90000.0, monotonic=True)
    aux = model.Aux(pid=2065,
                    first=model.AuxSample(date=list(date), time=list(time)),
                    last=model.AuxSample(date=list(date), time=list(time)))
    return model.Span(span_id=sid, source_id=src, byte_start=b0, byte_end=b1,
                      packet_count=(b1 - b0) // 188, pmt=pmt, pcr=pcr,
                      pcr_pid_last_cc=0, aux=aux, first_pusi_offset_by_pid={},
                      confidence=conf, reasons=["pmt", "pcr-monotonic", "aux-anchor"],
                      terminated_by=term)


class TestClassifyJoin(unittest.TestCase):
    def J(self, a, b):
        return plan.classify_join(a, b, DEFAULT_PARAMS)

    def test_byte_adjacent_same_day_merges_despite_aux_boundary(self):
        # The pause/resume case: byte-adjacent, same date, AUX time jumps.
        a = span("0:0", 0, 0, 1000, (2007, 7, 18), (10, 54, 1), 100, 200,
                 term="aux_recording_boundary")
        b = span("0:1", 0, 1000, 2000, (2007, 7, 18), (10, 54, 14), 203, 400,
                 term="aux_recording_boundary")
        kind = self.J(a, b)
        self.assertEqual(kind[0], "join")
        self.assertEqual(kind[1], "verbatim")        # contiguous -> no marker

    def test_byte_adjacent_different_day_splits(self):
        a = span("0:0", 0, 0, 1000, (2007, 7, 18), (10, 0, 0), 100, 200,
                 term="aux_recording_boundary")
        b = span("0:1", 0, 1000, 2000, (2007, 10, 12), (16, 0, 0), 203, 400)
        kind = self.J(a, b)
        self.assertEqual((kind[0], kind[1]), ("split", "aux-date-mismatch"))

    def test_same_source_gap_pcr_continuous_joins(self):
        # A corruption gap, but PCR stays seekable -> merge with a marker.
        a = span("0:0", 0, 0, 1000, (2007, 7, 18), (10, 0, 0), 100, 200)
        b = span("0:1", 0, 1500, 2500, (2007, 7, 18), (10, 0, 5), 360, 500)
        kind = self.J(a, b)        # PCR +1.8s, within 30s
        self.assertEqual((kind[0], kind[1]), ("join", "discontinuity-marker"))
        self.assertEqual(kind[2], "same-source-gap")

    def test_same_source_pcr_reset_splits_as_unseekable(self):
        # Two sessions captured into one file: PCR restarts -> not seekable.
        a = span("0:0", 0, 0, 1000, (2007, 7, 18), (10, 0, 0), 9_000_000, 9_100_000)
        b = span("0:1", 0, 1500, 2500, (2007, 7, 18), (12, 0, 0), 100, 200)
        kind = self.J(a, b)
        self.assertEqual((kind[0], kind[1]), ("split", "pcr-discontinuity"))

    def test_same_source_pcr_far_forward_splits(self):
        a = span("0:0", 0, 0, 1000, (2007, 7, 18), (10, 0, 0), 100, 200)
        b = span("0:1", 0, 1500, 2500, (2007, 7, 18), (10, 30, 0),
                 200 + 30 * 60 * 90000, 200 + 30 * 60 * 90000 + 100)
        kind = self.J(a, b)        # +30 min PCR jump
        self.assertEqual((kind[0], kind[1]), ("split", "pcr-discontinuity"))

    def test_cross_source_uses_aux_window(self):
        a = span("0:0", 0, 0, 1000, (2007, 7, 18), (10, 0, 0), 100, 200)
        b = span("1:0", 1, 0, 1000, (2007, 7, 18), (10, 0, 2), 5_000_000, 5_001_000)
        kind = self.J(a, b)        # +2s AUX, different PCR clocks
        self.assertEqual((kind[0], kind[1]), ("join", "discontinuity-marker"))
        self.assertEqual(kind[2], "cross-source")

    def test_cross_source_aux_too_far_splits(self):
        a = span("0:0", 0, 0, 1000, (2007, 7, 18), (10, 0, 0), 100, 200)
        b = span("1:0", 1, 0, 1000, (2007, 7, 18), (10, 5, 0), 5_000_000, 5_001_000)
        kind = self.J(a, b)        # +5 min AUX, beyond chain window
        self.assertEqual((kind[0], kind[1]), ("split", "aux-elapsed-mismatch"))


def report_of(*spans):
    by_src = {}
    for s in spans:
        by_src.setdefault(s.source_id, []).append(s)
    srcs = [model.SourceReport(id=sid, path="/tmp/src%d.mpeg" % sid, size=10 ** 9,
                               hash=None, framing=None, needs_attention=False,
                               spans=sp, gaps=[], summary={})
            for sid, sp in sorted(by_src.items())]
    return model.Report(model.REPORT_VERSION, {}, srcs)


class TestMakePlan(unittest.TestCase):
    def test_pause_resume_run_collapses_to_one_output(self):
        # Five byte-adjacent same-day takes (the user's 9:001-9:005 shape).
        spans, b, pcr = [], 0, 100
        for i in range(5):
            s = span("0:%d" % i, 0, b, b + 1000, (2007, 7, 18),
                     (10, 54, i * 10), pcr, pcr + 100,
                     term="aux_recording_boundary")
            spans.append(s)
            b += 1000
            pcr += 103   # PCR stays continuous frame-to-frame
        pl = plan.make_plan(report_of(*spans))
        self.assertEqual(len(pl.outputs), 1)
        self.assertEqual(len(pl.outputs[0].members), 5)
        self.assertEqual(pl.outputs[0].name, "2007-07-18_10-54-00.m2t")
        # every join is verbatim (contiguous), no recording-boundary splits
        joins = [m.join.treatment for m in pl.outputs[0].members[1:]]
        self.assertEqual(joins, ["verbatim"] * 4)
        self.assertFalse(any(s.code == "recording-boundary" for s in pl.splits))

    def test_two_sessions_same_day_stay_separate(self):
        a = span("0:0", 0, 0, 1000, (2007, 7, 18), (10, 0, 0), 100, 9_000_000)
        b = span("0:1", 0, 5000, 6000, (2007, 7, 18), (12, 0, 0), 100, 200)
        pl = plan.make_plan(report_of(a, b))
        self.assertEqual(len(pl.outputs), 2)


if __name__ == "__main__":
    unittest.main()
