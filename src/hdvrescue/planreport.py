"""report — render a human-readable Markdown summary of a plan.json.

`plan` writes a machine-readable, hand-editable plan; this renders the same data
as a Markdown document you can read, archive, or share before running `build`.

It reads **only** the plan: every field it needs — recording timecodes, byte
ranges, PCR durations, the reason behind each join and split — is echoed into
plan.json, so the report never opens a source file. ``render_markdown`` is a pure
function of the plan (no clock, no I/O), which keeps it deterministic and testable.
"""

import os
import sys


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def _fmt_size(nbytes):
    """A byte count as a human size: 12.40 MB, 1.31 GB, 512 B."""
    if nbytes is None:
        return "?"
    for name, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if nbytes >= scale:
            return "%.2f %s" % (nbytes / scale, name)
    return "%d B" % nbytes


def _fmt_off(n):
    """A byte *offset* compactly, for a start–end range column: 0, 2.5M, 1.3G."""
    if n is None:
        return "?"
    if n == 0:
        return "0"
    for name, scale in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if n >= scale:
            return "%.1f%s" % (n / scale, name)
    return "%dB" % n


def _fmt_dur(sec):
    """Seconds as M:SS, or H:MM:SS past an hour."""
    if sec is None:
        return "?"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


def _esc(text):
    """Escape the one character that would break a Markdown table cell."""
    return (text or "").replace("|", "\\|")


