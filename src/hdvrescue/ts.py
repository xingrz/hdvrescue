"""Shared byte-level MPEG-TS primitives.

The single source of truth for TS packet parsing across the whole package.
Everything here operates on a ``pkt`` that is a 188-byte ``bytes``/``memoryview``
slice whose first byte is the 0x47 sync. None of these functions mutate their
input; the only "writer", :func:`with_cc`, returns a fresh ``bytes``.

Reference: docs/hdv-internals.md (TS header, adaptation field, PCR math).
"""

from . import SYNC, TS, PCR_HZ, PCR_MAX

# Strides we know how to *recognise*. v1 only builds 188; 192 (M2TS, 4-byte
# timestamp prefix) and 204 (TS + Reed-Solomon FEC) are detected and flagged.
STRIDES = (188, 192, 204)
# Bytes that precede the sync byte inside one slot of each stride.
SLOT_OFFSET = {188: 0, 192: 4, 204: 0}

# A backward PCR difference is treated as a 2**33 wrap only when the implied
# forward elapse is within this guard (i.e. the prior value was genuinely near
# PCR_MAX and the new one near 0). Anything larger is a real discontinuity, not a
# wrap — this is what stops a multi-hour backward corruption from being silently
# "corrected" into a fake forward wrap.
PCR_WRAP_GUARD = 60 * PCR_HZ   # 60 seconds of ticks


# ---------------------------------------------------------------------------
# Sync / framing
# ---------------------------------------------------------------------------

def longest_sync_run(buf, stride):
    """Longest run of consecutive 0x47 syncs at ``stride`` within ``buf``.

    Returns ``(run_length, start_offset)``. O(len(buf)) per call: each byte
    position is visited once across all alignment classes.
    """
    n = len(buf)
    best_run = 0
    best_pos = -1
    for off in range(stride):
        run = 0
        run_start = off
        i = off
        while i < n:
            if buf[i] == SYNC:
                if run == 0:
                    run_start = i
                run += 1
                if run > best_run:
                    best_run = run
                    best_pos = run_start
            else:
                run = 0
            i += stride
    return best_run, best_pos


def detect_framing(buf, min_run=4):
    """Detect TS framing over ``buf`` by longest strided-sync run.

    Probes 188/192/204 and picks the stride with the longest run, not the first
    sync hit — a corrupt head can put a coincidental 0x47 before the real stream,
    and the longest-run rule is robust to that. Returns a dict::

        {stride, first_sync, slot_offset, longest_run, confidence, runs}

    or ``None`` if no stride shows even ``min_run`` consecutive syncs.
    ``confidence`` is "high"/"medium"/"low"; "low" means the call site should
    flag ``needs_attention`` rather than trust the result.
    """
    runs = {}
    pos = {}
    for stride in STRIDES:
        r, p = longest_sync_run(buf, stride)
        runs[stride] = r
        pos[stride] = p

    best_stride = max(STRIDES, key=lambda s: runs[s])
    best = runs[best_stride]
    if best < min_run:
        return None
    others = max((runs[s] for s in STRIDES if s != best_stride), default=0)

    if best >= max(min_run * 4, 16) and best >= others * 2:
        confidence = "high"
    elif best >= others * 1.5:
        confidence = "medium"
    else:
        confidence = "low"

    first_sync = find_first_sync(buf, min_run=min_run, stride=best_stride)
    if first_sync is None:
        first_sync = pos[best_stride]
    return {
        "stride": best_stride,
        "first_sync": first_sync,
        "slot_offset": SLOT_OFFSET[best_stride],
        "longest_run": best,
        "confidence": confidence,
        "runs": dict(runs),
    }


def find_first_sync(buf, min_run=4, start=0, stride=TS, limit=None):
    """First offset >= ``start`` where ``min_run`` consecutive syncs appear at
    ``stride``. ``limit`` caps the offset considered (for bounded resync search).
    Returns the offset or ``None``.
    """
    n = len(buf)
    last = n - stride * min_run
    if limit is not None:
        last = min(last, limit)
    i = start
    while i <= last:
        if buf[i] == SYNC:
            ok = True
            for k in range(1, min_run):
                if buf[i + k * stride] != SYNC:
                    ok = False
                    break
            if ok:
                return i
        i += 1
    return None


def iter_packets(buf, start, stride=TS):
    """Yield ``(offset, pkt)`` 188-byte TS slices from ``start`` while sync holds.

    ``offset`` is the position of the sync byte; ``pkt`` is ``buf[offset:offset+188]``
    (the TS packet, even for 192/204 where the slot is larger). Stops at the first
    non-sync byte or when fewer than 188 bytes remain.
    """
    pos = start
    n = len(buf)
    while pos + TS <= n:
        if buf[pos] != SYNC:
            return
        yield pos, buf[pos:pos + TS]
        pos += stride


