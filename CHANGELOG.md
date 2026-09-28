# Changelog

## 3.5.0 — development

- Add optional Micro-Manager camera connections alongside native IC4, using
  BURST's setup, discovery, profile and isolated-helper approach. Add capability-
  driven camera controls, verified external arming and restoration of preview.
- Preserve Micro-Manager monochrome 8/16-bit pixels and RGB8 in TIFF recordings;
  retain adapter metadata without treating software counters as hardware IDs.
- Keep PRIM's pressure/pump protocol and CSV/TIFF associations. No firmware
  changes or automatic fallback to software synchronization.
- Include the BURST-style interface, recording completion reports, per-run plot
  reset/retention, preview-rate recovery and camera-rate rounding corrections
  prepared during the PRIM computer audit.

Micro-Manager hardware acceptance remains specific to each camera/adapter. The
implementation was validated with simulated cameras and helper-process tests;
no additional camera was available on this computer.
