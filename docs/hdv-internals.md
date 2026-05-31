# HDV / MPEG-TS internals — reference

Everything needed to know about HDV-flavored MPEG-TS to work on this project.
Sourced from ISO/IEC 13818-1, the Sony HDV documentation, and direct byte-level
inspection of real captures.

## TS framing

- **188 bytes per packet** (Sony HDV). M2TS (Blu-ray) uses 192-byte packets (a
  4-byte timestamp prefix per packet); some captures use 204 bytes (with
  Reed-Solomon FEC). `ts.detect_framing` probes all three and picks the stride
  with the **longest** run of strided syncs. v1 builds only 188; a confidently
  detected 192/204 region is recorded as a `non_188` gap, never mis-scanned.
- Sync byte: `0x47` at the start of each packet.
- A valid stream has `0x47` at stride 188 for many consecutive packets. Framing
  detection requires a run of several consecutive syncs to declare "in sync",
  which raises the bar against coincidental matches in random data.

## Packet header (4 bytes)

```
byte 0: 0x47 (sync)
byte 1: 0bABCDDDDD
            A     = transport_error_indicator
            B     = payload_unit_start_indicator (PUSI)
            C     = transport_priority
            DDDDD = top 5 bits of 13-bit PID
byte 2: low 8 bits of PID
byte 3: 0bAABBCCCC
            AA    = transport_scrambling_control
            BB    = adaptation_field_control (AFC)
                       01 = payload only
                       10 = adaptation field only (no payload)
                       11 = both
            CCCC  = continuity_counter (CC)
```

**CC** increments by 1 (mod 16) per *payload-bearing* packet of that PID (AFC 01
or 11). Adaptation-only packets (AFC 10) and null packets do **not** increment it
— which is why a dedicated PCR-only PID carries a constant CC. A duplicate CC is
allowed once (for a re-sent packet). Any other deviation is a continuity error;
decoders treat it as packet loss. `scan` ends a span at such a break, so each span
is internally continuous.

## Adaptation field (if AFC ∈ {10, 11})

```
byte 0:    adaptation_field_length  (covers bytes AFTER this length byte)
byte 1:    flags (only present if length > 0)
            bit 7: discontinuity_indicator     <- set on injected boundary markers
            bit 6: random_access_indicator
            bit 5: elementary_stream_priority_indicator
            bit 4: PCR_flag
            bit 3: OPCR_flag
            bit 2: splicing_point_flag
            bit 1: transport_private_data_flag
            bit 0: adaptation_field_extension_flag
bytes 2..7: PCR (if PCR_flag): 33-bit base + 6 reserved + 9-bit extension
... optional fields gated by other flags ...
... stuffing 0xFF to fill `adaptation_field_length` ...
```

Reading the `discontinuity_indicator` is guarded: only AFC ∈ {2,3} packets with a
non-zero `adaptation_field_length` have the flags byte at all (otherwise byte 5 is
stuffing or payload).

**PCR base** is in 90 kHz ticks. Within a packet the 33-bit value lives across
bytes 6..10 as `(b6<<25) | (b7<<17) | (b8<<9) | (b9<<1) | (b10>>7)`. PCR wraps at
2^33 (~26.5 hours); `ts.pcr_diff` corrects a difference only when it is within a
narrow guard of a full wrap, so a large backward corruption is never rewritten
into a fake forward wrap.

**discontinuity_indicator** — when set, tells the decoder "the next PCR and/or CC
values may not match the expected continuation; reset STC tracking, don't treat it
as packet loss." `build` injects a marker packet with only this flag set at each
merge boundary (`ts.make_disc_marker`).

## PAT and PMT

**PAT** (PID `0x0000`): maps program number → PMT PID.

**PMT** structure (after pointer_field + section header):

