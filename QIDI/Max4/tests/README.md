# Tests

## Off-printer tests — no hardware needed

```bash
python3 test_discover.py
```
Mock Klipper + a fake QIDI-style sensor. Checks the discovery module finds the
live reading, ignores static decoys, never invokes a callable, and measures
sample rate correctly at 40 / 640 / 1280 SPS. Needs nothing but python3.

## Rate-adequacy tests — need numpy, scipy, and autopa's analysis

These synthesise force traces with a known K and run them through autopa's
**real** analysis code, which is not bundled here (it's AGPL-3.0 and belongs to
its authors). Fetch it first:

```bash
# see rate_adequacy.py's header - fetch autopa yourself if you want to re-run it
pip install numpy scipy      # if not already present

python3 rate_adequacy.py     # error in recovered K at each sample rate
python3 rate_sensitivity.py  # does recovered K TRACK true K? (the real test)
python3 noise_robustness.py  # how much sensor noise before it breaks
```

`rate_sensitivity.py` is the one that matters. A single test point can be right
by luck — what counts is whether recovered K moves with true K across a range.

## Results already obtained

| Rate | Tracks true K | R² | Slope | Run-to-run spread |
|---|---|---|---|---|
| 40 SPS | yes, monotonic | 0.999 | 0.900 | ±0.0015 (~4%) |
| 640 SPS | yes, monotonic | 1.000 | 0.879 | ±0.0008 (~2%) |
| 1280 SPS | yes, monotonic | 0.999 | 0.874 | ±0.0007 (~2%) |

Slope consistently below 1 across all rates = a systematic, correctable bias in
the estimator against this melt model, not a sample-rate limitation.

Noise tolerance: both 40 and 1280 SPS hold up until noise reaches ~2% of the
force step between legs (~15 gf on a 640 gf step); 1280 keeps roughly 4x tighter
repeatability throughout.

**Caveat:** a first-order melt model, not real hardware. This shows 40 SPS is
not disqualified on sample-count grounds. It cannot show the real signal behaves.
