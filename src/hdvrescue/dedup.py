"""dedup — find and byte-verify duplicate fragments across carved sources.

A disk-recovery tool sometimes carves the same footage twice (the file existed in
more than one place on the failing disk), so two spans can cover the *same*
recording moment. Concatenating them duplicates content; the safe fix is to keep
one copy and drop the redundant one — but only after the bytes confirm it.

Candidates are grouped cheaply from the report (same PMT class + same AUX start
timecode, appearing at two or more distinct source/offset locations). Then this
module reads just those byte ranges and classifies each pair against the longest
("primary") member of its group:

  * identical — same length, byte-for-byte equal            -> drop either copy.
  * contained — the shorter is a byte-prefix of the longer  -> drop the shorter.
  * diverges  — same start but the bytes differ from some    -> NOT the same capture
                offset on                                       past there; keep both.

Only the candidate spans are read (a small fraction of the sources), so this is
cheap next to a full re-scan. It reports; it never deletes.
"""

import hashlib
import mmap
import os


def _aux_key(span):
    a = span.aux.first
    if a is None or a.date is None:
        return None
    return (tuple(a.date), tuple(a.time) if a.time else None)


def _fmt_tc(key):
    (date, time) = key
    s = "%04d-%02d-%02d" % tuple(date)
    if time:
        s += " %02d:%02d:%02d" % tuple(time)
    return s


def candidate_groups(report):
    """Spans grouped by (PMT class, AUX start timecode) that occur at >=2 distinct
    (source, offset) locations. Each returned group is a duplicate candidate set,
    sorted longest-first."""
    buckets = {}
    for sp in report.all_spans():
        if sp.pmt is None:
            continue
        k = _aux_key(sp)
        if k is None:
            continue
        buckets.setdefault((sp.pmt.class_key, k), []).append(sp)
    groups = []
    for (cls, tc), spans in buckets.items():
        locs = {(s.source_id, s.byte_start) for s in spans}
        if len(spans) >= 2 and len(locs) >= 2:
            spans = sorted(spans, key=lambda s: (s.length, s.span_id), reverse=True)
            groups.append((tc, spans))
    groups.sort(key=lambda g: g[1][0].length, reverse=True)
    return groups


def _first_diff(a, b, limit):
    """Offset of the first differing byte in ``a[:limit]`` vs ``b[:limit]``, or
    ``None`` if equal. The whole-slice compare is C-speed; on a mismatch we chunk
    down to localise the offset without copying the whole range."""
    if a[:limit] == b[:limit]:
        return None
    step = 1 << 20
    off = 0
    while off < limit:
        n = min(step, limit - off)
        ca, cb = bytes(a[off:off + n]), bytes(b[off:off + n])
        if ca != cb:
            for i in range(n):
                if ca[i] != cb[i]:
                    return off + i
        off += n
    return None


def _classify_pair(primary, other, mmaps):
    A = mmaps[primary.source_id][primary.byte_start:primary.byte_end]
    B = mmaps[other.source_id][other.byte_start:other.byte_end]
    la, lb = len(A), len(B)
    n = min(la, lb)
    diff = _first_diff(A, B, n)
    if diff is None:
        if la == lb:
            return ("identical", n)
        return ("contained", n)        # shorter is a prefix of the longer
    return ("diverges", diff)


def analyze(report, want_md5=False):
    """Return a list of group findings. Each is a dict with the timecode, the
    primary span, and per-candidate verdicts."""
    groups = candidate_groups(report)
    needed = {s.source_id for _, spans in groups for s in spans}
    mmaps, fds = {}, {}
    findings = []
    try:
        for src in report.sources:
            if src.id in needed:
                fd = os.open(src.path, os.O_RDONLY)
                fds[src.id] = fd
                mmaps[src.id] = memoryview(mmap.mmap(fd, 0, access=mmap.ACCESS_READ))

        def md5(span):
            if not want_md5:
                return None
            h = hashlib.md5()
            h.update(mmaps[span.source_id][span.byte_start:span.byte_end])
            return h.hexdigest()

        for tc, spans in groups:
            primary = spans[0]
            g = {"timecode": _fmt_tc(tc), "primary": _summary(primary, md5),
                 "candidates": []}
            for other in spans[1:]:
                verdict, where = _classify_pair(primary, other, mmaps)
                g["candidates"].append({
                    **_summary(other, md5), "verdict": verdict,
                    "diff_offset": where if verdict == "diverges" else None,
                    "prefix_bytes": where if verdict == "contained" else None,
                    "drop_safe": verdict in ("identical", "contained"),
                })
            findings.append(g)
        return findings
    finally:
        for mv in mmaps.values():
            obj = mv.obj
            mv.release()
            obj.close()
        for fd in fds.values():
            os.close(fd)


def _summary(span, md5fn):
    return {"span": span.span_id, "source_id": span.source_id,
            "bytes": [span.byte_start, span.byte_end], "length": span.length,
            "packets": span.packet_count, "md5": md5fn(span)}


def format_report(findings):
    """Human-readable summary of analyze() findings."""
    lines = []
    drop = []
    for g in findings:
        p = g["primary"]
        lines.append("● %s" % g["timecode"])
        lines.append("    keep %-8s %d pkts, %.1f MB%s"
                     % (p["span"], p["packets"], p["length"] / 1048576.0,
                        ("  md5=%s" % p["md5"]) if p["md5"] else ""))
        for c in g["candidates"]:
            if c["verdict"] == "identical":
                note = "IDENTICAL -> drop"
            elif c["verdict"] == "contained":
                note = "prefix of primary (%d bytes) -> drop" % c["prefix_bytes"]
            else:
                note = "DIVERGES at byte %d -> keep both" % c["diff_offset"]
            lines.append("    %-8s %d pkts, %.1f MB%s   %s"
                         % (c["span"], c["packets"], c["length"] / 1048576.0,
                            ("  md5=%s" % c["md5"]) if c["md5"] else "", note))
            if c["drop_safe"]:
                drop.append(c["span"])
    lines.append("")
    if drop:
        lines.append("%d span(s) safe to drop (verified redundant): %s"
                     % (len(drop), ", ".join(drop)))
    else:
        lines.append("No verified-redundant spans (nothing dropped).")
    return "\n".join(lines)


def main(argv):
    import argparse
    from . import model
    ap = argparse.ArgumentParser(prog="hdvrescue dedup",
                                 description="Byte-verify duplicate fragments in a report.")
    ap.add_argument("report", help="report.json from scan")
    ap.add_argument("--md5", action="store_true",
                    help="also compute each candidate's full MD5 (slower)")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.report):
        print("error: no such report: %s" % args.report)
        return 2
    report = model.load_report(args.report)
    findings = analyze(report, want_md5=args.md5)
    if not findings:
        print("No duplicate candidates (no shared start timecodes across locations).")
        return 0
    print(format_report(findings))
    return 0
