"""hdvrescue — non-destructive, report-driven recovery of damaged HDV captures.

The package is organised as a dataflow of read-only stages over the original
carved source files:

    scan   sources              -> report.json   (byte-precise spans + gaps)
    plan   report.json          -> plan.json     (human-editable merge/split plan)
    build  plan.json + sources  -> out/*.m2t     (the only stage that copies bytes)
    verify a .m2t/.mpeg file     -> exit 0/1/2    (AUX timecode survival check)

    recover = scan -> plan -> build (+ optional verify)

Every operation is byte-level 188-byte MPEG-TS packet manipulation. ffmpeg is
never used, because even ``ffmpeg -c copy`` strips Sony's private AUX stream
(``stream_type 0xA1``) that carries the camera recording date/time.
"""

__version__ = "0.1.0"

# Byte-level constants shared across the whole package.
SYNC = 0x47          # TS sync byte
TS = 188             # Sony HDV transport-stream packet size
PCR_HZ = 90000       # PCR base clock, 90 kHz ticks
PCR_MAX = 1 << 33    # PCR base wraps at 2**33 (~26.5 hours)
