# ADR-002: Rely on hardware DNE, drop software arousal calibration

- **Status:** Accepted
- **Date:** 2026-08-13 (decision made earlier in project history; recorded here)
- **Context:** Earlier versions computed a software "arousal score" calibration on
  top of the raw EDA stream. The ring's firmware provides a hardware DNE
  (Digital Neuro-Emotional) stress index on the live stream (`d306262b`), which is
  preprocessed onboard.
- **Decision:** Drop the software arousal calibration and rely on the hardware DNE
  value as the stress signal. The `--filter` path (median + Butterworth lowpass on
  conductance) remains available but is off by default (`apply_filter=False`).
- **Consequences:**
  - `arousal_score` in `RingDeviceState` mirrors the hardware DNE index; no
    secondary calibration math to maintain or drift.
  - CSV "computed" rows carry DNE-derived values; the `MM_Arousal_Score` column is
    the hardware index.
- **Alternatives considered:** Keep the software calibration (rejected: duplicated
  signal processing with no clear accuracy gain over the onboard index).
