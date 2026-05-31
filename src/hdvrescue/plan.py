"""plan — turn a report into a human-editable plan.json.

Reads the report only (never source bytes), groups spans into output recordings,
and classifies every join. The guiding rule is **default to splitting**: a wrong
merge welds two recordings into one corrupt file, while a wrong split merely
yields two good files. So a join must be positively justified — by byte adjacency
within one source, or by AUX provenance agreeing across a gap / across sources.

Treatments the planner emits:
  * ``verbatim``             — a single-span output (nothing to reconcile).
  * ``discontinuity-marker`` — every real join. One marker is injected at the
    seam; continuity counters are NOT forged, so a genuine packet loss is never
    concealed. This is always safe.
``cc-fix`` (forging continuity counters) is honored by :mod:`build` for
hand-edited plans but is not emitted automatically, because the report cannot
prove zero packet loss across a seam.
"""

from . import PCR_HZ
from . import ts, timecal
from . import model

DEFAULT_PARAMS = {
    "max_chain_sec": 5.0,        # cross-source / no-PCR: max AUX gap still chained
    "max_pcr_jump_sec": 30.0,    # same-source: merge while PCR stays this seekable
    "aux_boundary_sec": 2.0,     # AUX agreement slack (+1s rounding applied)
    "min_confidence": "medium",  # spans below this go to unplaced[]
}

_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _seek_compatible(a, b, max_jump):
    """Would concatenating ``b`` after ``a`` keep the file *seekable*?

    Players estimate a seek position linearly from the PCR-derived duration, then
    snap to a PCR/I-frame. That stays usable as long as the PCR clock keeps moving
    **forward**: a small forward step (content dropped by corruption) only leaves a
    short dead zone, but a PCR **reset** (b restarts an earlier clock) or a large
    leap (a different recording session) makes the duration meaningless and the bar
    un-draggable. PCR clocks differ between capture files, so this is only
    meaningful within one source; callers gate on same-source.

    Returns ``(True/False/None, detail)`` — ``None`` when there is no PCR to judge.
    """
    if not (a.pcr and b.pcr and a.pcr.last is not None and b.pcr.first is not None):
        return (None, "no PCR to compare")
    delta, monotonic = ts.pcr_delta_sec(a.pcr.last, b.pcr.first)
    if delta is None:
        return (None, "no PCR to compare")
    if not monotonic:
        return (False, "PCR resets %.1fs (un-seekable)" % delta)
    if delta > max_jump:
        return (False, "PCR jumps +%.0fs > %.0fs (separate session)" % (delta, max_jump))
    return (True, "PCR +%.2fs" % delta)


def _aux_dict(sample):
    if sample is None or sample.date is None:
        return None
    return {"date": list(sample.date),
            "time": list(sample.time) if sample.time else None}


def _sort_key(span):
    a = _aux_dict(span.aux.first)
    epoch = timecal.aux_epoch(a) if a else None
    return (
        0 if epoch is not None else 1,
        epoch if epoch is not None else 0,
        span.pcr.first if span.pcr and span.pcr.first is not None else 0,
        span.source_id, span.byte_start,
    )


def classify_join(a, b, params):
    """Decide whether span ``b`` may follow span ``a``.

    Returns ``("join", treatment, provenance, confidence, reason)`` or
    ``("split", code, detail)``.
    """
    max_gap = params["max_chain_sec"]
    max_jump = params["max_pcr_jump_sec"]
    slack = params["aux_boundary_sec"] + 1.0   # +1s for AUX 1-second rounding

    if a.pmt is None or b.pmt is None:
        return ("split", "no-pmt-context",
                "a marker needs a PCR PID from the PMT; one side has no PMT")
    if a.pmt.class_key != b.pmt.class_key:
        return ("split", "pmt-class-differs",
                "%s vs %s" % (a.pmt.signature, b.pmt.signature))

    same_source = a.source_id == b.source_id
    byte_adjacent = same_source and b.byte_start == a.byte_end

    a_aux = _aux_dict(a.aux.last) or _aux_dict(a.aux.first)
    b_aux = _aux_dict(b.aux.first)
    # A different recording *day* is always a different recording — never merge.
    if a_aux and b_aux and a_aux["date"] != b_aux["date"]:
        return ("split", "aux-date-mismatch",
                "%s vs %s" % (a_aux["date"], b_aux["date"]))

    if same_source and byte_adjacent:
        # Contiguous bytes in one source. Copying them verbatim reproduces the
        # original byte range exactly, so it is as seekable as the original
        # capture — merge regardless of an internal aux/cc/pcr boundary (a camera
        # pause/resume within a continuous capture). Only a day change (above) or
        # a PMT-class change (above) separates them.
        return ("join", "verbatim", "same-source-contiguous", 0.97,
                "byte-adjacent within one source (%s)" % a.terminated_by)

    if same_source:
        # A gap within one source (content dropped by corruption). Keep same-day
        # footage together as long as the PCR clock stays seekable across the gap;
        # a reset/leap is a separate session -> split.
        ok, detail = _seek_compatible(a, b, max_jump)
        if ok is True:
            return ("join", "discontinuity-marker", "same-source-gap", 0.9,
                    "same-source same-day, %s" % detail)
        if ok is False:
            return ("split", "pcr-discontinuity", detail)
        # No PCR to judge: fall back to the conservative AUX-elapsed window.
        elapsed = (timecal.aux_elapsed(a_aux, b_aux)
                   if a_aux and b_aux and a_aux["time"] and b_aux["time"] else None)
        if elapsed is None:
            return ("split", "aux-unknown", "no PCR and no AUX to prove continuity")
        if elapsed < -slack or elapsed > max_gap + slack:
            return ("split", "aux-elapsed-mismatch",
                    "AUX elapse %.0fs outside [0, %.0fs]" % (elapsed, max_gap))
        return ("join", "discontinuity-marker", "same-source-gap", 0.85,
                "AUX continuous (+%.0fs)" % elapsed)

    # Cross-source: capture files carry independent PCR clocks, so seek-continuity
    # cannot be proven from PCR. Require positive AUX agreement within the
    # (conservative) chain window. Possible duplicate captures are surfaced
    # separately by ``hdvrescue dedup``, not merged blindly here.
    if not (a_aux and b_aux and a_aux["time"] and b_aux["time"]):
        return ("split", "aux-unknown",
                "no AUX timecode on one side; cannot prove continuity")
    elapsed = timecal.aux_elapsed(a_aux, b_aux)
    if elapsed is None or elapsed < -slack or elapsed > max_gap + slack:
        return ("split", "aux-elapsed-mismatch",
                "AUX elapse %.0fs outside [0, %.0fs]" % (elapsed or 0, max_gap))
    return ("join", "discontinuity-marker", "cross-source", 0.8,
            "AUX continuous across cross-source (+%.0fs)" % elapsed)


