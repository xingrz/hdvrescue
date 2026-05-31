"""Unit tests for the byte-level primitives (ts, psi, aux, timecal, model)."""

import unittest

from hdvrescue import ts, psi, aux, timecal, model
from hdvrescue import PCR_MAX, PCR_HZ, TS, SYNC
import tests.fixtures as fx


class TestTs(unittest.TestCase):
    def test_with_cc_touches_only_nibble(self):
        pkt = fx.payload_packet(0x810, b"hello", pusi=True, cc=4)
        out = ts.with_cc(pkt, 9)
        self.assertEqual(ts.packet_cc(out), 9)
        # Every byte except byte 3 is identical.
        self.assertEqual(pkt[:3], out[:3])
        self.assertEqual(pkt[4:], out[4:])
        self.assertEqual(pkt[3] & 0xF0, out[3] & 0xF0)

    def test_payload_start_variants(self):
        p1 = fx.payload_packet(0x810, b"x", cc=0)          # AFC=1
        self.assertEqual(ts.packet_payload_start(p1), 4)
        p2 = fx.pcr_only_packet(0x134, 1000, cc=0)         # AFC=2, no payload
        self.assertIsNone(ts.packet_payload_start(p2))
        p3 = fx.video_pcr_packet(0x810, 1000, b"y", cc=0)  # AFC=3
        self.assertEqual(ts.packet_payload_start(p3), 5 + p3[4])

    def test_pcr_roundtrip_and_dedicated_pid(self):
        for base in (0, 1, 100000, PCR_MAX - 1, (1 << 30) + 7):
            pkt = fx.pcr_only_packet(0x134, base, cc=0)
            self.assertEqual(ts.packet_pcr(pkt), base)
            self.assertEqual(ts.packet_afc(pkt), 2)
            self.assertFalse(ts.packet_has_payload(pkt))

    def test_disc_indicator_guarded(self):
        plain = fx.pcr_only_packet(0x134, 5, cc=0)
        self.assertFalse(ts.disc_indicator(plain))
        marked = fx.pcr_only_packet(0x134, 5, cc=0, discontinuity=True)
        self.assertTrue(ts.disc_indicator(marked))
        # A payload-only packet has no flags byte to misread.
        self.assertFalse(ts.disc_indicator(fx.payload_packet(0x810, b"z")))

    def test_make_disc_marker(self):
        m = ts.make_disc_marker(0x134, 7)
        self.assertEqual(len(m), TS)
        self.assertEqual(m[0], SYNC)
        self.assertEqual(ts.packet_pid(m), 0x134)
        self.assertEqual(ts.packet_afc(m), 2)        # adaptation only
        self.assertFalse(ts.packet_has_payload(m))
        self.assertEqual(ts.packet_cc(m), 7)
        self.assertTrue(ts.disc_indicator(m))

    def test_pcr_diff_wrap(self):
        # Forward wrap: just after the boundary reads as a small positive delta.
        self.assertEqual(ts.pcr_diff(PCR_MAX - 10, 5), 15)
        # A genuine large backward jump is NOT rewritten into a fake wrap.
        d = ts.pcr_diff(PCR_MAX - 10, PCR_MAX // 4)
        self.assertLess(d, 0)
        sec, mono = ts.pcr_delta_sec(1000, 1000 + 5 * PCR_HZ)
        self.assertAlmostEqual(sec, 5.0)
        self.assertTrue(mono)

    def test_iter_packets_stops_at_desync(self):
        data = fx.recording(n_frames=3)
        got = list(ts.iter_packets(data, 0))
        self.assertEqual(len(got), len(data) // TS)
        # Corrupt one byte mid-stream; iteration stops there.
        broken = bytearray(data)
        broken[5 * TS] = 0x00
        got2 = list(ts.iter_packets(bytes(broken), 0))
        self.assertEqual(len(got2), 5)


class TestFraming(unittest.TestCase):
    def test_detect_188(self):
        data = fx.recording()
        fr = ts.detect_framing(data)
        self.assertEqual(fr["stride"], 188)
        self.assertEqual(fr["first_sync"], 0)
        self.assertEqual(fr["confidence"], "high")

    def test_detect_192(self):
        data = fx.reframe_192(fx.recording(), corrupt_head=7)
        fr = ts.detect_framing(data)
        self.assertEqual(fr["stride"], 192)
        self.assertEqual(fr["slot_offset"], 4)
        self.assertEqual(fr["first_sync"], 7 + 4)

    def test_detect_none_on_garbage(self):
        self.assertIsNone(ts.detect_framing(fx.junk_bytes(4000)))


class TestPsi(unittest.TestCase):
    def test_pmt_dedicated_pcr_pid(self):
        pkt = fx.pmt_packet(fx.DEFAULT_STREAMS, fx.PCR_PID)
        ps = ts.packet_payload_start(pkt)
        asm = psi.SectionAssembler()
        sec = asm.feed(ts.packet_pusi(pkt), pkt[ps:])
        pm = psi.parse_pmt_section(sec[0])
        self.assertEqual(pm["pcr_pid"], fx.PCR_PID)
        self.assertNotIn(fx.PCR_PID, [pid for _, pid in pm["streams"]])
        self.assertEqual(pm["aux_pid"], fx.AUX_A1_PID)
        self.assertEqual(pm["aux_type"], 0xA1)
        self.assertFalse(pm["truncated"])

    def test_multipacket_pmt_finds_aux(self):
        p1, p2 = fx.pmt_packets_multi(fx.DEFAULT_STREAMS, fx.PCR_PID)
        asm = psi.SectionAssembler()
        out = []
        for pkt in (p1, p2):
            ps = ts.packet_payload_start(pkt)
            out += asm.feed(ts.packet_pusi(pkt), pkt[ps:])
        self.assertEqual(len(out), 1)        # completed only after packet 2
        pm = psi.parse_pmt_section(out[0])
        self.assertEqual(pm["aux_pid"], fx.AUX_A1_PID)
        self.assertEqual(pm["aux_type"], 0xA1)
        self.assertFalse(pm["truncated"])

    def test_pmt_class_key_ignores_pids_and_version(self):
        a = fx.pmt_packet(fx.DEFAULT_STREAMS, fx.PCR_PID, version=0)
        b = fx.pmt_packet(fx.DEFAULT_STREAMS, fx.PCR_PID, version=7)
        ka = self._class(a)
        kb = self._class(b)
        self.assertEqual(ka, kb)

    def _class(self, pkt):
        ps = ts.packet_payload_start(pkt)
        asm = psi.SectionAssembler()
        sec = asm.feed(ts.packet_pusi(pkt), pkt[ps:])
        return psi.pmt_class_key(psi.parse_pmt_section(sec[0]))


class TestAux(unittest.TestCase):
    def test_roundtrip(self):
        pkt = fx.sony_aux_pes_packet(fx.AUX_A1_PID, (2010, 12, 31), (23, 59, 58))
        ps = ts.packet_payload_start(pkt)
        hit = aux.decode_aux_pes(pkt[ps:])
        self.assertEqual(hit.date, (2010, 12, 31))
        self.assertEqual(hit.time, (23, 59, 58))
        self.assertFalse(hit.truncated_seen)

    def test_no_false_positive_on_random(self):
        self.assertIsNone(aux.sony_aux_decode(fx.junk_bytes(2000, seed=3)))

    def test_requires_pes_context(self):
        # The bare anchor without the 0xBF PES wrapper must not decode via the
        # PES entry point.
        y, mo, d = (2007, 3, 2)
        anchor = bytes([0x63, 0, 0, 0, 0, 0xC0, 0, fx.to_bcd(d), fx.to_bcd(mo),
                        fx.to_bcd(7), 0xFF, fx.to_bcd(1), fx.to_bcd(2),
                        fx.to_bcd(3)])
        self.assertIsNone(aux.decode_aux_pes(anchor))

    def test_truncated_at_edge(self):
        pkt = fx.sony_aux_pes_packet(fx.AUX_A1_PID, (2007, 10, 18), (9, 14, 3))
        ps = ts.packet_payload_start(pkt)
        body = pkt[ps + 6:]                  # PES body (after 00 00 01 BF + len)
        # Cut right after the anchor's 0xFF, before SS/MM/HH.
        idx = bytes(body).index(b"\x63")
        hit = aux.sony_aux_decode(body[:idx + 11])
        self.assertIsNotNone(hit)
        self.assertEqual(hit.date, (2007, 10, 18))
        self.assertIsNone(hit.time)
        self.assertTrue(hit.truncated_seen)


class TestTimecal(unittest.TestCase):
    def test_month_boundary_ordering(self):
        jul31 = {"date": [2010, 7, 31], "time": [10, 0, 0]}
        aug01 = {"date": [2010, 8, 1], "time": [10, 0, 0]}
        self.assertLess(timecal.aux_epoch(jul31), timecal.aux_epoch(aug01))
        self.assertEqual(timecal.aux_elapsed(jul31, aug01), 86400)

    def test_year_boundary(self):
        dec31 = {"date": [2009, 12, 31], "time": [23, 59, 59]}
        jan01 = {"date": [2010, 1, 1], "time": [0, 0, 0]}
        self.assertEqual(timecal.aux_elapsed(dec31, jan01), 1)

    def test_same_date(self):
        a = {"date": [2007, 10, 18], "time": [9, 0, 0]}
        b = {"date": [2007, 10, 18], "time": [9, 5, 0]}
        c = {"date": [2007, 10, 19], "time": [9, 0, 0]}
        self.assertTrue(timecal.same_date(a, b))
        self.assertFalse(timecal.same_date(a, c))


class TestModelRoundtrip(unittest.TestCase):
    def test_report_roundtrip(self):
        sp = model.Span(
            span_id="0:000", source_id=0, byte_start=0, byte_end=188,
            packet_count=1,
            pmt=model.Pmt(pmt_pid=129, pcr_pid=308, streams=[[2, 2064]],
                          stream_type_set=[2], aux_pid=2065, aux_type=161,
                          video_pid=2064, version=0, signature="sig"),
            pcr=model.Pcr(pid=308, first=1, last=2, sample_count=2,
                          duration_sec=0.0),
            pcr_pid_last_cc=0,
            aux=model.Aux(pid=2065,
                          first=model.AuxSample([2007, 10, 18], [9, 14, 3], 0)),
            first_pusi_offset_by_pid={"2064": 0},
            confidence="high", reasons=["pat+pmt"], terminated_by="eof")
        sr = model.SourceReport(id=0, path="/x.mpeg", size=188, hash=None,
                                framing=model.Framing(188, 0, 0, "high", 1),
                                needs_attention=False, spans=[sp], gaps=[],
                                summary={"coverage_pct": 100.0})
        rep = model.Report(model.REPORT_VERSION, {"probe_mb": 4}, [sr])
        rep2 = model.Report.from_dict(rep.to_dict())
        self.assertEqual(rep2.to_dict(), rep.to_dict())
        self.assertEqual(rep2.span_index()["0:000"].pmt.class_key, ((2,), 161))

    def test_plan_roundtrip(self):
        out = model.Output(name="a.m2t", enabled=True, members=[
            model.Member(span="0:000", join=None),
            model.Member(span="0:001",
                         join=model.Join("discontinuity-marker", "cross", 0.9, "r")),
        ])
        plan = model.Plan(model.PLAN_VERSION, [{"id": 0, "path": "/x", "size": 1}],
                          [out], splits=[model.Split("0:001", "1:000", "code")],
                          unplaced=[model.Unplaced("2:000", "low", "r")])
        plan2 = model.Plan.from_dict(plan.to_dict())
        self.assertEqual(plan2.to_dict(), plan.to_dict())


if __name__ == "__main__":
    unittest.main()
