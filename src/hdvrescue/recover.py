"""recover — the one-shot wrapper: scan -> plan -> build (+ optional verify).

Writes ``report.json`` and ``plan.json`` into the output directory so the run is
inspectable and resumable: ``--from-report`` skips scanning, ``--from-plan`` skips
scanning and planning (build only). The individual subcommands give the same
control with manual hand-off; this just chains them with sensible defaults.
"""

import os

from . import scan, plan as planmod, build as buildmod, verify, model


def recover(paths, outdir, scan_params=None, plan_params=None, do_verify=False,
            from_report=None, from_plan=None, on_exist="error", log=None):
    log = log or (lambda *_: None)
    os.makedirs(outdir, exist_ok=True)
    report_path = os.path.join(outdir, "report.json")
    plan_path = os.path.join(outdir, "plan.json")

    if from_plan:
        report = model.load_report(from_report or report_path)
        plan_obj = model.load_plan(from_plan)
        log("[recover] loaded plan %s" % from_plan)
    else:
        if from_report:
            report = model.load_report(from_report)
            log("[recover] loaded report %s" % from_report)
        else:
            log("[scan] %d source(s)..." % len(paths))
            report = scan.scan(paths, scan_params)
            model.save_report(report, report_path)
            spans = sum(len(s.spans) for s in report.sources)
            gaps = sum(len(s.gaps) for s in report.sources)
            log("[scan] %d span(s), %d gap(s) -> %s" % (spans, gaps, report_path))
        plan_obj = planmod.make_plan(report, plan_params)
        model.save_plan(plan_obj, plan_path)
        log("[plan] %d output(s), %d split(s), %d unplaced -> %s" % (
            len(plan_obj.outputs), len(plan_obj.splits), len(plan_obj.unplaced),
            plan_path))

    log("[build] writing %d output(s) to %s/" % (
        sum(1 for o in plan_obj.outputs if o.enabled), outdir))
    results = buildmod.build(plan_obj, report, outdir, on_exist)

    if do_verify:
        for r in results:
            if r.get("skipped"):
                continue
            code = 0 if r.get("verified") else 1
            log("[verify] %s  ->  %s" % (
                r["name"], "OK %s" % r.get("timecode") if code == 0 else "NO timecode"))
    return report, plan_obj, results
