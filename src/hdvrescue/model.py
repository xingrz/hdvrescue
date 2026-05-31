"""Data model for report.json and plan.json + their JSON (de)serialization.

Everything that crosses a stage boundary lives here so the three stages share one
schema and the round-trip is testable. Dataclasses serialize via
:func:`dataclasses.asdict`; ``from_dict`` constructors rebuild them. List-typed
fields (dates, stream pairs) are stored as lists so JSON round-trips are stable.
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
import json

REPORT_VERSION = 1
PLAN_VERSION = 1


# ===========================================================================
# report.json
# ===========================================================================

@dataclass
class Framing:
    stride: int
    first_sync: int
    slot_offset: int
    confidence: str
    longest_run: int
    note: Optional[str] = None

    @staticmethod
    def from_dict(d):
        return Framing(**d)


@dataclass
class Pmt:
    pmt_pid: Optional[int]
    pcr_pid: Optional[int]
    streams: List[List[int]]            # [[stream_type, pid], ...]
    stream_type_set: List[int]
    aux_pid: Optional[int]
    aux_type: Optional[int]
    video_pid: Optional[int]
    version: Optional[int]
    reassembled: bool = False
    truncated: bool = False
    signature: Optional[str] = None

    @property
    def class_key(self):
        return (tuple(self.stream_type_set), self.aux_type)

    @staticmethod
    def from_dict(d):
        return Pmt(**d)


@dataclass
class Pcr:
    pid: Optional[int]
    first: Optional[int]
    last: Optional[int]
    sample_count: int = 0
    duration_sec: Optional[float] = None
    wrapped: bool = False
    monotonic: bool = True

    @staticmethod
    def from_dict(d):
        return Pcr(**d)


@dataclass
class AuxSample:
    date: Optional[List[int]]           # [year, month, day]
    time: Optional[List[int]]           # [hour, minute, second] or None
    pes_offset: Optional[int] = None

    @staticmethod
    def from_dict(d):
        return AuxSample(**d) if d is not None else None


@dataclass
class Aux:
    pid: Optional[int] = None
    first: Optional[AuxSample] = None
    last: Optional[AuxSample] = None
    truncated_seen: bool = False

    @staticmethod
    def from_dict(d):
        if d is None:
            return Aux()
        return Aux(pid=d.get("pid"),
                   first=AuxSample.from_dict(d.get("first")),
                   last=AuxSample.from_dict(d.get("last")),
                   truncated_seen=d.get("truncated_seen", False))


@dataclass
class Span:
    span_id: str
    source_id: int
    byte_start: int
    byte_end: int
    packet_count: int
    pmt: Optional[Pmt]
    pcr: Optional[Pcr]
    pcr_pid_last_cc: Optional[int]
    aux: Aux
    first_pusi_offset_by_pid: Dict[str, int]
    confidence: str
    reasons: List[str]
    terminated_by: str
    terminated_detail: Optional[Dict[str, Any]] = None
    pts: Optional[Any] = None           # null in v1

    @property
    def length(self):
        return self.byte_end - self.byte_start

    @staticmethod
    def from_dict(d):
        return Span(
            span_id=d["span_id"], source_id=d["source_id"],
            byte_start=d["byte_start"], byte_end=d["byte_end"],
            packet_count=d["packet_count"],
            pmt=Pmt.from_dict(d["pmt"]) if d.get("pmt") else None,
            pcr=Pcr.from_dict(d["pcr"]) if d.get("pcr") else None,
            pcr_pid_last_cc=d.get("pcr_pid_last_cc"),
            aux=Aux.from_dict(d.get("aux")),
            first_pusi_offset_by_pid=d.get("first_pusi_offset_by_pid", {}),
            confidence=d.get("confidence", "low"),
            reasons=d.get("reasons", []),
            terminated_by=d.get("terminated_by", "eof"),
            terminated_detail=d.get("terminated_detail"),
            pts=d.get("pts"),
        )


@dataclass
class Gap:
    byte_start: int
    byte_end: int
    length: int
    kind: str
    reason: str = ""

    @staticmethod
    def from_dict(d):
        return Gap(**d)


@dataclass
class SourceReport:
    id: int
    path: str
    size: int
    hash: Optional[str]
    framing: Optional[Framing]
    needs_attention: bool
    spans: List[Span]
    gaps: List[Gap]
    summary: Dict[str, Any]

    @staticmethod
    def from_dict(d):
        return SourceReport(
            id=d["id"], path=d["path"], size=d["size"], hash=d.get("hash"),
            framing=Framing.from_dict(d["framing"]) if d.get("framing") else None,
            needs_attention=d.get("needs_attention", False),
            spans=[Span.from_dict(s) for s in d.get("spans", [])],
            gaps=[Gap.from_dict(g) for g in d.get("gaps", [])],
            summary=d.get("summary", {}),
        )


@dataclass
class Report:
    report_version: int
    params: Dict[str, Any]
    sources: List[SourceReport]

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return Report(
            report_version=d.get("report_version", REPORT_VERSION),
            params=d.get("params", {}),
            sources=[SourceReport.from_dict(s) for s in d.get("sources", [])],
        )

    # --- convenience indexes used by plan/build ---
    def all_spans(self):
        for s in self.sources:
            for sp in s.spans:
                yield sp

    def span_index(self):
        return {sp.span_id: sp for sp in self.all_spans()}

    def source_path(self, source_id):
        for s in self.sources:
            if s.id == source_id:
                return s.path
        return None


# ===========================================================================
# plan.json
# ===========================================================================

@dataclass
class Join:
    treatment: str                      # verbatim | cc-fix | discontinuity-marker
    provenance: str
    confidence: float
    reason: str

    @staticmethod
    def from_dict(d):
        return Join(**d) if d is not None else None


@dataclass
class Member:
    span: str                           # span_id (load-bearing)
    join: Optional[Join]                # None on the first member of an output
    # echoed-for-review fields (build ignores these; they ease hand-editing)
    source_file: Optional[str] = None
    aux: Optional[Dict[str, Any]] = None
    pcr_range_sec: Optional[List[float]] = None
    bytes: Optional[List[int]] = None

    @staticmethod
    def from_dict(d):
        return Member(
            span=d["span"],
            join=Join.from_dict(d.get("join")),
            source_file=d.get("source_file"),
            aux=d.get("aux"),
            pcr_range_sec=d.get("pcr_range_sec"),
            bytes=d.get("bytes"),
        )


@dataclass
class Output:
    name: str
    enabled: bool
    members: List[Member]

    @staticmethod
    def from_dict(d):
        return Output(
            name=d["name"], enabled=d.get("enabled", True),
            members=[Member.from_dict(m) for m in d.get("members", [])],
        )


@dataclass
class Split:
    left: str
    right: str
    code: str
    detail: str = ""

    @staticmethod
    def from_dict(d):
        return Split(**d)


@dataclass
class Unplaced:
    span: str
    confidence: str
    reason: str

    @staticmethod
    def from_dict(d):
        return Unplaced(**d)


@dataclass
class Plan:
    plan_version: int
    sources: List[Dict[str, Any]]       # [{id, path, size}]
    outputs: List[Output]
    splits: List[Split] = field(default_factory=list)
    unplaced: List[Unplaced] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return Plan(
            plan_version=d.get("plan_version", PLAN_VERSION),
            sources=d.get("sources", []),
            outputs=[Output.from_dict(o) for o in d.get("outputs", [])],
            splits=[Split.from_dict(s) for s in d.get("splits", [])],
            unplaced=[Unplaced.from_dict(u) for u in d.get("unplaced", [])],
        )

    def source_path(self, source_id):
        for s in self.sources:
            if s["id"] == source_id:
                return s["path"]
        return None


# ===========================================================================
# JSON I/O
# ===========================================================================

def dumps(obj):
    """Serialize a Report/Plan dataclass to a stable, pretty JSON string."""
    return json.dumps(obj.to_dict(), ensure_ascii=False, indent=2)


def save_report(report, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(report))


def load_report(path):
    with open(path, "r", encoding="utf-8") as f:
        return Report.from_dict(json.load(f))


def save_plan(plan, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(plan))


def load_plan(path):
    with open(path, "r", encoding="utf-8") as f:
        return Plan.from_dict(json.load(f))
