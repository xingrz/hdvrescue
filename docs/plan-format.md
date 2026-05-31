# `plan.json` format

`plan` writes this from a report; `build` reads it. It groups spans into output
files. It is designed to be **edited by hand** before building — that is the whole
point of having a plan step.

```jsonc
{
  "plan_version": 1,
  "sources": [
    { "id": 0, "path": "/abs/CLIP001.mpeg", "size": 48952 },
    { "id": 1, "path": "/abs/CLIP002.mpeg", "size": 14476 }
  ],
  "outputs": [
    {
      "name": "2007-10-18_09-14-03.m2t",
      "enabled": true,
      "members": [
        {
          "span": "0:000",               // <- load-bearing: a span_id from the report
          "join": null,                  //    first member: nothing to join
          "source_file": "/abs/CLIP001.mpeg",   // v-- echoed for review; build ignores
          "aux": { "date": [2007, 10, 18], "time": [9, 14, 3] },
          "pcr_range_sec": [0.0, 29.0],
          "bytes": [0, 17296]
        },
        {
          "span": "1:000",
          "join": {                      // <- load-bearing: how to stitch this seam
            "treatment": "discontinuity-marker",
            "provenance": "cross-source",
            "confidence": 0.8,
            "reason": "AUX continuous across cross-source (+1s)"
          },
          "source_file": "/abs/CLIP002.mpeg",
          "aux": { "date": [2007, 10, 18], "time": [9, 14, 33] },
          "pcr_range_sec": [0.0, 24.0],
          "bytes": [0, 14476]
        }
      ]
    }
  ],
  "splits": [
    { "left": "0:001", "right": "0:000", "code": "aux-date-mismatch",
      "detail": "[2007, 3, 2] vs [2007, 10, 18]" }
  ],
  "unplaced": [
    { "span": "2:007", "confidence": "low", "reason": "below medium confidence" }
  ]
}
```

## What `build` actually reads

Only two fields per member are load-bearing: **`span`** (which span, by id) and
**`join.treatment`** (how to stitch it to the previous member). Everything else —
`source_file`, `aux`, `pcr_range_sec`, `bytes` — is echoed from the report to make
the plan readable, and `build` re-resolves the authoritative bytes from the report
by `span` id, so editing those echoes has no effect.

## Editing the plan

- **Drop an output:** set `"enabled": false` (keeps it on record) or delete it.
- **Re-group:** move a member object from one output's `members` to another. The
  `join` lives on the *second* member of a pair, so cutting/pasting one object is
  enough to re-stitch.
- **Force a merge** the planner refused (e.g. two cameras at one event, different
  AUX dates): append the span as a member with
  `"join": {"treatment": "discontinuity-marker", ...}`. `build` honours it.
- **Recover an `unplaced` span:** add it as a member of some output.
- **Rename:** change `name`.

## How spans are grouped into outputs

`plan` keeps a recording **whole** while it stays *seekable*, and splits where a
merge would not be:

- **Same source, byte-adjacent** (`B.start == A.end`) → `verbatim`. The bytes are
  already contiguous in the capture, so copying them reproduces a range that
  already played — a camera pause/resume (the AUX timecode jumps but the capture
  ran on) is kept together, not split.
- **Same source, with a corruption gap** → merged with a `discontinuity-marker`
  **only while the PCR clock stays seekable**: monotonic forward within
  `--max-pcr-jump-sec` (default 30 s). A PCR **reset** or a large leap means a
  separate recording session, which is split (`pcr-discontinuity`) — merging it
  would make the player's duration bogus and the scrub bar un-draggable.
- **Cross-source** (a recording carved across files) → merged with a marker only
  when the AUX recording timecode agrees within `--max-chain-sec`. Capture files
  carry independent PCR clocks, so seek-continuity can't be proven here.
- **Different recording day** → always split (`aux-date-mismatch`).

## Join treatments

- **`verbatim`** — copy bytes unchanged. Used for the first member and for every
  byte-adjacent same-source seam (contiguous, nothing to signal).
- **`discontinuity-marker`** — inject one adaptation-only marker on the entered
  span's PCR PID, then copy bytes unchanged. Continuity counters are *not* forged,
  so a real packet loss is never concealed. This is what the planner emits for a
  gap or cross-source join, and it is always safe.
- **`cc-fix`** — rewrite continuity counters to continue the previous member, no
  marker. `build` honours it for hand-edited plans, but the planner never emits it
  automatically because the report cannot prove zero packet loss across a seam.

## `splits[].code` values

Why two adjacent-in-time spans were *not* merged: `pmt-class-differs`,
`aux-date-mismatch` (different day), `pcr-discontinuity` (PCR reset/leap — a
separate session; merging would break seeking), `aux-elapsed-mismatch` /
`aux-unknown` (cross-source AUX disagrees or is missing), `no-pmt-context`. A
wrong split just yields two good files, so `plan` errs toward splitting; review
these if you expected a merge. Raise `--max-pcr-jump-sec` to merge more
aggressively within a day.

## Duplicate fragments (`hdvrescue dedup`)

A disk-recovery tool can carve the same footage twice, so two spans may cover the
same recording moment (you'll see two outputs with the same timecode, the second
suffixed `_a`). `plan` reads no bytes, so it can't tell them apart — run
`hdvrescue dedup report.json`, which reads just the candidate ranges and reports,
per shared-timecode group: `identical` / `contained` (a byte-prefix of the longer
copy — safe to drop the shorter) / `diverges at byte N` (not the same capture past
there — keep both). Add `--md5` for full hashes. It only reports; you then disable
the redundant members in `plan.json`.
