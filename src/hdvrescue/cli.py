"""hdvrescue CLI: scan | plan | build | verify | recover | dedup."""

import argparse
import os
import sys

from . import __version__
from . import scan as scanmod
from . import plan as planmod
from . import build as buildmod
from . import verify as verifymod
from . import recover as recovermod
from . import dedup as dedupmod
from . import model


def _err(msg):
    print("error: %s" % msg, file=sys.stderr)


# Only show a progress bar for work large enough to be worth it (keeps small
# files and the test suite quiet).
_PROGRESS_MIN_BYTES = 64 * 1024 * 1024


class _Bar:
    """A single progress line. Live (carriage-return) on a TTY; periodic 10%
    lines otherwise, so redirected logs stay readable."""

    def __init__(self, label, total):
        self.label = label
        self.total = max(1, total)
        self.tty = sys.stderr.isatty()
        self.shown = -1

    def update(self, done, total):
        self.total = max(1, total)
        pct = min(100, int(100 * done / self.total))
        mb_done, mb_total = done >> 20, self.total >> 20
        if self.tty:
            if pct == self.shown:
                return
            sys.stderr.write("\r  %s  %3d%%  (%d/%d MB)" %
                             (self.label, pct, mb_done, mb_total))
            sys.stderr.flush()
            self.shown = pct
        else:
            step = (pct // 10) * 10
            if step > self.shown:
                sys.stderr.write("  %s  %d%% (%d/%d MB)\n" %
                                 (self.label, step, mb_done, mb_total))
                self.shown = step

    def finish(self):
        if self.tty and self.shown >= 0:
            sys.stderr.write("\r  %s  done%s\n" % (self.label, " " * 28))
            sys.stderr.flush()


def _make_progress():
    """Return a callback ``(key, done, total)`` that renders one bar per key."""
    bars = {}
    skip = set()

    def cb(key, done, total):
        if key in skip:
            return
        bar = bars.get(key)
        if bar is None:
            if total < _PROGRESS_MIN_BYTES:
                skip.add(key)
                return
            bar = bars[key] = _Bar(os.path.basename(key), total)
        bar.update(done, total)
        if done >= total:
            bar.finish()

    return cb


def _add_scan_knobs(ap):
    ap.add_argument("--probe-mb", type=int, default=scanmod.DEFAULT_PARAMS["probe_mb"],
                    help="MB scanned to detect TS framing (default %(default)s)")
    ap.add_argument("--min-run", type=int, default=scanmod.DEFAULT_PARAMS["min_run"],
                    help="consecutive syncs to declare in-sync (default %(default)s)")
    ap.add_argument("--pcr-jump-sec", type=float,
                    default=scanmod.DEFAULT_PARAMS["pcr_jump_sec"],
                    help="PCR step beyond this = discontinuity (default %(default)s)")
    ap.add_argument("--cc-tolerance", choices=("strict", "lenient"),
                    default=scanmod.DEFAULT_PARAMS["cc_tolerance"],
                    help="continuity-counter break tolerance (default %(default)s)")
    ap.add_argument("--aux-boundary-sec", type=float,
                    default=scanmod.DEFAULT_PARAMS["aux_boundary_sec"],
                    help="AUX vs PCR slack for a recording boundary (default %(default)s)")


def _scan_params(args):
    return {"probe_mb": args.probe_mb, "min_run": args.min_run,
            "pcr_jump_sec": args.pcr_jump_sec, "cc_tolerance": args.cc_tolerance,
            "aux_boundary_sec": args.aux_boundary_sec}


def _add_plan_knobs(ap):
    ap.add_argument("--max-chain-sec", type=float,
                    default=planmod.DEFAULT_PARAMS["max_chain_sec"],
                    help="cross-source: max AUX gap still chained (default %(default)s)")
    ap.add_argument("--max-pcr-jump-sec", type=float,
                    default=planmod.DEFAULT_PARAMS["max_pcr_jump_sec"],
                    help="same-source: merge same-day spans while the PCR clock "
                         "stays within this many seconds (a larger jump = separate "
                         "session, kept seekable; default %(default)s)")
    ap.add_argument("--aux-boundary-sec", type=float,
                    default=planmod.DEFAULT_PARAMS["aux_boundary_sec"],
                    help="AUX agreement slack (default %(default)s)")
    ap.add_argument("--min-confidence", choices=("low", "medium", "high"),
                    default=planmod.DEFAULT_PARAMS["min_confidence"],
                    help="spans below this go to unplaced[] (default %(default)s)")


def _plan_params(args):
    return {"max_chain_sec": args.max_chain_sec,
            "max_pcr_jump_sec": args.max_pcr_jump_sec,
            "aux_boundary_sec": args.aux_boundary_sec,
            "min_confidence": args.min_confidence}


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_scan(args):
    for p in args.inputs:
        if not os.path.isfile(p):
            _err("no such file: %s" % p)
            return 2
    report = scanmod.scan(args.inputs, _scan_params(args),
                          on_progress=_make_progress())
    model.save_report(report, args.output)
    for s in report.sources:
        print("%s: %d span(s), %d gap(s), %.1f%% covered%s"
              % (os.path.basename(s.path), len(s.spans), len(s.gaps),
                 s.summary.get("coverage_pct", 0),
                 "  [needs attention]" if s.needs_attention else ""),
              file=sys.stderr)
        if len(s.summary.get("distinct_aux_dates", [])) > 1:
            print("    over-record / contamination: dates %s"
                  % ", ".join(s.summary["distinct_aux_dates"]), file=sys.stderr)
    print("report -> %s" % args.output, file=sys.stderr)
    return 0


def cmd_plan(args):
    if not os.path.isfile(args.report):
        _err("no such report: %s" % args.report)
        return 2
    report = model.load_report(args.report)
    plan = planmod.make_plan(report, _plan_params(args))
    model.save_plan(plan, args.output)
    for o in plan.outputs:
        if not o.enabled:
            continue
        joins = sum(1 for m in o.members if m.join)
        print("  %s  (%d span%s%s)" % (
            o.name, len(o.members), "" if len(o.members) == 1 else "s",
            ", %d join" % joins if joins else ""), file=sys.stderr)
    if plan.splits:
        print("  %d split point(s)" % len(plan.splits), file=sys.stderr)
    if plan.unplaced:
        print("  %d unplaced span(s)" % len(plan.unplaced), file=sys.stderr)
    print("plan -> %s" % args.output, file=sys.stderr)
    return 0


def cmd_build(args):
    if not os.path.isfile(args.plan):
        _err("no such plan: %s" % args.plan)
        return 2
    report_path = args.report or os.path.join(os.path.dirname(args.plan) or ".",
                                              "report.json")
    if not os.path.isfile(report_path):
        _err("report not found (looked for %s); pass --report" % report_path)
        return 2
    plan = model.load_plan(args.plan)
    report = model.load_report(report_path)
    try:
        results = buildmod.build(plan, report, args.output, args.on_exist,
                                 on_progress=_make_progress())
    except buildmod.BuildError as e:
        _err(str(e))
        return 2
    for r in results:
        if r.get("skipped"):
            print("  skip %s (exists)" % r["name"], file=sys.stderr)
        else:
            print("  %s  (%.2f MB, %d marker%s)%s" % (
                r["name"], r["bytes"] / 1048576.0, r["markers"],
                "" if r["markers"] == 1 else "s",
                "  -> %s" % r["timecode"] if r["timecode"] else ""),
                file=sys.stderr)
    print("built %d output(s) in %s/" %
          (len(results), args.output), file=sys.stderr)
    return 0


def cmd_verify(args):
    return verifymod.main([args.file]
                          + (["--window-mb", str(args.window_mb)])
                          + (["--quiet"] if args.quiet else []))


def cmd_dedup(args):
    return dedupmod.main([args.report] + (["--md5"] if args.md5 else []))


def cmd_recover(args):
    if not args.from_plan and not args.from_report:
        for p in args.inputs:
            if not os.path.isfile(p):
                _err("no such file: %s" % p)
                return 2
    try:
        _, _, results = recovermod.recover(
            args.inputs, args.output,
            scan_params=_scan_params(args), plan_params=_plan_params(args),
            do_verify=args.verify, from_report=args.from_report,
            from_plan=args.from_plan, on_exist=args.on_exist,
            log=lambda m: print(m, file=sys.stderr),
            on_progress=_make_progress())
    except buildmod.BuildError as e:
        _err(str(e))
        return 2
    ok = all(r.get("verified") or r.get("skipped") for r in results) \
        if args.verify else True
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog="hdvrescue",
        description="Non-destructive recovery of damaged HDV (Sony MPEG-TS) "
                    "captures: scan -> report -> plan -> build.")
    ap.add_argument("--version", action="version",
                    version="hdvrescue %s" % __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="scan sources -> report.json")
    sp.add_argument("inputs", nargs="+", help="source .mpeg/.m2t file(s)")
    sp.add_argument("-o", "--output", default="report.json",
                    help="report path (default report.json)")
    _add_scan_knobs(sp)
    sp.set_defaults(func=cmd_scan)

    pp = sub.add_parser("plan", help="report.json -> plan.json")
    pp.add_argument("report", help="report.json from scan")
    pp.add_argument("-o", "--output", default="plan.json",
                    help="plan path (default plan.json)")
    _add_plan_knobs(pp)
    pp.set_defaults(func=cmd_plan)

    bp = sub.add_parser(
        "build", help="plan.json + report.json + sources -> out/")
    bp.add_argument("plan", help="plan.json from plan")
    bp.add_argument("-o", "--output", required=True, help="output directory")
    bp.add_argument("--report", help="report.json (default: beside the plan)")
    bp.add_argument("--on-exist", choices=("error", "skip", "suffix"),
                    default="error", help="when an output exists (default error)")
    bp.set_defaults(func=cmd_build)

    dp = sub.add_parser(
        "dedup", help="byte-verify duplicate fragments in a report.json")
    dp.add_argument("report", help="report.json from scan")
    dp.add_argument("--md5", action="store_true",
                    help="also compute each candidate's full MD5 (slower)")
    dp.set_defaults(func=cmd_dedup)

    vp = sub.add_parser("verify", help="check a TS file for the AUX timecode")
    vp.add_argument("file")
    vp.add_argument("--window-mb", type=int, default=64)
    vp.add_argument("--quiet", action="store_true")
    vp.set_defaults(func=cmd_verify)

    rp = sub.add_parser("recover", help="scan -> plan -> build in one pass")
    rp.add_argument("inputs", nargs="*", help="source .mpeg/.m2t file(s)")
    rp.add_argument("-o", "--output", required=True, help="output directory")
    rp.add_argument("--verify", action="store_true",
                    help="verify each output's AUX timecode after building")
    rp.add_argument("--from-report", help="skip scan; use this report.json")
    rp.add_argument("--from-plan", help="skip scan+plan; use this plan.json")
    rp.add_argument("--on-exist", choices=("error", "skip", "suffix"),
                    default="error")
    _add_scan_knobs(rp)
    rp.add_argument("--max-chain-sec", type=float,
                    default=planmod.DEFAULT_PARAMS["max_chain_sec"])
    rp.add_argument("--max-pcr-jump-sec", type=float,
                    default=planmod.DEFAULT_PARAMS["max_pcr_jump_sec"])
    rp.add_argument("--min-confidence", choices=("low", "medium", "high"),
                    default=planmod.DEFAULT_PARAMS["min_confidence"])
    rp.set_defaults(func=cmd_recover)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
