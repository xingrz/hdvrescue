"""scan — read source bytes, write a byte-precise report.json.

The scanner is read-only. For each source it cuts the file into *spans* (maximal
byte ranges that are internally trustworthy and seekable) and *gaps* (everything
else). The two together tile ``[0, size)`` exactly — no byte is ever silently
dropped, which is the whole point of the report-driven design.

Design split:
  * the **walk** (:func:`_walk_source`) only finds span/gap boundaries. It carries
    just enough running state to detect the four things that end a span: a PMT
    class change, a PCR discontinuity, a continuity-counter break, and an AUX
    recording boundary. On corruption it finds the next sync run and records a gap.
  * :func:`summarize_range` re-derives every recorded field from a finalized byte
    range. Keeping summarization separate means a retroactive split (PCR
    discontinuity confirmation) is just "set the end offset and summarize" rather
    than an error-prone rewind of accumulated state.
"""

import mmap
import os

from . import TS, PCR_HZ, PCR_MAX
from . import ts, psi, aux as auxmod, timecal
from . import model


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

DEFAULT_PARAMS = {
    "probe_mb": 4,           # bytes scanned to detect framing
    "min_run": 4,            # consecutive syncs to declare in-sync / resync
    "pcr_jump_sec": 5.0,     # PCR step beyond this (or backward) = discontinuity
    "cc_tolerance": "strict",  # "strict" | "lenient" (allow one isolated CC break)
    "aux_boundary_sec": 2.0,   # AUX vs PCR-elapsed slack before a recording boundary
    "hash": "none",          # "none" | "sha1" source-integrity hash
}


# ---------------------------------------------------------------------------
# Low-level helpers over an mmap/bytes buffer
# ---------------------------------------------------------------------------

def _find_resync(buf, start, min_run, stride=TS):
    """Offset >= ``start`` of the next run of ``min_run`` consecutive syncs at
    ``stride``, searching the whole remainder (never drops recoverable structure),
    or ``None``. Uses ``.find`` to skip cheaply over garbage."""
    n = len(buf)
    last = n - stride * min_run
    find = getattr(buf, "find", None)
    pos = start
    while pos <= last:
        if find is not None:
            pos = buf.find(0x47, pos, last + 1)
            if pos < 0:
                return None
        elif buf[pos] != 0x47:
            pos += 1
            continue
        ok = True
        for k in range(1, min_run):
            if buf[pos + k * stride] != 0x47:
                ok = False
                break
        if ok:
            return pos
        pos += 1
    return None


def _aux_hit(mv, off):
    """Decode a Sony AUX PES at packet offset ``off``, or ``None``."""
    pkt = mv[off:off + TS]
    if not ts.packet_pusi(pkt):
        return None
    ps = ts.packet_payload_start(pkt)
    if ps is None:
        return None
    return auxmod.decode_aux_pes(bytes(pkt[ps:]))


# ---------------------------------------------------------------------------
# Span summarization — re-derive recorded fields from a byte range
# ---------------------------------------------------------------------------

