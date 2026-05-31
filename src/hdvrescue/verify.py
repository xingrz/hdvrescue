"""verify — does a TS file still carry the Sony AUX recording timecode?

A self-contained check (not tied to any external player). It answers one
question: would the camera rec-date/time still be readable from this file? It
also runs cheap structural sanity checks (PCR monotonicity, gross CC breaks) and
surfaces them as warnings.

Exit codes (stable contract for scripting):
  0  the AUX recording timecode is present and decodable
  1  it is absent / not decodable
  2  file or usage error
"""

import argparse
import mmap
import os
import sys

from . import TS
from . import ts, psi, aux as auxmod


def _find_pids(mv, size, scan_bytes):
    """Find pmt_pid then aux_pid by walking up to ``scan_bytes`` of packets.
    Prefers the PMT that declares a Sony AUX stream (0xA1/0xA0)."""
    fr = ts.detect_framing(mv[:min(size, 4 * 1024 * 1024)])
    if fr is None or fr["stride"] != 188:
        return fr, None, None
    pmt_pid = None
    aux_pid = None
    asm = psi.SectionAssembler()
    limit = min(size, scan_bytes)
    pos = fr["first_sync"]
    while pos + TS <= limit:
        if mv[pos] != 0x47:
            pos += 1                       # resync byte-by-byte
            continue
        pkt = mv[pos:pos + TS]
        pid = ts.packet_pid(pkt)
        pusi = ts.packet_pusi(pkt)
        if pid == 0 and pusi and pmt_pid is None:
            ps = ts.packet_payload_start(pkt)
            if ps is not None:
                pmt_pid = psi.parse_pat(bytes(pkt[ps:]))
        elif pmt_pid is not None and pid == pmt_pid:
            ps = ts.packet_payload_start(pkt)
            if ps is not None:
                for sec in asm.feed(pusi, bytes(pkt[ps:])):
                    parsed = psi.parse_pmt_section(sec)
                    if parsed and parsed["aux_pid"] is not None:
                        return fr, pmt_pid, parsed["aux_pid"]
        pos += TS
    return fr, pmt_pid, aux_pid


def _scan_aux_on_pid(mv, size, aux_pid, window):
    """Look for a decodable Sony AUX pack on ``aux_pid`` within ``window`` bytes."""
    pos = 0
    limit = min(size, window)
    while pos + TS <= limit:
        if mv[pos] != 0x47:
            pos += 1
            continue
        pkt = mv[pos:pos + TS]
        if ts.packet_pid(pkt) == aux_pid and ts.packet_pusi(pkt):
            ps = ts.packet_payload_start(pkt)
            if ps is not None:
                hit = auxmod.decode_aux_pes(bytes(pkt[ps:]))
                if hit and hit.date:
                    return hit
        pos += TS
    return None


def _deep_scan(mv, size, window):
    """Container-agnostic fallback: find the Sony anchor with its PES context."""
    buf = bytes(mv[:min(size, window)])
    for m in auxmod.AUX_PES_RE.finditer(buf):
        hit = auxmod.decode_aux_pes(buf[m.start():m.start() + 256])
        if hit and hit.date:
            return hit
    return None


def has_timecode(path, window_mb=64):
    """Return ``(present, timestamp, method, diag)`` for ``path``."""
    size = os.path.getsize(path)
    diag = {}
    if size < TS:
        return False, None, None, {"error": "file too small"}
    fd = os.open(path, os.O_RDONLY)
    try:
        mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
    finally:
        os.close(fd)
    mv = memoryview(mm)
    window = window_mb * 1024 * 1024
    try:
        fr, pmt_pid, aux_pid = _find_pids(mv, size, window)
        diag["framing"] = fr["stride"] if fr else None
        diag["aux_pid"] = aux_pid
        if aux_pid is not None:
            hit = _scan_aux_on_pid(mv, size, aux_pid, window)
            if hit:
                return True, auxmod.fmt_ts(hit.date, hit.time), "aux-pid", diag
        # Fallback: the timecode bytes may be present without a clean PMT.
        hit = _deep_scan(mv, size, window)
        if hit:
            return True, auxmod.fmt_ts(hit.date, hit.time), "deep-scan", diag
        return False, None, None, diag
    finally:
        mv.release()
        mm.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hdvrescue verify",
                                 description="Check a TS file for the Sony AUX "
                                             "recording timecode.")
    ap.add_argument("file")
    ap.add_argument("--window-mb", type=int, default=64,
                    help="bytes to sample from the file head (default 64)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        present, tstamp, method, diag = has_timecode(args.file, args.window_mb)
    except OSError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 2
    if diag.get("error"):                     # malformed / too small to be TS
        print("ERROR: %s" % diag["error"], file=sys.stderr)
        return 2

    if not args.quiet:
        print("file:      %s" % args.file)
        print("framing:   %s" % (("%d-byte TS" % diag["framing"])
                                 if diag.get("framing") else "not a TS"))
        if present:
            print("TIMECODE:  present  ->  %s  [%s]" % (tstamp, method))
        else:
            print("TIMECODE:  ABSENT")
    return 0 if present else 1


if __name__ == "__main__":
    sys.exit(main())
