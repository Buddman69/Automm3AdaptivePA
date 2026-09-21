"""How much sensor noise can each rate tolerate before K becomes unreliable?

The 40 SPS result depends on autopa resampling to 1 kHz and aggregating ~120
transitions. That averaging is what rescues a slow sensor -- but averaging only
works while the noise stays small relative to the force step. This sweeps noise
to find where each rate breaks.
"""
import math, os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from rate_adequacy import run_sweep, GAIN_GF_PER_MM_S, A_FIL

TRUE_K = 0.035
NOISE_GF = [0.5, 2., 5., 15., 40., 100.]
TRIALS = 3

step_gf = GAIN_GF_PER_MM_S * (16.0 - 2.0) / A_FIL
print("force step between legs: %.0f gf  (2 -> 16 mm3/s)" % step_gf)
print("true K = %.4f\n" % TRUE_K)
print("  %-10s %10s %10s %10s %10s" % ("rate", "noise gf", "noise %step",
                                       "mean K", "spread"))
print("  " + "-" * 54)
for rate in (40., 1280.):
    for noise in NOISE_GF:
        ks = []
        for s in range(TRIALS):
            k, _ = run_sweep(TRUE_K, rate, 2.0, 16.0, noise, 4242 + s)
            if k is not None and np.isfinite(k):
                ks.append(k)
        if ks:
            print("  %-10s %10.1f %9.2f%% %10.4f %10.4f"
                  % ("%.0f SPS" % rate, noise, 100 * noise / step_gf,
                     float(np.mean(ks)), float(np.std(ks))))
        else:
            print("  %-10s %10.1f %9.2f%% %10s" % ("%.0f SPS" % rate, noise,
                                                   100 * noise / step_gf,
                                                   "FAILED"))
    print()