def _source_id_of(span_id):
    """span_id is ``"<source_id>:<seq>"`` — recover the integer source id."""
    head = (span_id or "").split(":", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _member_bytes(m):
    if m.bytes and len(m.bytes) == 2:
        return m.bytes[1] - m.bytes[0]
    return None


def _member_dur(m):
    r = m.pcr_range_sec
    if r and len(r) == 2 and r[0] is not None and r[1] is not None:
        d = r[1] - r[0]
        return d if d >= 0 else None
    return None


def _member_date(m):
    if m.aux and m.aux.get("date"):
        return tuple(m.aux["date"])
    return None


def _fmt_tc(m, with_date=False):
    """The member's AUX recording timecode. Time only by default; full date+time
    when ``with_date`` (used when a member's day differs from its output's day)."""
    if not m.aux:
        return "—"
    date = m.aux.get("date")
    time = m.aux.get("time")
    ds = "%04d-%02d-%02d" % tuple(date) if date else None
    ts = "%02d:%02d:%02d" % tuple(time) if time else None
    if with_date and ds and ts:
        return ds + " " + ts
    if ts:
        return ts
    return ds or "—"


_JOIN_LABEL = {"verbatim": "verbatim", "discontinuity-marker": "marker",
               "cc-fix": "cc-fix"}

_SPLIT_HELP = {
    "pmt-class-differs": "节目结构不同，不是同一路流",
    "aux-date-mismatch": "录制日期不同，是不同的录制",
    "pcr-discontinuity": "PCR 重置或大跳变 —— 另一段录制；合并会让进度条无法拖动",
    "aux-elapsed-mismatch": "跨文件的 AUX 时间间隔超出可链接窗口",
    "aux-unknown": "一侧缺少 AUX 时间码，无法证明连续",
    "no-pmt-context": "缺少可解析的 PMT 上下文",
    "recording-boundary": "扫描阶段的录制边界",
    "pmt-class-mismatch": "节目结构不同，不是同一路流",
}


def _join_cell(m, first):
    if first or m.join is None:
        return "首段"
    label = _JOIN_LABEL.get(m.join.treatment, m.join.treatment)
    reason = (m.join.reason or "").strip()
    return "%s — %s" % (label, reason) if reason else label


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _render_output(L, o, plan):
    members = o.members
    size = sum(_member_bytes(m) or 0 for m in members)
    durs = [_member_dur(m) for m in members]
    total_dur = sum(d for d in durs if d is not None)
    has_dur = any(d is not None for d in durs)
    srcs = sorted({_source_id_of(m.span) for m in members} - {None})
    multi = len(srcs) > 1
    dates = sorted({_member_date(m) for m in members} - {None})
    out_date = dates[0] if dates else None

    L.append("### %s" % o.name)
    meta = ["%d 段" % len(members), _fmt_size(size)]
    if has_dur:
        meta.append("~%s" % _fmt_dur(total_dur))
    if not multi and srcs:
        sp = plan.source_path(srcs[0])
        if sp:
            meta.append("来源 `%s`" % os.path.basename(sp))
    if multi:
        meta.append("跨 %d 个来源" % len(srcs))
    if len(dates) > 1:
        meta.append("⚠ 跨 %d 个日期" % len(dates))
    L.append("**" + " · ".join(meta) + "**")
    L.append("")

    head = ["#", "span"]
    align = ["--:", "---"]
    if multi:
        head.append("来源")
        align.append("---")
    head += ["时间码", "字节范围", "接合"]
    align += ["---", "---", "---"]
    L.append("| " + " | ".join(head) + " |")
    L.append("|" + "|".join(align) + "|")

    for i, m in enumerate(members, 1):
        with_date = _member_date(m) is not None and _member_date(m) != out_date
        cells = ["%d" % i, "`%s`" % m.span]
        if multi:
            sp = plan.source_path(_source_id_of(m.span))
            cells.append("`%s`" % (os.path.basename(sp) if sp else "?"))
        cells.append(_fmt_tc(m, with_date))
        if m.bytes and len(m.bytes) == 2:
            cells.append("%s – %s" % (_fmt_off(m.bytes[0]), _fmt_off(m.bytes[1])))
        else:
            cells.append("?")
        cells.append(_esc(_join_cell(m, i == 1)))
        L.append("| " + " | ".join(cells) + " |")
    L.append("")


def render_markdown(plan):
    """Return a Markdown document summarizing ``plan`` (a ``model.Plan``)."""
    L = []
    enabled = [o for o in plan.outputs if o.enabled]
    disabled = [o for o in plan.outputs if not o.enabled]

    total_src = sum(s.get("size", 0) for s in plan.sources)
    total_out = sum(_member_bytes(m) or 0 for o in enabled for m in o.members)

    L.append("# hdvrescue 恢复计划")
    L.append("")
    L.append("- 来源：%d 个文件，共 %s" % (len(plan.sources), _fmt_size(total_src)))
    out_line = "- 输出：%d 个文件，共 %s" % (len(enabled), _fmt_size(total_out))
    if disabled:
        out_line += "（另有 %d 个已禁用）" % len(disabled)
    L.append(out_line)
    if plan.splits:
        L.append("- 拆分点：%d" % len(plan.splits))
    if plan.unplaced:
        L.append("- 未放置片段：%d" % len(plan.unplaced))
    L.append("")

    if plan.sources:
        L.append("| # | 来源文件 | 大小 |")
        L.append("|--:|----------|------|")
        for s in plan.sources:
            L.append("| %s | `%s` | %s |"
                     % (s.get("id"), s.get("path"), _fmt_size(s.get("size"))))
        L.append("")

    L.append("## 输出文件")
    L.append("")
    if enabled:
        for o in enabled:
            _render_output(L, o, plan)
    else:
        L.append("*(无启用的输出)*")
        L.append("")

    # Duplicate hint: two outputs sharing a start timecode are likely the same
    # footage carved twice (the `_a` suffix). Point at dedup to confirm by bytes.
    bykey = {}
    for o in enabled:
        if o.members:
            bykey.setdefault(_fmt_tc(o.members[0], with_date=True), []).append(o.name)
    dups = {k: v for k, v in bykey.items() if len(v) > 1}
    if dups:
        L.append("## 可能的重复")
        L.append("")
        L.append("> 多个输出共享同一起始时间码 —— 可能是恢复工具把同一段素材切出了两份。"
                 "运行 `hdvrescue dedup report.json` 按字节核对，再决定丢弃哪一份。")
        L.append("")
        for k, names in sorted(dups.items()):
            L.append("- **%s**：%s" % (k, "、".join("`%s`" % n for n in names)))
        L.append("")

    if disabled:
        L.append("## 已禁用（不会构建）")
        L.append("")
        for o in disabled:
            L.append("- `%s`（%d 段）" % (o.name, len(o.members)))
        L.append("")

    if plan.splits:
        L.append("## 拆分点")
        L.append("")
        L.append("> 相邻的两段为何**没有**合并成一个文件。错误的拆分只是多出一个文件，"
                 "错误的合并才会损坏文件，所以 plan 偏向拆分。")
        L.append("")
        L.append("| 左 | 右 | 原因 | 说明 |")
        L.append("|----|----|------|------|")
        for s in plan.splits:
            L.append("| `%s` | `%s` | `%s` | %s |"
                     % (s.left, s.right, s.code, _esc(s.detail)))
        L.append("")
        seen = [c for c in dict.fromkeys(s.code for s in plan.splits)
                if c in _SPLIT_HELP]
        if seen:
            for c in seen:
                L.append("- `%s`：%s" % (c, _SPLIT_HELP[c]))
            L.append("")

    if plan.unplaced:
        L.append("## 未放置片段")
        L.append("")
        L.append("> 置信度过低，未自动编入任何输出（避免把巧合的字节拼进文件）。"
                 "如确认有用，可在 plan.json 中手动加入某个输出。")
        L.append("")
        L.append("| span | 置信度 | 原因 |")
        L.append("|------|--------|------|")
        for u in plan.unplaced:
            L.append("| `%s` | %s | %s |" % (u.span, u.confidence, _esc(u.reason)))
        L.append("")

    return "\n".join(L).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main(argv):
    import argparse
    from . import model
    ap = argparse.ArgumentParser(
        prog="hdvrescue report",
        description="Render a human-readable Markdown summary of a plan.json.")
    ap.add_argument("plan", help="plan.json from plan")
    ap.add_argument("-o", "--output",
                    help="Markdown path (default: <plan>.md; '-' writes to stdout)")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.plan):
        print("error: no such plan: %s" % args.plan, file=sys.stderr)
        return 2
    plan = model.load_plan(args.plan)
    md = render_markdown(plan)

    out = args.output or (os.path.splitext(args.plan)[0] + ".md")
    if out == "-":
        sys.stdout.write(md)
        return 0
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print("report -> %s" % out, file=sys.stderr)
    return 0
