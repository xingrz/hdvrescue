# Working context for Claude

`hdvrescue` recovers footage from damaged HDV tape captures (Sony MPEG-TS,
`.mpeg`/`.m2t`). The damage is filesystem-level: a recovery tool (EasyRecovery,
PhotoRec) carving a lost partition inlines unrelated file fragments into the
stream and can split one recording across several carved files.

## The hard constraint

Every operation is byte-level 188-byte TS packet manipulation. **Never use
ffmpeg** — even `ffmpeg -c copy` strips Sony's private AUX stream
(`stream_type 0xA1`, usually PID `0x811`), which carries the camera rec-date/time.
Staying byte-level is the only way to losslessly preserve the original bytes and
that timecode. The timecode is core data: outputs are named by it and fragments
are correlated across files by it.

`hdvrescue verify FILE` is the authoritative check (exit 0 = the AUX recording
timecode is still readable).

## Architecture

Non-destructive and report-driven. Sources are read-only; bytes are copied once,
at `build`.

```
scan   sources              -> report.json   spans + gaps tiling [0,size) exactly
plan   report.json          -> plan.json     merge/split, human-editable
build  plan.json + sources  -> out/*.m2t     the only stage that copies bytes
verify a .m2t/.mpeg          -> exit 0/1/2    AUX-timecode survival
recover = scan -> plan -> build (+ --verify)
```

Load-bearing invariants (asserted + tested): no source mutation; **no silent
drop** (`spans ∪ gaps` tile each source exactly); **no untrustworthy span** (a
range is a span only with positive TS structure — a parseable PMT, ≥2 monotonic
PCR samples, or a Sony AUX anchor; coincidental strided `0x47` becomes a gap);
**no forged continuity across recordings** (continuity counters are rewritten only
for a proven same-stream cut; every other join gets a discontinuity marker only).

## Layout

```
src/hdvrescue/
  cli.py        argparse dispatch
  ts.py         byte-level TS primitives (the shared core)
  psi.py        PAT/PMT incl. multi-packet section reassembly
  aux.py        Sony AUX recording-timecode decode
  timecal.py    real-calendar AUX epoch (calendar.timegm)
  model.py      Span/Gap/Source/Report/Plan dataclasses + JSON
  scan.py  plan.py  build.py  verify.py  recover.py
tests/ fixtures.py + test_*.py   (synthetic TS, no sample data needed)
docs/  hdv-internals.md  report-format.md  plan-format.md
```

The byte-level primitives live in **one** place (`ts.py`/`psi.py`/`aux.py`) and
are imported everywhere. Do not fork copies into individual stage modules — a
single source of truth for packet parsing is deliberate.

## Things worth knowing about the data

- **Dedicated PCR-only PID.** A PMT often declares a PCR PID (e.g. `0x134`) that
  is not in the ES list; its packets are adaptation-only (AFC=2) and their
  continuity counter never increments. `build` injects its discontinuity marker
  on this PID and repeats the observed CC (a `+1` would force a wrong value).
  `psi.parse_pmt_section` reads it from the PMT header.
- **Sony AUX anchor.** `\x63 .{4} \xc0 .{4} \xff` inside a `private_stream_2`
  (`0xBF`) PES. The PES context is required — the bare anchor matches random video
  bytes by chance.
- **Over-recorded / spliced residue is real data.** A fragment's AUX timecode may
  be months before the rest. That is earlier footage from the same tape (or
  another session the recovery tool spliced in), not corruption. `plan` separates
  it into its own output by recording date; it must never be welded into the
  intended clip.
- **PMTs can span packets.** Parse them via `psi.SectionAssembler`, never from a
  single packet, or a multi-packet PMT's `0xA1` declaration can be missed.

## When making changes

- No ffmpeg, in any tool, ever.
- Keep the three stages decoupled through the JSON files: `plan` reads only the
  report, `build` reads plan + report + sources. `build` is the only writer of
  source bytes and must **assert** packet alignment, never slide to resync.
- Re-run `python -m unittest discover -s tests` after touching `ts.py`,
  `scan.py`, `plan.py`, or `build.py`. The fixtures cover tiling, the trust gate,
  cross-source correlation, over-record splitting, marker injection, and
  byte-fidelity.
- 192-byte M2TS and 204-byte (FEC) framings are detected and recorded as
  `non_188` gaps (visible, not lost) but not built in v1. The report schema
  carries the forward hook (`framing.slot_offset`) for adding build support.
