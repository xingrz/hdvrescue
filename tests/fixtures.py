"""Synthetic 188-byte MPEG-TS builders and corruptors for tests.

Everything here produces tiny (a few KB) byte-exact transport streams with known
ground truth, so the recovery pipeline can be tested deterministically without
multi-GB sample captures. The packet layout of :func:`recording` is fixed and
documented so tests can address individual packets by index.
"""

import struct

SYNC = 0x47
TS = 188
PCR_HZ = 90000

# Typical Sony HDV PIDs/stream_types (match docs/hdv-internals.md).
PMT_PID = 0x081       # 129
PCR_PID = 0x134       # 308  (dedicated PCR-only PID, not in the ES list)
VIDEO_PID = 0x810     # 2064
AUDIO_PID = 0x814     # 2068
AUX_A0_PID = 0x815    # 2069
AUX_A1_PID = 0x811    # 2065

DEFAULT_STREAMS = [
    (0x02, VIDEO_PID),
    (0x03, AUDIO_PID),
    (0xA0, AUX_A0_PID),
    (0xA1, AUX_A1_PID),
]


def to_bcd(n):
    return ((n // 10) << 4) | (n % 10)


def add_seconds(time, secs):
    """Add ``secs`` to an ``(h, m, s)`` tuple, wrapping at 24h."""
    h, m, s = time
    total = ((h * 3600 + m * 60 + s) + secs) % 86400
    return (total // 3600, (total % 3600) // 60, total % 60)


# ---------------------------------------------------------------------------
# Low-level packet builders
# ---------------------------------------------------------------------------

def _header(pid, afc, cc, pusi=False):
    b = bytearray(4)
    b[0] = SYNC
    b[1] = (0x40 if pusi else 0) | ((pid >> 8) & 0x1F)
    b[2] = pid & 0xFF
    b[3] = ((afc & 0x3) << 4) | (cc & 0x0F)
    return b


def payload_packet(pid, payload=b"", pusi=False, cc=0):
    """AFC=1 (payload only). Payload is padded to 184 bytes with 0xFF."""
    assert len(payload) <= 184, "payload too long for one packet"
    b = _header(pid, 1, cc, pusi)
    b += payload + b"\xff" * (184 - len(payload))
    return bytes(b)


def _encode_pcr(base, ext=0):
    return bytes([
        (base >> 25) & 0xFF,
        (base >> 17) & 0xFF,
        (base >> 9) & 0xFF,
        (base >> 1) & 0xFF,
        ((base & 1) << 7) | 0x7E | ((ext >> 8) & 0x01),
        ext & 0xFF,
    ])


def pcr_only_packet(pid, pcr, cc=0, discontinuity=False):
    """AFC=2 (adaptation only) carrying a PCR. A dedicated PCR-only PID emits
    these; per spec their continuity_counter does NOT increment, so ``cc`` is
    held constant by callers."""
    b = _header(pid, 2, cc)
    b.append(183)                                  # adaptation_field_length
    flags = 0x10 | (0x80 if discontinuity else 0)  # PCR_flag (+ discontinuity)
    b.append(flags)
    b += _encode_pcr(pcr)
    b += b"\xff" * (TS - len(b))                    # stuffing
    return bytes(b)


def video_pcr_packet(pid, pcr, payload=b"", pusi=False, cc=0):
    """AFC=3 (adaptation + payload) carrying a PCR, for streams where the video
    PID is also the PCR PID."""
    b = _header(pid, 3, cc, pusi)
    af = bytearray([0x10])                          # flags: PCR_flag
    af += _encode_pcr(pcr)
    b.append(len(af))                               # adaptation_field_length
    b += af
    room = TS - len(b)
    b += payload[:room] + b"\xff" * (room - min(room, len(payload)))
    return bytes(b)


def _psi_section(table_id, body, version=0):
    """Wrap ``body`` (everything after the 3-byte section header, incl. CRC) in a
    section header. A dummy CRC is appended by the caller inside ``body``."""
    section_length = len(body)
    return bytes([
        table_id,
        0x80 | 0x30 | ((section_length >> 8) & 0x0F),
        section_length & 0xFF,
    ]) + body


def pat_packet(pmt_pid, program=100, cc=0):
    body = bytearray()
    body += bytes([0x00, 0x01])                     # transport_stream_id
    body.append(0xC0 | 0x01)                        # reserved|version 0|current_next 1
    body += bytes([0x00, 0x00])                     # section/last_section number
    body += bytes([(program >> 8) & 0xFF, program & 0xFF])
    body += bytes([0xE0 | ((pmt_pid >> 8) & 0x1F), pmt_pid & 0xFF])
    body += b"\x00\x00\x00\x00"                     # dummy CRC_32
    section = _psi_section(0x00, bytes(body))
    return payload_packet(0, b"\x00" + section, pusi=True, cc=cc)


def _pmt_body(streams, pcr_pid, program=100, version=0, program_info=b""):
    body = bytearray()
    body += bytes([(program >> 8) & 0xFF, program & 0xFF])
    body.append(0xC0 | ((version & 0x1F) << 1) | 0x01)
    body += bytes([0x00, 0x00])                     # section/last_section number
    body += bytes([0xE0 | ((pcr_pid >> 8) & 0x1F), pcr_pid & 0xFF])
    body += bytes([0xF0 | ((len(program_info) >> 8) & 0x0F), len(program_info) & 0xFF])
    body += program_info
    for st, pid in streams:
        body.append(st)
        body += bytes([0xE0 | ((pid >> 8) & 0x1F), pid & 0xFF])
        body += bytes([0xF0, 0x00])                 # es_info_length 0
    body += b"\x00\x00\x00\x00"                     # dummy CRC_32
    return bytes(body)


def pmt_packet(streams, pcr_pid, pmt_pid=PMT_PID, program=100, version=0, cc=0):
    section = _psi_section(0x02, _pmt_body(streams, pcr_pid, program, version))
    return payload_packet(pmt_pid, b"\x00" + section, pusi=True, cc=cc)


def pmt_packets_multi(streams, pcr_pid, pmt_pid=PMT_PID, program=100, version=0, cc=0):
    """A PMT whose section spans two TS packets, with the ES loop (and thus the
    0xA1 AUX declaration) pushed into the continuation packet. Returns a list of
    two packets. Exercises section reassembly."""
    # Inflate program_info so the ES loop starts beyond the first packet's 183
    # section-bytes, forcing the AUX stream into the continuation.
    program_info = b"\xff" * 180
    section = _psi_section(0x02, _pmt_body(streams, pcr_pid, program, version,
                                           program_info))
    first_payload = b"\x00" + section[:183]         # pointer_field + head (full)
    assert len(first_payload) == 184
    p1 = payload_packet(pmt_pid, first_payload, pusi=True, cc=cc)
    p2 = payload_packet(pmt_pid, section[183:], pusi=False, cc=(cc + 1) & 0xF)
    return [p1, p2]


def sony_aux_pes_packet(aux_pid, date, time, cc=0, prefix=b""):
    """A private_stream_2 (0xBF) PES packet carrying the Sony AUX timecode anchor.

    ``date`` = (year, month, day); ``time`` = (hour, minute, second) or None.
    """
    y, mo, d = date
    yb = (y - 2000) if y >= 2000 else (y - 1900)
    anchor = bytes([0x63, 0, 0, 0, 0,
                    0xC0, 0x00, to_bcd(d), to_bcd(mo), to_bcd(yb),
                    0xFF])
    if time:
        h, mi, s = time
        anchor += bytes([to_bcd(s), to_bcd(mi), to_bcd(h)])
    else:
        anchor += bytes([0xFF, 0xFF, 0xFF])
    body = prefix + anchor
    payload = b"\x00\x00\x01\xBF" + struct.pack(">H", len(body)) + body
    return payload_packet(aux_pid, payload, pusi=True, cc=cc)


# ---------------------------------------------------------------------------
# Whole recordings
# ---------------------------------------------------------------------------
#
# Packet layout of a recording with dedicated_pcr=True, with_aux=True:
#
#   index 0           : PAT
#   index 1           : PMT
#   index 2 + 3*f + 0 : PCR-only packet   (frame f)
#   index 2 + 3*f + 1 : video packet      (frame f, PUSI)
#   index 2 + 3*f + 2 : AUX PES packet    (frame f)
#
# so frame f's video packet is at byte (2 + 3*f + 1) * 188.

def recording(date=(2007, 10, 18), start_time=(9, 14, 3), n_frames=12,
              pcr_start=100000, pcr_step=3600, streams=None, pcr_pid=PCR_PID,
              video_pid=VIDEO_PID, aux_pid=AUX_A1_PID, pmt_pid=PMT_PID,
              program=100, version=0, dedicated_pcr=True, with_aux=True,
              cc=None, pcr_cc=0):
    """Build a complete, internally-consistent mini HDV recording as bytes.

    PCR advances ``pcr_step`` ticks per frame; AUX time tracks PCR-elapsed
    seconds so the stream is self-consistent. ``cc`` may be a shared dict to
    continue continuity counters across appended recordings. ``pcr_cc`` is the
    (constant) continuity counter the dedicated PCR-only PID carries.
    """
    if streams is None:
        streams = DEFAULT_STREAMS
    if cc is None:
        cc = {}

    def next_cc(pid):
        cc[pid] = (cc.get(pid, -1) + 1) & 0x0F
        return cc[pid]

    out = bytearray()
    out += pat_packet(pmt_pid, program=program, cc=next_cc(0))
    out += pmt_packet(streams, pcr_pid, pmt_pid=pmt_pid, program=program,
                      version=version, cc=next_cc(pmt_pid))
    # dedicated PCR-only PID: constant CC (AFC=2 packets do not increment it)
    for f in range(n_frames):
        pcr = pcr_start + f * pcr_step
        elapsed = (pcr - pcr_start) // PCR_HZ
        if dedicated_pcr:
            out += pcr_only_packet(pcr_pid, pcr, cc=pcr_cc)
            out += payload_packet(video_pid, b"\x00\x00\x01\xE0\x00\x00",
                                  pusi=True, cc=next_cc(video_pid))
        else:
            out += video_pcr_packet(video_pid, pcr, b"\x00\x00\x01\xE0\x00\x00",
                                    pusi=True, cc=next_cc(video_pid))
        if with_aux:
            t = add_seconds(start_time, elapsed)
            out += sony_aux_pes_packet(aux_pid, date, t, cc=next_cc(aux_pid))
    return bytes(out)


# ---------------------------------------------------------------------------
# Corruptors
# ---------------------------------------------------------------------------

def splice_junk(data, at, junk):
    """Insert raw ``junk`` bytes at byte offset ``at`` (simulates a carved-in
    foreign fragment)."""
    return data[:at] + junk + data[at:]


def drop_bytes(data, at, n):
    """Remove ``n`` bytes at offset ``at`` (simulates lost sectors). Generally
    de-aligns subsequent framing."""
    return data[:at] + data[at + n:]


def drop_packets(data, packet_index, count=1):
    """Remove whole 188-byte packets, keeping alignment. The PID of the dropped
    packet(s) gets a continuity-counter discontinuity at that point."""
    at = packet_index * TS
    return data[:at] + data[at + count * TS:]


def junk_bytes(n, seed=0):
    """Deterministic non-TS filler (no 0x47 at any stride)."""
    return bytes(((seed + i) % 0x47) for i in range(n))


def truncate_to(data, n_bytes):
    """Cut the stream to ``n_bytes`` (may leave a sub-packet tail remainder)."""
    return data[:n_bytes]


def reframe_192(data, corrupt_head=0):
    """Convert an 188-byte stream to 192-byte M2TS by prefixing a 4-byte
    timestamp to each packet. ``corrupt_head`` junk bytes are prepended."""
    out = bytearray(junk_bytes(corrupt_head))
    for i in range(0, len(data) - TS + 1, TS):
        out += b"\x00\x00\x00\x00" + data[i:i + TS]
    return bytes(out)