def _build_chains(spans, params):
    """Greedy time-ordered chaining within one PMT class. Returns (chains,
    splits) where chains is a list of span lists and splits records adjacent
    spans that were not joined."""
    ordered = sorted(spans, key=_sort_key)
    used = set()
    chains = []
    for start in ordered:
        if id(start) in used:
            continue
        chain = [start]
        used.add(id(start))
        while True:
            tail = chain[-1]
            best = None
            best_score = None
            for cand in ordered:
                if id(cand) in used:
                    continue
                kind = classify_join(tail, cand, params)
                if kind[0] != "join":
                    continue
                a_aux = _aux_dict(tail.aux.last) or _aux_dict(tail.aux.first)
                b_aux = _aux_dict(cand.aux.first)
                el = timecal.aux_elapsed(a_aux, b_aux) if (a_aux and b_aux) else 0
                el = abs(el) if el is not None else 0
                # Prefer continuing the same source (byte-adjacent / same-clock
                # runs stay whole) before reaching into another capture.
                score = (0 if cand.source_id == tail.source_id else 1,
                         0 if cand.byte_start == tail.byte_end else 1, el)
                if best is None or score < best_score:
                    best, best_score = cand, score
            if best is None:
                break
            chain.append(best)
            used.add(id(best))
        chains.append(chain)

    # Record splits between adjacent-in-time spans that ended up in different
    # chains (a reviewer wants to see *why* two neighbours weren't merged).
    splits = []
    chain_of = {}
    for ci, ch in enumerate(chains):
        for sp in ch:
            chain_of[sp.span_id] = ci
    for i in range(len(ordered) - 1):
        a, b = ordered[i], ordered[i + 1]
        if chain_of[a.span_id] != chain_of[b.span_id]:
            kind = classify_join(a, b, params)
            if kind[0] == "split":
                splits.append(model.Split(a.span_id, b.span_id, kind[1], kind[2]))
    return chains, splits


def _name(aux_first, span):
    if aux_first and aux_first.date:
        base = "%04d-%02d-%02d" % tuple(aux_first.date)
        if aux_first.time:
            base += "_%02d-%02d-%02d" % tuple(aux_first.time)
        return base
    return "recovered_src%d_%d" % (span.source_id, span.byte_start)


def _member(span, join, report):
    dur = span.pcr.duration_sec if span.pcr else None
    return model.Member(
        span=span.span_id,
        join=join,
        source_file=report.source_path(span.source_id),
        aux=_aux_dict(span.aux.first),
        pcr_range_sec=[0.0, dur] if dur is not None else None,
        bytes=[span.byte_start, span.byte_end])


def make_plan(report, params=None):
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    min_rank = _CONF_RANK[p["min_confidence"]]

    spans = list(report.all_spans())
    placed, unplaced = [], []
    for s in spans:
        if _CONF_RANK.get(s.confidence, 0) < min_rank:
            unplaced.append(model.Unplaced(s.span_id, s.confidence,
                                           "below %s confidence" % p["min_confidence"]))
        else:
            placed.append(s)

    by_class = {}
    for s in placed:
        by_class.setdefault(s.pmt.class_key if s.pmt else None, []).append(s)

    outputs = []
    all_splits = []
    for cls, group in by_class.items():
        chains, splits = _build_chains(group, p)
        all_splits.extend(splits)
        for chain in chains:
            members = []
            for i, sp in enumerate(chain):
                if i == 0:
                    members.append(_member(sp, None, report))
                else:
                    kind = classify_join(chain[i - 1], sp, p)
                    join = model.Join(treatment=kind[1], provenance=kind[2],
                                      confidence=kind[3], reason=kind[4])
                    members.append(_member(sp, join, report))
            outputs.append((chain[0], members))

    # Deterministic order + global collision-resolved names.
    outputs.sort(key=lambda om: _sort_key(om[0]))
    final = []
    used_names = {}
    for first_span, members in outputs:
        base = _name(first_span.aux.first, first_span)
        n = used_names.get(base, 0)
        used_names[base] = n + 1
        name = base if n == 0 else "%s_%s" % (base, chr(ord("a") + n - 1))
        final.append(model.Output(name=name + ".m2t", enabled=True, members=members))

    sources = [{"id": s.id, "path": s.path, "size": s.size} for s in report.sources]
    return model.Plan(model.PLAN_VERSION, sources, final,
                      splits=all_splits, unplaced=unplaced)
