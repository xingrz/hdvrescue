"""Tests for dedup: candidate grouping + byte-level containment/divergence."""

import os
import shutil
import tempfile
import unittest

from hdvrescue import dedup, model

PMT = model.Pmt(pmt_pid=129, pcr_pid=308, streams=[[2, 2064]],
                stream_type_set=[2, 3, 160, 161], aux_pid=2065, aux_type=161,
                video_pid=2064, version=0, signature="sig")


def dspan(sid, src, b0, b1, date, time):
    aux = model.Aux(pid=2065,
                    first=model.AuxSample(date=list(date), time=list(time)),
                    last=model.AuxSample(date=list(date), time=list(time)))
    return model.Span(span_id=sid, source_id=src, byte_start=b0, byte_end=b1,
                      packet_count=(b1 - b0) // 188, pmt=PMT, pcr=None,
                      pcr_pid_last_cc=0, aux=aux, first_pusi_offset_by_pid={},
                      confidence="high", reasons=[], terminated_by="eof")


class TestDedup(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        X = (bytes(range(256)) * 12)[:3000]          # primary content, 3000 B
        Y = (bytes(reversed(range(256))) * 8)[:2000]  # a second moment, 2000 B
        Xdiv = bytearray(X[:2500])
        Xdiv[1500] ^= 0xFF                            # flip one byte at 1500
        src0 = X + Y
        src1 = X[:1000] + bytes(Xdiv) + Y
        self.p0 = os.path.join(self.d, "src0.mpeg")
        self.p1 = os.path.join(self.d, "src1.mpeg")
        open(self.p0, "wb").write(src0)
        open(self.p1, "wb").write(src1)
        s0 = model.SourceReport(0, self.p0, len(src0), None, None, False, [
            dspan("0:0", 0, 0, 3000, (2010, 1, 1), (8, 0, 0)),     # T1 primary
            dspan("0:1", 0, 3000, 5000, (2010, 1, 1), (9, 0, 0)),  # T2
        ], [], {})
        s1 = model.SourceReport(1, self.p1, len(src1), None, None, False, [
            dspan("1:0", 1, 0, 1000, (2010, 1, 1), (8, 0, 0)),     # T1 contained
            dspan("1:2", 1, 1000, 3500, (2010, 1, 1), (8, 0, 0)),  # T1 diverges
            dspan("1:3", 1, 3500, 5500, (2010, 1, 1), (9, 0, 0)),  # T2 identical
        ], [], {})
        self.report = model.Report(model.REPORT_VERSION, {}, [s0, s1])

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_groups_by_shared_timecode(self):
        groups = dedup.candidate_groups(self.report)
        tcs = sorted(g[0] for g in groups)
        self.assertEqual(len(groups), 2)         # T1 (3 spans) and T2 (2 spans)

    def test_byte_verdicts(self):
        findings = dedup.analyze(self.report)
        verdicts = {}
        for g in findings:
            for c in g["candidates"]:
                verdicts[c["span"]] = c
        # 1:0 is a byte-prefix of the primary; 1:2 diverges at byte 1500.
        self.assertEqual(verdicts["1:0"]["verdict"], "contained")
        self.assertTrue(verdicts["1:0"]["drop_safe"])
        self.assertEqual(verdicts["1:2"]["verdict"], "diverges")
        self.assertFalse(verdicts["1:2"]["drop_safe"])
        self.assertEqual(verdicts["1:2"]["diff_offset"], 1500)
        # The T2 group has one identical pair (the equal-length copy).
        by_verdict = {c["verdict"] for g in findings for c in g["candidates"]}
        self.assertEqual(by_verdict, {"contained", "diverges", "identical"})

    def test_md5_optional(self):
        findings = dedup.analyze(self.report, want_md5=True)
        self.assertTrue(all(g["primary"]["md5"] for g in findings))

    def test_report_text_lists_droppable(self):
        text = dedup.format_report(dedup.analyze(self.report))
        self.assertIn("safe to drop", text)
        self.assertIn("1:0", text)


if __name__ == "__main__":
    unittest.main()