def summarize_range(mv, start, end, stride, inherited_pmt=None):
    """Walk packets in ``[start, end)`` and return a dict of recorded fields plus
    a ``corroborators`` set used by the trust gate. Assumes the whole range is in
    sync (the walk guarantees it)."""
    pmt = inherited_pmt
    pmt_pid = inherited_pmt and inherited_pmt.get("pmt_pid")
    reassembled = False
    truncated = bool(inherited_pmt and inherited_pmt.get("truncated"))
    saw_pat = False
    saw_pmt_here = False

    pmt_pid_known = pmt_pid
    asm = psi.SectionAssembler()

    pcr_by_pid = {}        # pid -> dict(first,last,count,monotonic,total,prev,wrapped,last_cc)
    aux_pid_seen = {}      # pid -> dict(first,last,truncated_seen)
    first_pusi = {}

    pos = start
    while pos + TS <= end:
        pkt = mv[pos:pos + TS]
        pid = ts.packet_pid(pkt)
        pusi = ts.packet_pusi(pkt)
        if pusi and str(pid) not in first_pusi:
            first_pusi[str(pid)] = pos

        # PAT -> PMT PID
        if pid == 0 and pusi:
            ps = ts.packet_payload_start(pkt)
            if ps is not None:
                mp = psi.parse_pat(bytes(pkt[ps:]))
                if mp is not None:
                    saw_pat = True
                    if pmt_pid_known is None:
                        pmt_pid_known = mp

        # PMT (reassembled)
        if pmt_pid_known is not None and pid == pmt_pid_known:
            ps = ts.packet_payload_start(pkt)
            if ps is not None:
                for sec in asm.feed(pusi, bytes(pkt[ps:])):
                    parsed = psi.parse_pmt_section(sec)
                    if parsed is not None:
                        saw_pmt_here = True
                        parsed["pmt_pid"] = pmt_pid_known
                        # A reassembled section is longer than one packet payload.
                        reassembled = reassembled or len(sec) > 184
                        truncated = parsed["truncated"]
                        pmt = parsed
        if asm.pending_truncated():
            truncated = True

        # PCR (track per pid)
        pcr = ts.packet_pcr(pkt)
        if pcr is not None:
            s = pcr_by_pid.get(pid)
            if s is None:
                pcr_by_pid[pid] = {"first": pcr, "last": pcr, "count": 1,
                                   "monotonic": True, "total": 0, "prev": pcr,
                                   "wrapped": False, "last_cc": ts.packet_cc(pkt)}
            else:
                d = ts.pcr_diff(s["prev"], pcr)
                if d is None or d < 0:
                    s["monotonic"] = False
                else:
                    s["total"] += d
                    if pcr < s["prev"]:
                        s["wrapped"] = True
                s["last"] = pcr
                s["prev"] = pcr
                s["count"] += 1
                s["last_cc"] = ts.packet_cc(pkt)

        # AUX
        if pid != 0 and pusi:
            ps = ts.packet_payload_start(pkt)
            if ps is not None:
                hit = auxmod.decode_aux_pes(bytes(pkt[ps:]))
                if hit is not None:
                    e = aux_pid_seen.get(pid)
                    sample = {"date": list(hit.date) if hit.date else None,
                              "time": list(hit.time) if hit.time else None,
                              "pes_offset": pos}
                    if e is None:
                        aux_pid_seen[pid] = {"first": sample, "last": sample,
                                             "truncated_seen": hit.truncated_seen}
                    else:
                        e["last"] = sample
                        e["truncated_seen"] = e["truncated_seen"] or hit.truncated_seen

        pos += stride

    packet_count = (end - start) // stride

    # Choose the PCR PID: prefer the PMT-declared one if it has samples here.
    pcr_pid = None
    if pmt and pmt.get("pcr_pid") in pcr_by_pid:
        pcr_pid = pmt["pcr_pid"]
    elif pcr_by_pid:
        pcr_pid = max(pcr_by_pid, key=lambda p: pcr_by_pid[p]["count"])
    pcr_obj = None
    pcr_last_cc = None
    if pcr_pid is not None:
        s = pcr_by_pid[pcr_pid]
        pcr_obj = model.Pcr(pid=pcr_pid, first=s["first"], last=s["last"],
                            sample_count=s["count"],
                            duration_sec=round(s["total"] / PCR_HZ, 3),
                            wrapped=s["wrapped"], monotonic=s["monotonic"])
        pcr_last_cc = s["last_cc"]

    # Choose the AUX PID: prefer the PMT-declared one.
    aux_obj = model.Aux()
    aux_pid = None
    if pmt and pmt.get("aux_pid") in aux_pid_seen:
        aux_pid = pmt["aux_pid"]
    elif aux_pid_seen:
        aux_pid = next(iter(aux_pid_seen))
    if aux_pid is not None:
        e = aux_pid_seen[aux_pid]
        aux_obj = model.Aux(
            pid=aux_pid,
            first=model.AuxSample(**e["first"]),
            last=model.AuxSample(**e["last"]),
            truncated_seen=e["truncated_seen"])

    pmt_obj = None
    if pmt is not None:
        pmt_obj = model.Pmt(
            pmt_pid=pmt.get("pmt_pid"), pcr_pid=pmt.get("pcr_pid"),
            streams=[[st, p] for st, p in pmt["streams"]],
            stream_type_set=list(pmt["stream_type_set"]),
            aux_pid=pmt.get("aux_pid"), aux_type=pmt.get("aux_type"),
            video_pid=pmt.get("video_pid"), version=pmt.get("version"),
            reassembled=reassembled, truncated=truncated,
            signature=psi.pmt_signature(pmt))

    corr = set()
    if pmt_obj is not None:
        corr.add("pmt")
    if pcr_obj is not None and pcr_obj.sample_count >= 2 and pcr_obj.monotonic:
        corr.add("pcr")
    if aux_obj.first is not None:
        corr.add("aux")

    return {
        "packet_count": packet_count,
        "pmt": pmt_obj,
        "pcr": pcr_obj,
        "pcr_pid_last_cc": pcr_last_cc,
        "aux": aux_obj,
        "first_pusi_offset_by_pid": first_pusi,
        "corroborators": corr,
        "saw_pat": saw_pat or (inherited_pmt is not None),
        "saw_pmt_here": saw_pmt_here,
    }


