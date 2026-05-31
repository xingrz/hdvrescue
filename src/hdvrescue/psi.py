"""PAT/PMT (PSI) parsing, including multi-packet section reassembly.

A real PMT can span continuation packets; when it does, parsing only the first
packet can miss the very 0xA1 AUX stream we exist to preserve.
:class:`SectionAssembler` reassembles a section across packets before parsing.

A PSI section is ``table_id(1) + section_length(2) + section_length bytes`` (the
trailing 4 of which are CRC_32). ``section_length`` is the low 12 bits of bytes
1..2. A PUSI packet's payload starts with a ``pointer_field``; the section begins
``1 + pointer_field`` bytes in.
"""

PAT_TABLE_ID = 0x00
PMT_TABLE_ID = 0x02


class SectionAssembler:
    """Streaming reassembler for one PID's PSI sections.

    Feed it ``(pusi, payload)`` per packet of the PID, in order; it yields each
    completed section's raw bytes. A PUSI packet restarts collection at the
    pointer_field (we only care about the section it points to). Truncation at
    end-of-run is surfaced via :meth:`flush`.
    """

    def __init__(self):
        self._buf = bytearray()
        self._need = None
        self._collecting = False

    def feed(self, pusi, payload):
        out = []
        if not payload:
            return out
        if pusi:
            pointer = payload[0]
            start = 1 + pointer
            if start > len(payload):
                # Pointer runs past this packet's payload; ignore, stay idle.
                self._collecting = False
                return out
            self._buf = bytearray(payload[start:])
            self._need = None
            self._collecting = True
        else:
            if not self._collecting:
                return out
            self._buf += payload

        if self._collecting and self._need is None and len(self._buf) >= 3:
            section_length = ((self._buf[1] & 0x0F) << 8) | self._buf[2]
            self._need = 3 + section_length
        if self._need is not None and len(self._buf) >= self._need:
            out.append(bytes(self._buf[:self._need]))
            self._buf = bytearray()
            self._need = None
            self._collecting = False
        return out

    def pending_truncated(self):
        """True if a section was started but never completed (run ended mid-PMT)."""
        return self._collecting and self._need is not None and len(self._buf) < self._need


def parse_pat(payload):
    """First PMT PID from a single-packet PAT payload (pointer_field first).

    Returns the PMT PID of the first program with ``program_number != 0``, or
    ``None``. PATs are tiny and effectively never span packets, so a single
    packet is sufficient here.
    """
    if len(payload) < 9:
        return None
    pointer = payload[0]
    t = 1 + pointer
    if t + 8 > len(payload) or payload[t] != PAT_TABLE_ID:
        return None
    section_length = ((payload[t + 1] & 0x0F) << 8) | payload[t + 2]
    p = t + 8
    end = min(t + 3 + section_length - 4, len(payload))
    while p + 4 <= end:
        prog = (payload[p] << 8) | payload[p + 1]
        pid = ((payload[p + 2] & 0x1F) << 8) | payload[p + 3]
        if prog != 0:
            return pid
        p += 4
    return None


def parse_pmt_section(section):
    """Parse a reassembled PMT section. Returns a dict or ``None`` if not a PMT.

    Keys: ``program_number, version, pcr_pid, streams [(stream_type, pid)],
    stream_type_set, aux_pid, aux_type, video_pid, truncated``. ``truncated`` is
    True when the section bytes are shorter than ``section_length`` declares (the
    stream list parsed is then partial but still returned).
    """
    if len(section) < 12 or section[0] != PMT_TABLE_ID:
        return None
    section_length = ((section[1] & 0x0F) << 8) | section[2]
    program_number = (section[3] << 8) | section[4]
    version = (section[5] >> 1) & 0x1F
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]

    declared_end = 3 + section_length - 4  # exclude CRC_32
    end = min(declared_end, len(section))
    truncated = len(section) < declared_end

    p = 12 + program_info_length
    streams = []
    while p + 5 <= end:
        st = section[p]
        pid = ((section[p + 1] & 0x1F) << 8) | section[p + 2]
        es_info_len = ((section[p + 3] & 0x0F) << 8) | section[p + 4]
        streams.append((st, pid))
        p += 5 + es_info_len

    aux_a1 = next((pid for st, pid in streams if st == 0xA1), None)
    aux_a0 = next((pid for st, pid in streams if st == 0xA0), None)
    video = next((pid for st, pid in streams if st in (0x01, 0x02)), None)
    aux_pid = aux_a1 if aux_a1 is not None else aux_a0
    aux_type = 0xA1 if aux_a1 is not None else (0xA0 if aux_a0 is not None else None)

    return {
        "program_number": program_number,
        "version": version,
        "pcr_pid": pcr_pid,
        "streams": streams,
        "stream_type_set": sorted({st for st, _ in streams}),
        "aux_pid": aux_pid,
        "aux_type": aux_type,
        "video_pid": video,
        "truncated": truncated,
    }


def pmt_class_key(pmt):
    """A stable grouping key insensitive to incidental PID renumbering.

    Two PMTs are the *same class* (and therefore mergeable) iff they declare the
    same set of stream_types and the same AUX presence. We deliberately do NOT
    key on individual PIDs or PMT ``version`` — those vary across a re-recorded
    tape's sessions without meaning a different program structure.
    """
    if pmt is None:
        return None
    return (tuple(pmt["stream_type_set"]), pmt["aux_type"])


def pmt_signature(pmt):
    """Human-readable PMT signature for the report (display only)."""
    if pmt is None:
        return None
    st = ",".join("0x%x" % s for s in pmt["stream_type_set"])
    aux = ("0x%x@0x%x" % (pmt["aux_type"], pmt["aux_pid"])
           if pmt["aux_pid"] is not None else "none")
    pcr = "0x%x" % pmt["pcr_pid"] if pmt["pcr_pid"] is not None else "?"
    return "st={%s};aux=%s;pcr=%s" % (st, aux, pcr)
