# Candida Multidimensional LIF Acceptance

Date: 2026-08-02

## Conclusion

Four real project LIF files covering time-lapse, Z-stack, mixed 2D/T/Z, and ZT
were converted with the v0.3-alpha inferred-only workflow. All 63 TIFF/OME-TIFF
files and all 633 exported planes matched the source LIF pixels exactly.

The source files were read from a filesystem mounted read-only. All generated
outputs were written under the local sandbox `test_runs/` directory.

## Results

| Case | Source structure | Output files | Verified planes | Result |
|---|---:|---:|---:|---|
| `acceptance_candida_timelapse` | 1 time-lapse | 3 | 51 | Pass |
| `acceptance_candida_zstack` | 6 2D + 1 Z-stack | 21 | 81 | Pass |
| `acceptance_candida_mixed_t_z` | 4 2D + 1 time-lapse + 1 Z-stack | 18 | 93 | Pass |
| `acceptance_candida_zt` | 6 2D + 1 ZT | 21 | 408 | Pass |

## Representative multidimensional records

- Pure time-lapse: `TYX`, T=17, 120.306375 s interval.
- Z-stack: `ZYX`, Z=21, 0.258740 um Z spacing.
- Mixed acquisition: T=7 at 120.360167 s plus Z=20 at 0.418550 um.
- ZT: `TZYX`, T=13, Z=10, 300.813333 s interval, 0.772415 um Z spacing.

All records preserved 1024 x 1024 source planes, physical X/Y pixel sizes, the
expected grayscale integer data, and channel-first output organization.

## Safety and portability audit

- source signatures still matched during post-conversion validation;
- no conversion error or warning was reported;
- no temporary or partial artifact remained;
- no application-generated absolute path was found in JSON, CSV, Markdown, or
  conversion logs;
- inferred dye candidates remained unconfirmed;
- one acquisition had gain/laser-power variation, correctly retained as a
  non-blocking variation rather than a protocol warning.

This closes the project-specific real-data evidence gap for ordinary Candida
2D, Z, T, and ZT acquisitions. Public Leica fixtures continue to provide the
independent Bio-Formats-verified mosaic and combined-frame-order evidence.