def _assess(summary):
    """Trust gate. Returns ``(confidence, reasons)`` or ``(None, reasons)`` to
    demote the range to an unstructured gap (no positive TS structure)."""
    corr = summary["corroborators"]
    reasons = []
    if "pmt" in corr:
        reasons.append("pmt")
    if "pcr" in corr:
        reasons.append("pcr-monotonic")
    if "aux" in corr:
        reasons.append("aux-anchor")
    if not corr:
        return None, ["no-structure"]
    n = len(corr)
    pmt = summary["pmt"]
    if pmt is not None and pmt.truncated:
        reasons.append("pmt-truncated")
        n = min(n, 2)
    if n >= 3:
        conf = "high"
    elif n == 2:
        conf = "medium"
    else:
        conf = "low"
    return conf, reasons


# ---------------------------------------------------------------------------
# The walk — boundary finder
# ---------------------------------------------------------------------------

class _SpanCtx:
    """Program context that persists across clean span splits and resets after a
    corruption gap."""
    def __init__(self):
        self.pmt = None          # last parsed PMT dict
        self.pmt_pid = None
        self.pcr_pid = None
        self.aux_pid = None
        self.cls = None          # pmt class_key
        self.asm = psi.SectionAssembler()

    def reset(self):
        self.__init__()


_PROGRESS_STEP = 16 * 1024 * 1024   # report scan progress at most this often


