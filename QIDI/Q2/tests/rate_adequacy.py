"""Does the Max4's sample rate resolve pressure advance?

Synthesises load-cell force traces for a PA sweep with a KNOWN true K, samples
them at each CS1237 rate, and runs them through autopa's REAL analysis
(sweep_analysis.py, unmodified).  The error in recovered K tells us what a
measured sample rate on the printer actually buys us -- before any calibration
code is written.

Physics model (the standard PA model):
    melt pressure P obeys   tau*dP/dt + P = G*v_in
    pressure advance feeds  v_in = v_cmd + K*dv_cmd/dt
    so K == tau cancels the lag exactly -> a clean square response.
    K < tau  -> rounded, lagging edges.   K > tau -> overshoot spikes.
The load cell reads P, so recovering K from the step response is exactly what
the real instrument would be doing.

THIS SCRIPT NEEDS A FILE THAT IS NOT IN THIS REPOSITORY.
  It imports autopa's `sweep_analysis.py` (AGPL-3.0). That file is deliberately
  not vendored and is no longer fetched automatically - the helper that used to
  download it was removed on 2026-09-17 so that no copyleft code enters the tree
  at all. To re-run this, fetch it yourself:

      curl -fsSL https://raw.githubusercontent.com/G0BL1N/autopa/main/\
autopa/sweep_analysis.py -o tests/sweep_analysis.py

  The script is kept because it is the provenance of the sample-rate result -
  that result was cross-checked against an independent implementation rather
  than only against our own simulation, and deleting this would leave the
  finding unreproducible. Using a tool does not make its output derivative, so
  citing it costs nothing; the shipped modules import only the standard library.
"""
import math
import os
import sys

import numpy as np
from scipy.signal import lfilter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sweep_analysis as sa

A_FIL = math.pi * (1.75 / 2) ** 2      # mm^2

# --- sweep geometry, matching autopa's defaults ----------------------------
T_SLOW, T_FAST = 1.0, 0.25              # leg durations (s)
CYCLES = 8
K_GRID = np.round(np.arange(0.010, 0.0801, 0.005), 4)
RAMP_S = 0.04                           # accel-limited velocity ramp
SIM_DT = 1e-4                           # integration step

# Force model: P = G * v_in ** N  (shear thinning), expressed via a linear
# gain on an effective velocity so the ODE stays linear in P.
GAIN_GF_PER_MM_S = 110.0
BASE_GF = 250.0


def sim_segment(k, tau, slow_v, fast_v, t0, noise_rms, rng):
    """Integrate one K segment; return (t, force, rising_times, falling_times)."""
    cycle = T_SLOW + T_FAST
    total = CYCLES * cycle
    n = int(round(total / SIM_DT))
    t = t0 + np.arange(n) * SIM_DT
    phase = (np.arange(n) * SIM_DT) % cycle

    # Commanded velocity: square wave with accel-limited ramps, both ramps kept
    # inside the fast leg so nothing spills past the cycle boundary.
    r_start = T_SLOW                       # slow -> fast ramp begins
    r_end = T_SLOW + RAMP_S
    f_start = T_SLOW + T_FAST - RAMP_S     # fast -> slow ramp begins
    f_end = T_SLOW + T_FAST

    v_cmd = np.full(n, slow_v)
    up = (phase >= r_start) & (phase < r_end)
    v_cmd[up] = slow_v + (fast_v - slow_v) * (phase[up] - r_start) / RAMP_S
    plateau = (phase >= r_end) & (phase < f_start)
    v_cmd[plateau] = fast_v
    dn = (phase >= f_start) & (phase < f_end)
    v_cmd[dn] = fast_v - (fast_v - slow_v) * (phase[dn] - f_start) / RAMP_S

    rising = [t0 + c * cycle + r_start for c in range(CYCLES)]
    falling = [t0 + c * cycle + f_start for c in range(CYCLES)]

    # pressure advance adds K * dv/dt to the feed entering the melt
    dv = np.gradient(v_cmd, SIM_DT)
    v_in = v_cmd + k * dv

    # first-order melt: tau*dP/dt + P = G*v_in, as a one-pole IIR
    a = SIM_DT / tau
    p = lfilter([a * GAIN_GF_PER_MM_S], [1.0, -(1.0 - a)], v_in,
                zi=[(1.0 - a) * GAIN_GF_PER_MM_S * v_in[0]])[0]
    force = BASE_GF + p + rng.normal(0.0, noise_rms, n)
    return t, force, rising, falling


