# 2026-11-20 — Stage 1's force abort follows the configured counts_per_gf

## Why

Stage 1's force abort (`QIDI_FLOW_RAMP`, `QIDI_FLOW_SEARCH`,
`QIDI_FLOW_BED_SEARCH`) came from a module-level constant:

    DEFAULT_ABORT_COUNTS = int(ABORT_GF * COUNTS_PER_GF)

That uses the module's built-in `COUNTS_PER_GF` (201), **not** the
`counts_per_gf` read from `[qidi_flow_ramp]` / `[qidi_flow_bed_search]`.
Everything else already used the configured value: every reported force,
the variance abort, and Stage 3's own abort
(`abort_counts = ABORT_GF * self.counts_per_gf`). So setting
`counts_per_gf` changed what the console printed, but not where Stage 1
stopped.

On a Q2 whose cell measures 178.76 counts/gf (13 points, 207-1787 gf
against a scale, R² 0.99999), configured as 178, the abort sat at
1650 × 201 = 331,650 counts. That is ~1863 gf: 93% of the cell's 2000 gf
rating, instead of the intended 1650. A live run printed
`force 1886 gf over the 1863 gf limit`. QIDI disables Klipper's own
extrusion limits on this printer, so this abort is the only overload
protection there is, as `SAFETY.md` says.

## What changed

`qidi_flow_ramp.py` and `qidi_flow_bed_search.py`:
`DEFAULT_ABORT_COUNTS` is replaced by `_default_abort_counts(counts_per_gf)`.
It is called per command with `self.counts_per_gf`. `ABORT_COUNTS=` on the
command line still overrides it.

`test_flow_ramp.py` and `test_flow_bed_search.py` set `counts_per_gf` to
178 and check the abort each command uses: 293,700 counts (1650 × 178). On
the previous modules they get 331,650 and fail.

## Not changed

`QIDI/Max4/qidi_flow_ramp.py` and `qidi_flow_bed_search.py` build the same
constant from their own `COUNTS_PER_GF`. A Max4 configured with any other
`counts_per_gf` has the same mismatch.