def _walk_source(mv, size, framing, params, progress=None):
    """Yield ``("span", start, end, terminated_by, inherited_pmt)`` and
    ``("gap", start, end, kind, reason)`` tuples tiling ``[0, size)``.

    ``progress(pos)`` is called periodically with the current byte offset."""
    stride = framing["stride"]
    first_sync = framing["first_sync"]
    next_report = 0
    min_run = params["min_run"]
    jump_ticks = int(params["pcr_jump_sec"] * PCR_HZ)
    aux_slack = params["aux_boundary_sec"]
    lenient = params["cc_tolerance"] == "lenient"

    if first_sync > 0:
        yield ("gap", 0, first_sync, "leading", "bytes before first TS sync")

    pos = first_sync
    span_start = pos
    ctx = _SpanCtx()

    # per-span detection state
    cc_last = {}
    cc_duped = set()
    cc_broke = False
    pcr_clock = None
    pcr_cand = None
    pcr_cand_off = None
    pcr_clock_start = None
    aux_first = None

    def reset_span_state():
        nonlocal cc_last, cc_duped, cc_broke, pcr_clock, pcr_cand, pcr_cand_off
        nonlocal pcr_clock_start, aux_first
        cc_last = {}
        cc_duped = set()
        cc_broke = False
        pcr_clock = None
        pcr_cand = None
        pcr_cand_off = None
        pcr_clock_start = None
        aux_first = None

    while pos + TS <= size:
        if progress is not None and pos >= next_report:
            progress(pos)
            next_report = pos + _PROGRESS_STEP

        if mv[pos] != 0x47:
            # Sync lost — close span, find next run.
            if pos > span_start:
                yield ("span", span_start, pos, "corruption", ctx.pmt)
            resync = _find_resync(mv, pos, min_run, stride)
            if resync is None:
                yield ("gap", pos, size, "no_resync",
                       "no further TS sync run found")
                return
            yield ("gap", pos, resync, "resync", "sync lost; resync downstream")
            ctx.reset()
            reset_span_state()
            pos = span_start = resync
            continue

        pkt = mv[pos:pos + TS]
        pid = ts.packet_pid(pkt)
        afc = ts.packet_afc(pkt)
        split_at = None
        terminated_by = None

        # --- PMT class change ---
        if ctx.pmt_pid is None and pid == 0 and ts.packet_pusi(pkt):
            ps = ts.packet_payload_start(pkt)
            if ps is not None:
                mp = psi.parse_pat(bytes(pkt[ps:]))
                if mp is not None:
                    ctx.pmt_pid = mp
        if ctx.pmt_pid is not None and pid == ctx.pmt_pid and ts.packet_pusi(pkt):
            ps = ts.packet_payload_start(pkt)
            if ps is not None:
                for sec in ctx.asm.feed(True, bytes(pkt[ps:])):
                    parsed = psi.parse_pmt_section(sec)
                    if parsed is None or parsed["truncated"]:
                        continue
                    parsed["pmt_pid"] = ctx.pmt_pid
                    cls = psi.pmt_class_key(parsed)
                    if ctx.cls is None:
                        ctx.cls = cls
                        ctx.pmt = parsed
                        ctx.pcr_pid = parsed["pcr_pid"]
                        ctx.aux_pid = parsed["aux_pid"]
                    elif cls != ctx.cls:
                        split_at = pos
                        terminated_by = "pmt_change"
                    else:
                        ctx.pmt = parsed
                        ctx.pcr_pid = parsed["pcr_pid"]
                        ctx.aux_pid = parsed["aux_pid"]

        # --- continuity-counter break (payload-bearing packets only) ---
        if split_at is None and afc in (1, 3):
            cc = ts.packet_cc(pkt)
            if pid in cc_last:
                exp = (cc_last[pid] + 1) & 0x0F
                if cc == cc_last[pid]:
                    if pid in cc_duped:           # second dup in a row = break
                        if not (lenient and not cc_broke):
                            split_at = pos
                            terminated_by = "cc_break"
                        else:
                            cc_broke = True
                    else:
                        cc_duped.add(pid)
                elif cc != exp:
                    if ts.disc_indicator(pkt):
                        split_at = pos
                        terminated_by = "disc_indicator"
                    elif lenient and not cc_broke:
                        cc_broke = True               # tolerate one isolated break
                        cc_duped.discard(pid)
                    else:
                        split_at = pos
                        terminated_by = "cc_break"
                else:
                    cc_duped.discard(pid)
            if split_at is None:
                cc_last[pid] = cc

        # --- PCR discontinuity (on the program clock PID, two-sample confirm) ---
        if split_at is None and ctx.pcr_pid is not None and pid == ctx.pcr_pid:
            v = ts.packet_pcr(pkt)
            if v is not None:
                if pcr_clock is None:
                    pcr_clock = v
                    pcr_clock_start = v
                elif pcr_cand is None:
                    d = ts.pcr_diff(pcr_clock, v)
                    if d is not None and 0 <= d <= jump_ticks:
                        pcr_clock = v
                    else:
                        pcr_cand = v
                        pcr_cand_off = pos
                else:
                    d2 = ts.pcr_diff(pcr_cand, v)
                    if d2 is not None and 0 <= d2 <= jump_ticks:
                        split_at = pcr_cand_off       # confirmed; rewind to jump
                        terminated_by = "pcr_discontinuity"
                    else:
                        d3 = ts.pcr_diff(pcr_clock, v)
                        if d3 is not None and 0 <= d3 <= jump_ticks:
                            pcr_clock = v             # candidate was a glitch
                            pcr_cand = None
                        else:
                            split_at = pcr_cand_off
                            terminated_by = "pcr_discontinuity"

        # --- AUX recording boundary ---
        # Compare AUX-elapsed and PCR-elapsed BOTH cumulatively from the span's
        # first sample. A continuous recording keeps the two in lock-step (modulo
        # 1 s AUX rounding); a new recording on the same date resets the camera
        # clock, breaking the relation.
        if split_at is None and ctx.aux_pid is not None and pid == ctx.aux_pid \
                and ts.packet_pusi(pkt):
            hit = _aux_hit(mv, pos)
            if hit is not None and hit.date is not None:
                cur = {"date": list(hit.date),
                       "time": list(hit.time) if hit.time else None}
                if aux_first is None:
                    aux_first = cur
                else:
                    boundary = False
                    if cur["date"] != aux_first["date"]:
                        boundary = True
                    elif cur["time"] and aux_first["time"] \
                            and pcr_clock is not None and pcr_clock_start is not None:
                        aux_dt = timecal.aux_elapsed(aux_first, cur)
                        pcr_dt = ts.pcr_diff(pcr_clock_start, pcr_clock) / PCR_HZ
                        if aux_dt is not None and abs(aux_dt - pcr_dt) > aux_slack + 1:
                            boundary = True
                    if boundary:
                        split_at = pos
                        terminated_by = "aux_recording_boundary"

        if split_at is not None:
            yield ("span", span_start, split_at, terminated_by, ctx.pmt)
            inherited = ctx.pmt
            if terminated_by == "pmt_change":
                ctx.reset()           # the new class is established by the next PMT
                inherited = None
            reset_span_state()
            pos = span_start = split_at
            if terminated_by != "pmt_change" and inherited is not None:
                ctx.pmt = inherited   # keep program context across a clean split
            continue

        pos += stride

    # Final span + sub-stride tail remainder.
    last_packet_end = span_start + ((size - span_start) // stride) * stride
    if last_packet_end > span_start:
        yield ("span", span_start, last_packet_end, "eof", ctx.pmt)
    if last_packet_end < size:
        yield ("gap", last_packet_end, size, "trailing",
               "sub-packet remainder at end of file")


# ---------------------------------------------------------------------------
# Per-source and top-level scan
# ---------------------------------------------------------------------------

def scan_source(path, source_id, params, on_progress=None):
    size = os.path.getsize(path)
    sr = model.SourceReport(id=source_id, path=os.path.abspath(path), size=size,
                            hash=None, framing=None, needs_attention=False,
                            spans=[], gaps=[], summary={})
    if size < TS:
        sr.gaps.append(model.Gap(0, size, size, "unstructured", "file too small"))
        sr.needs_attention = True
        _finalize_summary(sr)
        return sr

    fd = os.open(path, os.O_RDONLY)
    try:
        mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
    finally:
        os.close(fd)
    mv = memoryview(mm)
    try:
        probe = mm[:params["probe_mb"] * 1024 * 1024]
        framing = ts.detect_framing(probe, min_run=params["min_run"])
        if framing is None:
            sr.gaps.append(model.Gap(0, size, size, "unstructured",
                                     "no TS sync pattern (188/192/204)"))
            sr.needs_attention = True
            _finalize_summary(sr)
            return sr

        sr.framing = model.Framing(
            stride=framing["stride"], first_sync=framing["first_sync"],
            slot_offset=framing["slot_offset"], confidence=framing["confidence"],
            longest_run=framing["longest_run"])
        if framing["confidence"] == "low":
            sr.needs_attention = True
            sr.framing.note = "ambiguous framing; trusting longest run"

        if framing["stride"] != 188:
            # v1 builds 188 only. Record the structured region as visible, not lost.
            sr.gaps.append(model.Gap(0, size, size, "non_188",
                                     "stride %d detected; build unsupported in v1"
                                     % framing["stride"]))
            sr.needs_attention = True
            sr.framing.note = "non-188 stride; detect-and-flag only"
            _finalize_summary(sr)
            return sr

        prog = (lambda pos: on_progress(path, pos, size)) if on_progress else None
        span_idx = 0
        for item in _walk_source(mv, size, framing, params, progress=prog):
            if item[0] == "gap":
                _, gs, ge, kind, reason = item
                if ge > gs:
                    sr.gaps.append(model.Gap(gs, ge, ge - gs, kind, reason))
                continue
            _, ss, se, terminated_by, inherited = item
            summary = summarize_range(mv, ss, se, framing["stride"], inherited)
            conf, reasons = _assess(summary)
            if conf is None:
                # No positive structure — demote to an unstructured gap.
                sr.gaps.append(model.Gap(ss, se, se - ss, "unstructured",
                                         "framing only, no PSI/PCR/AUX"))
                continue
            sp = model.Span(
                span_id="%d:%03d" % (source_id, span_idx),
                source_id=source_id, byte_start=ss, byte_end=se,
                packet_count=summary["packet_count"], pmt=summary["pmt"],
                pcr=summary["pcr"], pcr_pid_last_cc=summary["pcr_pid_last_cc"],
                aux=summary["aux"],
                first_pusi_offset_by_pid=summary["first_pusi_offset_by_pid"],
                confidence=conf, reasons=reasons, terminated_by=terminated_by)
            sr.spans.append(sp)
            span_idx += 1
    finally:
        mv.release()
        mm.close()

    if on_progress:
        on_progress(path, size, size)   # finalize the bar at 100%
    _verify_tiling(sr)
    _finalize_summary(sr)
    return sr


def _verify_tiling(sr):
    """Assert spans ∪ gaps tile [0, size) with no overlap or hole."""
    pieces = sorted([(s.byte_start, s.byte_end) for s in sr.spans]
                    + [(g.byte_start, g.byte_end) for g in sr.gaps])
    cur = 0
    for a, b in pieces:
        assert a == cur, "tiling hole/overlap at %d (expected %d) in %s" % (
            a, cur, sr.path)
        assert b > a, "empty piece [%d,%d)" % (a, b)
        cur = b
    assert cur == sr.size, "tiling ends at %d != size %d in %s" % (
        cur, sr.size, sr.path)


def _finalize_summary(sr):
    covered = sum(s.byte_end - s.byte_start for s in sr.spans)
    classes = {tuple(s.pmt.stream_type_set) for s in sr.spans if s.pmt}
    dates = sorted({"%04d-%02d-%02d" % tuple(s.aux.first.date)
                    for s in sr.spans if s.aux.first and s.aux.first.date})
    sr.summary = {
        "coverage_pct": round(100.0 * covered / sr.size, 2) if sr.size else 0.0,
        "span_count": len(sr.spans),
        "gap_count": len(sr.gaps),
        "distinct_pmt_classes": len(classes),
        "distinct_aux_dates": dates,
    }


def scan(paths, params=None, on_progress=None):
    """Scan ``paths`` into a Report. ``on_progress(path, done, total)`` is called
    periodically per source for progress reporting."""
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    sources = [scan_source(path, i, p, on_progress=on_progress)
               for i, path in enumerate(paths)]
    return model.Report(model.REPORT_VERSION, p, sources)
