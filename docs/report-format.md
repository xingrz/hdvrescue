# `report.json` format

`scan` writes this. It describes each source file as a list of **spans** (byte
ranges that are internally trustworthy and seekable) and **gaps** (everything
else). For every source, `spans ∪ gaps` tile `[0, size)` exactly — no byte is
unaccounted for. The report references the *original* files by byte offset and is
never itself the recovered data; `build` copies bytes from the sources using these
offsets.

```jsonc
{
  "report_version": 1,
  "params": { "probe_mb": 4, "min_run": 4, "pcr_jump_sec": 5.0,
              "cc_tolerance": "strict", "aux_boundary_sec": 2.0, "hash": "none" },
  "sources": [
    {
      "id": 0,
      "path": "/abs/CLIP001.mpeg",
      "size": 48952,
      "hash": null,                       // size+mtime is build's "unchanged" guard
      "framing": { "stride": 188, "first_sync": 0, "slot_offset": 0,
                   "confidence": "high", "longest_run": 92, "note": null },
      "needs_attention": false,           // true => scanned but ambiguous/non-188
      "spans": [
        {
          "span_id": "0:000",             // "<source>:<index>" — stable handle
          "source_id": 0,
          "byte_start": 0, "byte_end": 17296,   // slot-aligned; end exclusive
          "packet_count": 92,
          "pmt": {
            "pmt_pid": 129, "pcr_pid": 308,
            "streams": [[2, 2064], [3, 2068], [160, 2069], [161, 2065]],
            "stream_type_set": [2, 3, 160, 161],   // the merge-grouping CLASS key
            "aux_pid": 2065, "aux_type": 161, "video_pid": 2064,
            "version": 0, "reassembled": false, "truncated": false,
            "signature": "st={0x2,0x3,0xa0,0xa1};aux=0xa1@0x811;pcr=0x134"
          },
          "pcr": { "pid": 308, "first": 100000, "last": 100000,
                   "sample_count": 30, "duration_sec": 29.0,
                   "wrapped": false, "monotonic": true },
          "pcr_pid_last_cc": 0,           // CC the marker repeats (PCR-only PID)
          "aux": {
            "pid": 2065,
            "first": { "date": [2007, 10, 18], "time": [9, 14, 3], "pes_offset": 752 },
            "last":  { "date": [2007, 10, 18], "time": [9, 14, 32], "pes_offset": 16920 },
            "truncated_seen": false       // anchor hit a window edge (≠ "no AUX")
          },
          "first_pusi_offset_by_pid": { "2064": 376 },   // PES alignment for joins
          "pts": null,                    // not extracted in v1
          "confidence": "high",           // high | medium | low
          "reasons": ["pmt", "pcr-monotonic", "aux-anchor"],
          "terminated_by": "eof"          // why the span ended (below)
        }
      ],
      "gaps": [
        { "byte_start": 17296, "byte_end": 32296, "length": 15000,
          "kind": "resync", "reason": "sync lost; resync downstream" }
      ],
      "summary": { "coverage_pct": 76.2, "span_count": 2, "gap_count": 1,
                   "distinct_pmt_classes": 1,
                   "distinct_aux_dates": ["2007-03-02", "2007-10-18"] }
    }
  ]
}
```

## Fields that drive decisions

- **`stream_type_set` + `aux_type`** form a span's *class*. `plan` only merges
  spans of the same class.
- **`confidence`** comes from how many independent corroborators a span has — a
  parseable PMT, ≥2 monotonic PCR samples, a Sony AUX anchor. `high` = all three;
  `medium` = two; `low` = one. `plan` excludes `low` spans from auto-chaining.
- **`aux.first` / `aux.last`** are the recording timecode at the span's start and
  end; `plan` orders and correlates by them using the real calendar.
- **`pcr_pid_last_cc`** is the continuity counter `build` repeats on the injected
  discontinuity marker (a dedicated PCR-only PID never increments its CC).

## `terminated_by` values

`pmt_change`, `pcr_discontinuity`, `cc_break`, `aux_recording_boundary` (the four
internal triggers that subdivide one clean run), `corruption` (sync was lost),
`eof`.

## `gaps[].kind` values

`leading` (bytes before the first sync), `resync` (corruption between two runs),
`no_resync` (no further sync run to end of file), `unstructured` (strided syncs
with no PSI/PCR/AUX — not trustworthy), `non_188` (192/204 framing, not built in
v1), `trailing` (sub-packet remainder at end of file).
