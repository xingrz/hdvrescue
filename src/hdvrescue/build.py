"""build — materialize a plan into final .m2t files.

The only stage that reads source bytes to copy. It resolves each output's member
spans back to authoritative byte ranges in the *report* (so hand-edits to the
plan's echoed fields can't move bytes), streams those ranges from the original
sources, applies the per-join boundary treatment, writes a ``.partial`` file,
self-verifies that the AUX timecode survived, and only then atomically renames it
into place.

Boundary treatments:
  * verbatim             — copy bytes unchanged (single-span outputs, first member).
  * discontinuity-marker — inject one adaptation-only marker on the entered span's
    PCR PID, then copy bytes unchanged. Continuity counters are NOT forged, so a
    genuine packet loss is never concealed.
  * cc-fix               — rewrite continuity counters to continue the previous
    member, no marker. Honored for hand-edited plans (proven-continuous joins).
"""

import os

from . import TS
from . import ts
from . import verify


class BuildError(Exception):
    pass


def _assert_aligned(mv, start, end, path):
    """A span must be a whole number of packets and have a 0x47 sync at every
    packet boundary. We assert and abort — never slide to resync, which would
    desync from the report's offsets and rewrite CC onto garbage. The strided
    check runs at C speed, so it is cheap even on multi-GB ranges."""
    if (end - start) % TS != 0:
        raise BuildError("span [%d,%d) in %s is not a whole number of packets"
                         % (start, end, path))
    syncs = bytes(mv[start:end:TS])
    if syncs.count(0x47) != len(syncs):
        i = next(k for k, b in enumerate(syncs) if b != 0x47)
        raise BuildError("in-span non-sync byte at offset %d in %s (scanner bug "
                         "or source changed since scan); aborting"
                         % (start + i * TS, path))


def _marker_for(span):
    """(pcr_pid, cc) for a discontinuity marker entering ``span``, or None."""
    pcr_pid = None
    if span.pcr and span.pcr.pid is not None:
        pcr_pid = span.pcr.pid
    elif span.pmt and span.pmt.pcr_pid is not None:
        pcr_pid = span.pmt.pcr_pid
    if pcr_pid is None:
        return None
    cc = span.pcr_pid_last_cc if span.pcr_pid_last_cc is not None else 0
    return pcr_pid, cc


def _write_verbatim(out, mv, start, end):
    """Bulk byte-copy [start,end). Byte-exact, fast."""
    pos = start
    chunk = 8192 * TS
    while pos < end:
        n = min(chunk, end - pos)
        out.write(mv[pos:pos + n])
        pos += n


def _write_cc_fixed(out, mv, start, end, last_cc):
    """Per-packet copy, rewriting continuity counters to continue ``last_cc``."""
    offset = {}
    pos = start
    while pos < end:
        pkt = mv[pos:pos + TS]
        if pkt[0] != 0x47:
            raise BuildError("in-span non-sync byte at %d (scanner bug)" % pos)
        pid = ts.packet_pid(pkt)
        if ts.packet_has_payload(pkt):
            cc = ts.packet_cc(pkt)
            if pid not in offset:
                target = (last_cc.get(pid, 15) + 1) & 0x0F
                offset[pid] = (target - cc) & 0x0F
            new_cc = (cc + offset[pid]) & 0x0F
            if new_cc != cc:
                pkt = ts.with_cc(pkt, new_cc)
            last_cc[pid] = new_cc
        out.write(pkt)
        pos += TS


def _track_cc(mv, start, end, last_cc):
    """Update ``last_cc`` for a verbatim-copied range (so a later cc-fix member
    continues correctly). Only needed when the output contains a cc-fix join."""
    pos = start
    while pos < end:
        pkt = mv[pos:pos + TS]
        if ts.packet_has_payload(pkt):
            last_cc[ts.packet_pid(pkt)] = ts.packet_cc(pkt)
        pos += TS


def build_output(output, report, mmaps, outdir, on_exist="error"):
    """Materialize one :class:`model.Output`. Returns a result dict."""
    span_index = report.span_index()
    members = output.members
    confs = [span_index[m.span].confidence for m in members if m.span in span_index]
    if "low" in confs:
        raise BuildError("output %s references a low-confidence span; refusing"
                         % output.name)

    final_path = os.path.join(outdir, output.name)
    if os.path.exists(final_path):
        if on_exist == "skip":
            return {"name": output.name, "skipped": True}
        if on_exist == "error":
            raise BuildError("output exists: %s (use --on-exist skip|suffix)"
                             % final_path)
        # suffix
        stem, ext = os.path.splitext(output.name)
        i = 1
        while os.path.exists(os.path.join(outdir, "%s_%d%s" % (stem, i, ext))):
            i += 1
        final_path = os.path.join(outdir, "%s_%d%s" % (stem, i, ext))

    needs_cc = any(m.join and m.join.treatment == "cc-fix" for m in members)
    partial = final_path + ".partial"
    markers = 0
    last_cc = {}
    with open(partial, "wb") as out:
        for m in members:
            span = span_index.get(m.span)
            if span is None:
                raise BuildError("plan references unknown span %s" % m.span)
            mv = mmaps[span.source_id]
            _assert_aligned(mv, span.byte_start, span.byte_end,
                            report.source_path(span.source_id))

            treatment = m.join.treatment if m.join else "verbatim"
            if treatment == "discontinuity-marker":
                mk = _marker_for(span)
                if mk is not None:
                    out.write(ts.make_disc_marker(*mk))
                    markers += 1

            if treatment == "cc-fix":
                _write_cc_fixed(out, mv, span.byte_start, span.byte_end, last_cc)
            else:
                _write_verbatim(out, mv, span.byte_start, span.byte_end)
                if needs_cc:
                    _track_cc(mv, span.byte_start, span.byte_end, last_cc)

    # Self-verify before publishing: the AUX timecode must have survived.
    present, tstamp, method, _ = verify.has_timecode(partial)
    expects_aux = any(span_index[m.span].aux.first is not None
                      for m in members if m.span in span_index)
    if expects_aux and not present:
        os.replace(partial, final_path + ".FAILED")
        raise BuildError("self-verify failed for %s: AUX timecode missing in "
                         "output (kept %s.FAILED)" % (output.name, final_path))

    size = os.path.getsize(partial)
    os.replace(partial, final_path)
    return {"name": os.path.basename(final_path), "bytes": size,
            "members": len(members), "markers": markers,
            "timecode": tstamp, "verified": present}


def build(plan, report, outdir, on_exist="error"):
    """Materialize every enabled output of ``plan`` into ``outdir``."""
    os.makedirs(outdir, exist_ok=True)
    # mmap each source once (read-only).
    mmaps = {}
    fds = {}
    import mmap as _mmap
    try:
        for s in report.sources:
            fd = os.open(s.path, os.O_RDONLY)
            fds[s.id] = fd
            mmaps[s.id] = memoryview(_mmap.mmap(fd, 0, access=_mmap.ACCESS_READ))
        results = []
        for output in plan.outputs:
            if not output.enabled:
                continue
            results.append(build_output(output, report, mmaps, outdir, on_exist))
        return results
    finally:
        for mv in mmaps.values():
            obj = mv.obj
            mv.release()
            obj.close()
        for fd in fds.values():
            os.close(fd)