def run_sweep(tau, rate_hz, slow_q, fast_q, noise_rms, seed):
    """Full synthetic sweep at one sample rate -> K recovered by autopa."""
    rng = np.random.default_rng(seed)
    slow_v, fast_v = slow_q / A_FIL, fast_q / A_FIL
    ts, fs, windows, transitions = [], [], [], []
    t0 = 0.0
    for k in K_GRID:
        t, f, ris, fal = sim_segment(k, tau, slow_v, fast_v, t0,
                                     noise_rms, rng)
        # decimate the fine integration grid down to the sensor's rate
        step = max(1, int(round((1.0 / rate_hz) / SIM_DT)))
        ts.append(t[::step])
        fs.append(f[::step])
        windows.append((t0, t[-1]))
        transitions.append((ris, fal))
        t0 = t[-1] + 0.25
    t_all = np.concatenate(ts)
    f_all = np.concatenate(fs)
    res = sa.analyse_sweep_segments(
        t_all, f_all, list(K_GRID), windows, transitions,
        slow_v=slow_v, fast_v=fast_v,
        slow_half_s=T_SLOW, fast_half_s=T_FAST,
        cycle_period_s=T_SLOW + T_FAST)
    return res.bd_k_opt, len(t_all)


def main():
    tau = 0.035                      # the "true" PA we are trying to recover
    slow_q, fast_q = 2.0, 16.0       # mm^3/s, a realistic sweep pair
    trials = 5
    # CS1237 noise grows with rate (less internal averaging): ~sqrt(rate)
    base_noise = 1.0                 # gf RMS at 40 SPS

    print("Synthetic PA sweep -> autopa's real analysis")
    print("  true tau (K)   : %.4f" % tau)
    print("  flow legs      : %.1f -> %.1f mm3/s  (%.2f -> %.2f mm/s filament)"
          % (slow_q, fast_q, slow_q / A_FIL, fast_q / A_FIL))
    print("  legs           : %.2fs slow / %.2fs fast, %d cycles, ramp %.0f ms"
          % (T_SLOW, T_FAST, CYCLES, RAMP_S * 1000))
    print("  K grid         : %.3f..%.3f step %.3f (%d segments)"
          % (K_GRID[0], K_GRID[-1], K_GRID[1] - K_GRID[0], len(K_GRID)))
    print("  settling       : 3*tau = %.0f ms" % (3 * tau * 1000))
    print()
    hdr = ("  %-10s %9s %9s %9s %9s %9s" %
           ("rate", "smp/ramp", "smp/3tau", "noise gf", "mean K", "err %"))
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    verdicts = {}
    for rate in (40., 640., 1280.):
        noise = base_noise * math.sqrt(rate / 40.)
        ks = []
        for s in range(trials):
            k, _n = run_sweep(tau, rate, slow_q, fast_q, noise, 1234 + s)
            if k is not None and np.isfinite(k):
                ks.append(k)
        if not ks:
            print("  %-10s %9s %9s %9.2f %9s %9s"
                  % ("%.0f SPS" % rate, "-", "-", noise, "FAILED", "-"))
            verdicts[rate] = None
            continue
        mean_k = float(np.mean(ks))
        spread = float(np.std(ks))
        err = abs(mean_k - tau) / tau * 100.
        print("  %-10s %9.1f %9.1f %9.2f %9.4f %9.1f"
              % ("%.0f SPS" % rate, RAMP_S * rate, 3 * tau * rate, noise,
                 mean_k, err))
        verdicts[rate] = (mean_k, spread, err)

    print()
    print("  run-to-run spread (std of %d trials):" % trials)
    for rate, v in verdicts.items():
        if v:
            print("    %-10s +/- %.4f  (%.1f%% of true K)"
                  % ("%.0f SPS" % rate, v[1], v[1] / tau * 100))

    print()
    print("VERDICT")
    for rate, v in verdicts.items():
        label = "%.0f SPS" % rate
        if v is None:
            print("  %-10s analysis failed to return a K" % label)
            continue
        err = v[2]
        if err < 10:
            print("  %-10s USABLE      (%.1f%% error)" % (label, err))
        elif err < 25:
            print("  %-10s MARGINAL    (%.1f%% error)" % (label, err))
        else:
            print("  %-10s INADEQUATE  (%.1f%% error)" % (label, err))


if __name__ == '__main__':
    main()