# ---------------------------------------------------------------------------
# Packet header fields
# ---------------------------------------------------------------------------

def packet_pid(pkt):
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


def packet_pusi(pkt):
    """payload_unit_start_indicator."""
    return bool(pkt[1] & 0x40)


def packet_tei(pkt):
    """transport_error_indicator — the demodulator/tape flagged this packet bad."""
    return bool(pkt[1] & 0x80)


def packet_afc(pkt):
    """adaptation_field_control: 1=payload, 2=adaptation only, 3=both, 0=reserved."""
    return (pkt[3] >> 4) & 0x3


def packet_has_payload(pkt):
    return packet_afc(pkt) in (1, 3)


def packet_cc(pkt):
    return pkt[3] & 0x0F


def packet_payload_start(pkt):
    """Byte offset within ``pkt`` where the payload begins, or ``None`` if the
    packet carries no payload / the adaptation field is malformed."""
    afc = packet_afc(pkt)
    if afc == 1:
        return 4
    if afc == 3:
        af_len = pkt[4]
        ps = 5 + af_len
        if ps >= TS:
            return None
        return ps
    return None


def packet_pcr(pkt):
    """33-bit PCR base (90 kHz ticks) from ``pkt``, or ``None``.

    Guards ``af_len >= 7`` so a short adaptation field can't make us read past
    the PCR bytes.
    """
    afc = packet_afc(pkt)
    if afc < 2:
        return None
    af_len = pkt[4]
    if af_len < 7 or 5 + af_len > TS:
        return None
    if not (pkt[5] & 0x10):  # PCR_flag
        return None
    return ((pkt[6] << 25) | (pkt[7] << 17) | (pkt[8] << 9)
            | (pkt[9] << 1) | (pkt[10] >> 7))


# Alias for call sites that read the PCR as a standalone "extract" operation.
extract_pcr = packet_pcr


def disc_indicator(pkt):
    """discontinuity_indicator bit, guarded.

    Only adaptation-bearing packets (AFC 2/3) with a non-zero adaptation field
    length even have the flags byte; reading ``pkt[5]`` otherwise would be a
    stuffing/payload byte. Returns ``False`` when the flag is absent.
    """
    afc = packet_afc(pkt)
    if afc < 2:
        return False
    if pkt[4] == 0:  # adaptation_field_length 0 => no flags byte
        return False
    return bool(pkt[5] & 0x80)


# ---------------------------------------------------------------------------
# The only "writer": continuity-counter patch
# ---------------------------------------------------------------------------

def with_cc(pkt, new_cc):
    """Return ``pkt`` with its continuity_counter nibble replaced. Touches only
    the low nibble of byte 3; every other byte (incl. the AUX PES body) is
    preserved exactly."""
    return pkt[:3] + bytes([(pkt[3] & 0xF0) | (new_cc & 0x0F)]) + pkt[4:]


def make_disc_marker(pid, cc):
    """A 188-byte adaptation-only TS packet with ``discontinuity_indicator=1``.

    Carries no PCR and no payload: its sole job is to tell the decoder, ahead of
    the next real packet of ``pid``, that the upcoming PCR/CC may not continue —
    reset STC, do not treat the jump as packet loss.
    """
    pkt = bytearray(TS)
    pkt[0] = SYNC
    pkt[1] = (pid >> 8) & 0x1F        # TEI/PUSI/transport_priority = 0
    pkt[2] = pid & 0xFF
    pkt[3] = 0x20 | (cc & 0x0F)       # AFC=10 (adaptation only)
    pkt[4] = 183                      # adaptation_field_length (fills the packet)
    pkt[5] = 0x80                     # discontinuity_indicator only
    for i in range(6, TS):
        pkt[i] = 0xFF                 # stuffing
    return bytes(pkt)


# ---------------------------------------------------------------------------
# PCR arithmetic
# ---------------------------------------------------------------------------

def pcr_diff(a, b):
    """Signed ``b - a`` in 90 kHz ticks, corrected for a single 2**33 wrap.

    A forward wrap (``b`` just past the 2**33 boundary from ``a``) yields a small
    positive result. A genuine large backward jump stays large-negative — it is
    *not* rewritten into a fake wrap; only a difference whose implied forward
    elapse is within :data:`PCR_WRAP_GUARD` is accepted as a wrap. A large
    forward jump (a real discontinuity to a much later value) is likewise left as
    a large positive, never folded to negative.
    """
    if a is None or b is None:
        return None
    d = b - a
    if d <= -(PCR_MAX - PCR_WRAP_GUARD):
        d += PCR_MAX
    return d


def pcr_delta_sec(a_last, b_first):
    """``(delta_seconds, monotonic)`` between two PCR values, wrap-aware."""
    d = pcr_diff(a_last, b_first)
    if d is None:
        return (None, False)
    return (d / PCR_HZ, d >= 0)
