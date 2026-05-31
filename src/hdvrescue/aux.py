"""Sony HDV AUX recording-timecode decoding.

Sony HDV carries the camera rec-date/rec-time in a private AUX stream
(``stream_type 0xA1``, usually PID ``0x811``). Each AUX packet is a
``private_stream_2`` PES (``00 00 01 BF``); inside the PES body the timecode
anchor is::

    0x63 ?? ?? ?? ??   0xC0 ?? DD MM YY   0xFF   SS  MM  HH
    ^ time-pack header ^ date pack               ^ BCD, reversed byte order

requiring the ``0xBF`` PES context (not the bare anchor) keeps random MPEG-2
video bytes from matching by chance. Dates/times are BCD; the year base flips at
75 (``19xx`` for >=75, else ``20xx``).
"""

import re
from typing import NamedTuple, Optional, Tuple

# private_stream_2 PES start code.
PES_PRIVATE_2 = bytes((0x00, 0x00, 0x01, 0xBF))

# The bare anchor: 0x63, 4 bytes, 0xC0, 4 bytes, 0xFF  (11 bytes).
SONY_AUX_RE = re.compile(rb"\x63.{4}\xc0.{4}\xff", re.DOTALL)

# Anchor *with* its PES context, for container-agnostic byte scanning (verify).
AUX_PES_RE = re.compile(
    rb"\x00\x00\x01\xbf.{0,64}?\x63.{4}\xc0.{4}\xff", re.DOTALL)


class AuxHit(NamedTuple):
    date: Optional[Tuple[int, int, int]]   # (year, month, day)
    time: Optional[Tuple[int, int, int]]   # (hour, minute, second) or None
    truncated_seen: bool                   # anchor matched but bytes ran off the edge


def bcd(b):
    return (b & 0x0F) + ((b >> 4) & 0x0F) * 10


def _decode_date(body, c0):
    """Decode the date pack whose 0xC0 header is at ``c0``. ``body[c0+2..c0+4]``
    hold DD/MM/YY (BCD). Returns ``(year, month, day)`` or ``None`` if implausible."""
    day = bcd(body[c0 + 2] & 0x3F)
    month = bcd(body[c0 + 3] & 0x1F)
    yb = bcd(body[c0 + 4])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    year = 1900 + yb if yb >= 75 else 2000 + yb
    return (year, month, day)


def sony_aux_decode(body):
    """Find the first valid Sony anchor in ``body``. Returns an :class:`AuxHit`
    or ``None`` if no anchor with a plausible date is present.

    A hit whose date is valid but whose time bytes are cut off at the end of
    ``body`` (a windowing artefact, not missing data) is returned with
    ``time=None, truncated_seen=True`` — distinct from "no timecode here".
    """
    for m in SONY_AUX_RE.finditer(bytes(body)):
        i = m.start()
        c0 = i + 5
        date = _decode_date(body, c0)
        if date is None:
            continue
        if i + 14 > len(body):
            return AuxHit(date, None, True)
        second = bcd(body[i + 11] & 0x7F)
        minute = bcd(body[i + 12] & 0x7F)
        hour = bcd(body[i + 13] & 0x3F)
        if second > 59 or minute > 59 or hour > 23:
            return AuxHit(date, None, False)
        return AuxHit(date, (hour, minute, second), False)
    return None


def decode_aux_pes(payload):
    """Decode a TS payload that begins a Sony AUX PES (``00 00 01 BF``).

    Returns an :class:`AuxHit` or ``None``. The 6-byte PES header (start code +
    2-byte length) is skipped before scanning for the anchor.
    """
    if len(payload) < 8 or payload[0] != 0 or payload[1] != 0 \
            or payload[2] != 1 or payload[3] != 0xBF:
        return None
    return sony_aux_decode(payload[6:])


def fmt_ts(date, time):
    """``(date, time)`` -> 'YYYY-MM-DD HH:MM:SS' (or date-only), or None."""
    if not date:
        return None
    s = "%04d-%02d-%02d" % date
    if time:
        s += " %02d:%02d:%02d" % time
    return s


def fmt_date(date):
    return "%04d-%02d-%02d" % date if date else None
