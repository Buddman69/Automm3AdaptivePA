"""Does the recovered K actually TRACK the true K, at each sample rate?

A single test point can be right by luck. What matters for calibration is
whether recovered K moves with true K -- slope near 1, tight fit, and ordering
preserved. A rate that returns a biased but linear answer is still usable
(the bias can be corrected); one that returns a flat or scrambled response is
not, however close it lands on one lucky point.
"""
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from rate_adequacy import run_sweep, A_FIL

TRUE_KS = [0.015, 0.025, 0.035, 0.045, 0.060, 0.075]
RATES = [40., 640., 1280.]
TRIALS = 3
BASE_NOISE = 1.0


def main():
    print("Recovered K vs true K, %d trials each" % TRIALS)
    print("  true K sits between grid points as often as on them\n")
    results = {}
    for rate in RATES:
        noise = BASE_NOISE * math.sqrt(rate / 40.)
        rec, tru = [], []
        row = []
        for tk in TRUE_KS:
            ks = []
            for s in range(TRIALS):
                k, _ = run_sweep(tk, rate, 2.0, 16.0, noise, 909 + s)
                if k is not None and np.isfinite(k):
                    ks.append(k)
            if ks:
                m = float(np.mean(ks))
                rec.append(m); tru.append(tk)
                row.append("%.4f" % m)
            else:
                row.append("  --  ")
        results[rate] = (np.array(tru), np.array(rec))
        print("  %-9s " % ("%.0f SPS" % rate) + "  ".join(row))
    print("  %-9s " % "true K" + "  ".join("%.4f" % k for k in TRUE_KS))

    print("\n  fit of recovered vs true (want slope ~1.00, R^2 ~1.00):")
    print("  %-10s %8s %10s %8s %10s" % ("rate", "slope", "intercept",
                                         "R^2", "monotonic"))
    print("  " + "-" * 50)
    verdict = {}
    for rate in RATES:
        tru, rec = results[rate]
        if len(tru) < 3:
            print("  %-10s   insufficient data" % ("%.0f SPS" % rate))
            continue
        slope, intercept = np.polyfit(tru, rec, 1)
        pred = slope * tru + intercept
        ss_res = float(np.sum((rec - pred) ** 2))
        ss_tot = float(np.sum((rec - np.mean(rec)) ** 2))
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.
        mono = bool(np.all(np.diff(rec) > 0))
        print("  %-10s %8.3f %10.4f %8.4f %10s"
              % ("%.0f SPS" % rate, slope, intercept, r2,
                 "yes" if mono else "NO"))
        verdict[rate] = (slope, r2, mono)

    print("\nVERDICT")
    for rate in RATES:
        if rate not in verdict:
            continue
        slope, r2, mono = verdict[rate]
        label = "%.0f SPS" % rate
        if not mono:
            print("  %-10s UNUSABLE  - ordering not preserved; cannot "
                  "distinguish K values" % label)
        elif r2 > 0.97 and 0.85 <= slope <= 1.15:
            print("  %-10s GOOD      - tracks true K directly" % label)
        elif r2 > 0.97:
            print("  %-10s USABLE    - linear but slope %.2f, needs a "
                  "correction factor" % (label, slope))
        elif r2 > 0.85:
            print("  %-10s MARGINAL  - noisy relationship (R2=%.3f)"
                  % (label, r2))
        else:
            print("  %-10s INADEQUATE - recovered K barely tracks true K "
                  "(R2=%.3f)" % (label, r2))


if __name__ == '__main__':
    main()
