"""Tests for the scan stage: tiling, spans, gaps, trust gate."""

import os
import tempfile
import unittest

import tests.fixtures as fx
from hdvrescue import scan, TS


def write_tmp(data, suffix=".mpeg"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.write(fd, data)
    os.close(fd)
    return path


class ScanCase(unittest.TestCase):
    def setUp(self):
        self._paths = []

    def tearDown(self):
        for p in self._paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def scan_bytes(self, data):
        p = write_tmp(data)
        self._paths.append(p)
        return scan.scan([p]).sources[0]

    def assert_tiles(self, sr):
        pieces = sorted([(s.byte_start, s.byte_end) for s in sr.spans]
                        + [(g.byte_start, g.byte_end) for g in sr.gaps])
        cur = 0
        for a, b in pieces:
            self.assertEqual(a, cur)
            cur = b
        self.assertEqual(cur, sr.size)


class TestScanHappy(ScanCase):
    def test_clean_single_span(self):
        sr = self.scan_bytes(fx.recording())
        self.assertEqual(len(sr.spans), 1)
        self.assertEqual(len(sr.gaps), 0)
        sp = sr.spans[0]
        self.assertEqual((sp.byte_start, sp.byte_end), (0, sr.size))
        self.assertEqual(sp.confidence, "high")
        self.assertEqual(sp.aux.first.date, [2007, 10, 18])
        self.assertEqual(sp.aux.first.time, [9, 14, 3])
        self.assertEqual(sp.pmt.aux_type, 0xA1)
        self.assertEqual(sp.pmt.pcr_pid, fx.PCR_PID)
        self.assertTrue(sp.pcr.monotonic)
        self.assertEqual(sr.summary["coverage_pct"], 100.0)
        self.assert_tiles(sr)

    def test_long_recording_stays_one_span(self):
        sr = self.scan_bytes(fx.recording(n_frames=40, pcr_step=90000))
        self.assertEqual(len(sr.spans), 1)
        self.assertEqual(sr.spans[0].terminated_by, "eof")


class TestScanTiling(ScanCase):
    def test_spliced_junk_tiles_exactly(self):
        data = fx.splice_junk(fx.recording(n_frames=8), 5 * TS, fx.junk_bytes(1000))
        sr = self.scan_bytes(data)
        self.assert_tiles(sr)
        self.assertTrue(any(g.kind == "resync" for g in sr.gaps))
        self.assertGreaterEqual(len(sr.spans), 2)

    def test_sub_stride_tail(self):
        data = fx.truncate_to(fx.recording(n_frames=5), 5 * TS + 77)
        sr = self.scan_bytes(data)
        self.assert_tiles(sr)
        self.assertTrue(any(g.kind == "trailing" for g in sr.gaps))

    def test_pure_junk_no_spans(self):
        sr = self.scan_bytes(fx.junk_bytes(6000))
        self.assertEqual(len(sr.spans), 0)
        self.assertTrue(sr.needs_attention)
        self.assert_tiles(sr)

    def test_resync_finds_run_far_away(self):
        # A real run 1.5 MB past the corruption must still be found, not given up.
        data = (fx.recording(n_frames=4) + fx.junk_bytes(1_500_000)
                + fx.recording(n_frames=4, date=(2009, 5, 5), pcr_start=9_000_000))
        sr = self.scan_bytes(data)
        self.assert_tiles(sr)
        self.assertGreaterEqual(len(sr.spans), 2)
        self.assertTrue(any(g.kind == "resync" for g in sr.gaps))


class TestScanTrust(ScanCase):
    def test_coincidental_sync_is_gap_not_span(self):
        # Strided 0x47 with no PAT/PMT/PCR/AUX must be an unstructured gap.
        run = b"".join(fx.payload_packet(0x100, fx.junk_bytes(40, seed=i), cc=i & 0xF)
                       for i in range(20))
        sr = self.scan_bytes(run)
        self.assertEqual(len(sr.spans), 0)
        self.assertTrue(any(g.kind == "unstructured" for g in sr.gaps))
        self.assert_tiles(sr)

    def test_short_island_with_aux_is_surfaced(self):
        island = (fx.pat_packet(fx.PMT_PID)
                  + fx.pmt_packet(fx.DEFAULT_STREAMS, fx.PCR_PID)
                  + fx.pcr_only_packet(fx.PCR_PID, 50000)
                  + fx.sony_aux_pes_packet(fx.AUX_A1_PID, (2011, 6, 1), (8, 0, 0)))
        data = fx.junk_bytes(2000) + island + fx.junk_bytes(2000)
        sr = self.scan_bytes(data)
        self.assert_tiles(sr)
        self.assertEqual(len(sr.spans), 1)
        self.assertEqual(sr.spans[0].aux.first.date, [2011, 6, 1])


class TestScanBoundaries(ScanCase):
    def test_over_record_two_dates_split(self):
        a = fx.recording(date=(2007, 10, 18), start_time=(9, 14, 3), n_frames=6)
        b = fx.recording(date=(2007, 3, 2), start_time=(14, 2, 13), n_frames=6,
                         pcr_start=500000)
        sr = self.scan_bytes(a + b)
        self.assertGreaterEqual(len(sr.spans), 2)
        self.assertEqual(sorted(sr.summary["distinct_aux_dates"]),
                         ["2007-03-02", "2007-10-18"])
        self.assert_tiles(sr)

    def test_dropped_packet_cc_break_splits_without_gap(self):
        data = fx.drop_packets(fx.recording(n_frames=10), 15, 1)
        sr = self.scan_bytes(data)
        self.assertEqual(len(sr.gaps), 0)              # alignment preserved
        self.assertEqual(len(sr.spans), 2)
        self.assertTrue(any(s.terminated_by == "cc_break" for s in sr.spans))
        self.assert_tiles(sr)


class TestScanFraming(ScanCase):
    def test_multipacket_pmt_in_recording(self):
        p1, p2 = fx.pmt_packets_multi(fx.DEFAULT_STREAMS, fx.PCR_PID)
        stream = bytearray(fx.pat_packet(fx.PMT_PID, cc=0))
        stream += p1 + p2
        cc = {0x810: -1, 0x811: -1}
        for f in range(8):
            stream += fx.pcr_only_packet(fx.PCR_PID, 100000 + f * 3600)
            cc[0x810] = (cc[0x810] + 1) & 0xF
            stream += fx.payload_packet(fx.VIDEO_PID, b"\x00\x00\x01\xe0",
                                        pusi=True, cc=cc[0x810])
            cc[0x811] = (cc[0x811] + 1) & 0xF
            stream += fx.sony_aux_pes_packet(fx.AUX_A1_PID, (2012, 5, 5),
                                             (8, 0, 0), cc=cc[0x811])
        sr = self.scan_bytes(bytes(stream))
        self.assertEqual(len(sr.spans), 1)
        self.assertEqual(sr.spans[0].pmt.aux_type, 0xA1)
        self.assertTrue(sr.spans[0].pmt.reassembled)

    def test_non_188_is_flagged_not_built(self):
        sr = self.scan_bytes(fx.reframe_192(fx.recording(), corrupt_head=3))
        self.assertEqual(sr.framing.stride, 192)
        self.assertTrue(sr.needs_attention)
        self.assertEqual(len(sr.spans), 0)
        self.assertTrue(any(g.kind == "non_188" for g in sr.gaps))
        self.assert_tiles(sr)


class TestScanMultiSource(unittest.TestCase):
    def test_two_sources(self):
        paths = []
        try:
            paths.append(write_tmp(fx.recording(date=(2007, 10, 18))))
            paths.append(write_tmp(fx.recording(date=(2007, 10, 18),
                                                start_time=(9, 20, 0),
                                                pcr_start=9_000_000)))
            rep = scan.scan(paths)
            self.assertEqual(len(rep.sources), 2)
            self.assertEqual(rep.sources[0].id, 0)
            self.assertEqual(rep.sources[1].id, 1)
            ids = [s.span_id for s in rep.all_spans()]
            self.assertIn("0:000", ids)
            self.assertIn("1:000", ids)
        finally:
            for p in paths:
                os.unlink(p)


if __name__ == "__main__":
    unittest.main()