```
byte t+0:       table_id = 0x02
bytes t+1..2:   ... section_length (12 bits)
bytes t+3..4:   program_number (16 bits)
byte t+5:       reserved(2) | version_number(5) | current_next(1)
byte t+6:       section_number
byte t+7:       last_section_number
bytes t+8..9:   reserved(3) | PCR_PID(13)         <- can be a dedicated PID
bytes t+10..11: reserved(4) | program_info_length(12)
... program_info descriptors ...
... ES loop (stream_type | PID | es_info_length | descriptors) repeated ...
CRC_32
```

Typical Sony HDV PMT:

| stream_type | PID | What |
| --- | --- | --- |
| `0x02` | `0x810` | MPEG-2 video |
| `0x03` | `0x814` | MPEG-1 Layer II audio |
| `0xA0` | `0x815` | Sony AUX (another flavor, usually empty) |
| `0xA1` | `0x811` | **Sony AUX carrying recording timecode** |

Two things to get right:

- **PCR_PID** in the PMT header is sometimes a dedicated PCR-only PID (e.g.
  `0x134`) that does not appear in the ES list. Its packets carry only an
  adaptation field with PCR, no payload. `psi.parse_pmt_section` reads it from
  bytes `t+8..t+9`.
- **A PMT can span multiple TS packets.** Parsing only the first packet can miss a
  trailing `0xA1` AUX declaration. `psi.SectionAssembler` reassembles the full
  section (following the pointer_field across continuation packets) before parsing.

## Sony AUX recording-timecode encoding

AUX packets are `private_stream_2` PES (`00 00 01 BF`), one per video frame.
Inside the PES body the recording-time anchor is:

```
0x63 ?? ?? ?? ??   0xC0 ?? DD MM YY   0xFF   SS  MM  HH
^ time-pack header ^ date pack               ^ BCD seconds/minutes/hours
                                                (reversed byte order)
```

Date decoding (the `0xC0` pack header is the anchor of the date fields):

```
day   = BCD(byte[+2] & 0x3F)
month = BCD(byte[+3] & 0x1F)
yb    = BCD(byte[+4])
year  = 1900 + yb  if yb >= 75 else 2000 + yb
```

Time decoding:

```
second = BCD(byte[+11] & 0x7F)
minute = BCD(byte[+12] & 0x7F)
hour   = BCD(byte[+13] & 0x3F)
```

The PES context (`00 00 01 BF` immediately preceding the anchor) is required when
scanning raw bytes — the bare ~11-byte anchor appears by chance in a gigabyte of
MPEG-2 video. `aux.decode_aux_pes` decodes from the PES header; `aux.sony_aux_decode`
scans a body and reports `truncated_seen` when an anchor matches but its time bytes
run off the end of the window (a windowing artefact, not missing data). `plan`
orders and correlates fragments by these timecodes.

## Why plain ffmpeg loses the timecode

`ffmpeg -c copy` (any variant) will:

- Relabel `stream_type 0xA1` → `0x06` (private_data) in the output PMT.
- Re-mux the PES, dropping the Sony anchor structure.

There is no known ffmpeg flag combination that preserves the `0xA1` framing
intact. That is *the* reason every operation here is byte-level. `hdvrescue verify`
reports whether the timecode is still readable.

## Why TS files don't seek precisely

MPEG-TS has no global index. Players estimate seek positions as
`time / duration * file_size` (linear), then snap to the nearest PAT/PMT + I-frame.
Two common failure modes:

1. **PCR/DTS discontinuities** inflate the computed duration. A 5-minute file with
   one mid-file PCR jump of one hour reports ~65 minutes total; seeking to 30
   minutes lands in imaginary time.
2. **CC breaks** mid-file poison the player's PCR-vs-byte-position index.

`scan` addresses both by ending a span at a PCR discontinuity or a CC break, so
each span is internally monotonic and continuous. `build` then signals each
inter-span seam with a discontinuity marker, so the decoder resets cleanly at the
boundary instead of mistaking it for packet loss.
