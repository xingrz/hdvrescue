# hdvrescue

Non-destructive recovery of footage from damaged HDV (Sony MPEG-TS) tape
captures — the `.mpeg`/`.m2t` files a disk-recovery tool (EasyRecovery, PhotoRec)
produces when it carves a lost partition. Such files mix the original recording
bytes with chunks of unrelated neighbouring files, and a single recording is
often split across several carved files. Players freeze mid-playback on them.

hdvrescue rebuilds clean, seekable files **without ever re-encoding or remuxing**.
Everything is byte-level 188-byte transport-stream manipulation; ffmpeg is never
used, because even `ffmpeg -c copy` strips Sony's private AUX stream
(`stream_type 0xA1`, usually PID `0x811`) — the stream that carries the camera's
recording date/time. That timecode is preserved exactly and is used to name
outputs and to correlate fragments across files.

## How it works

A non-destructive, report-driven pipeline. The original sources are read-only
throughout; bytes are copied exactly once, at the final `build` step.

```
   carved sources (.mpeg/.m2t, read-only)
        │
  scan   cut each source into trustworthy SPANS; everything else is a GAP.
  │      spans ∪ gaps tile the file exactly — nothing is silently dropped.
  ▼ report.json   byte-precise: offsets, PMT, PCR, AUX timecode, confidence
        │
  plan   group spans into output recordings: merge what is provably continuous
  │      (even across different source files), split where it is not.
  ▼ plan.json     human-editable: reorder / cut / force-join / rename
        │
  build  copy the exact byte ranges from the originals, fix continuity at each
  │      seam, name by AUX timecode, self-verify the timecode survived.
  ▼ out/2007-10-18_09-14-03.m2t  …
```

Why report-first instead of carve-then-merge: a scan that immediately writes out
fragments has to make irreversible decisions (minimum length, where to cut) while
it still has the least information. Here, scan only *describes* the bytes; the
merge/split decisions happen later against the full picture and are reviewable and
editable before a single output byte is written.

## Install

Python 3.9+, standard library only — no third-party packages, no ffmpeg.

```sh
pip install -e .          # provides the `hdvrescue` command
```

## Use

One shot:

```sh
hdvrescue recover CLIP001.mpeg CLIP002.mpeg -o out/ --verify
```

…or stay in the loop and inspect/edit between steps:

```sh
hdvrescue scan  CLIP001.mpeg CLIP002.mpeg -o report.json
hdvrescue plan  report.json -o plan.json      # then edit plan.json by hand
hdvrescue build plan.json --report report.json -o out/
hdvrescue verify out/2007-10-18_09-14-03.m2t
```

`plan.json` is the source of truth for `build`: reorder members, set
`"enabled": false`, move a span from one output to another, or rename an output,
and `build` honours it.

## Commands

| Command | Purpose |
| --- | --- |
| `scan INPUT… -o report.json` | Describe each source as byte-precise spans + gaps. |
| `plan report.json -o plan.json` | Propose merges and splits into a hand-editable plan. |
| `build plan.json -o out/` | Materialize the plan into final `.m2t` files (reads `report.json` beside the plan, or `--report`). |
| `verify FILE` | Is the Sony AUX recording timecode still readable? Exit `0` yes, `1` no, `2` error. |
| `recover INPUT… -o out/` | `scan → plan → build` in one pass; `report.json`/`plan.json` are saved in `out/`. |

Key scan knobs: `--cc-tolerance {strict,lenient}` (strict produces more, smaller,
internally-cleaner spans), `--pcr-jump-sec`, `--aux-boundary-sec`. Key plan knob:
`--max-chain-sec` (how large an AUX/PCR gap may be and still be chained).

## What you get

- Outputs named by their AUX recording timecode: `YYYY-MM-DD_HH-MM-SS.m2t`
  (`_a`, `_b`, … on collisions).
- A recording split across several carved files is reassembled into one output.
- Over-recorded tape residue and disk-recovery cross-file contamination (real
  footage from a *different* session that the recovery tool spliced in) are
  separated into their own outputs by recording date — not welded into your clip.
- The `0xA1` AUX stream is byte-preserved, so the recording timecode is intact.

## Documentation

- [docs/hdv-internals.md](docs/hdv-internals.md) — HDV/MPEG-TS byte-level reference.
- [docs/report-format.md](docs/report-format.md) — `report.json` schema.
- [docs/plan-format.md](docs/plan-format.md) — `plan.json` schema and how to edit it.

## Development

```sh
python -m unittest discover -s tests
```

Tests are deterministic and use tiny synthetic transport streams built in
[tests/fixtures.py](tests/fixtures.py) — no sample captures required.
