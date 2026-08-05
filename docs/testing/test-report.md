# LIF2TIFF v0.3-alpha Test Report

## Automated tests

Result: **54 passed, 0 failed**.

Coverage includes:

- inherited channel, Leica sequential metadata, catalog, and plate tests;
- channel-first 2D, Z-stack, time-lapse, and ZT output layouts;
- one streamed OME-TIFF per series and channel for Z/T/ZT;
- preservation of original Leica series names in filenames;
- liffile mosaic expansion and a Bio-Formats-verified plane hash;
- 8-bit and 16-bit TIFF;
- X/Y physical resolution and Z spacing;
- safe filenames and distinct numbered-LIF defaults;
- refusal to overwrite complete projects;
- resume after partial conversion;
- project relocation;
- optional intensity summary;
- undeclared or duplicated TIFF detection;
- calibration and source-XML tamper detection.
- transmitted-light classification from detector metadata rather than LUT;
- Gray-LUT fluorescence classification when active excitation is present;
- separation of BODIPY and PI filename hints by optical compatibility;
- protection against assigning filename stains to brightfield;
- explicit separation of instrument presets, candidates, and user confirmation;
- protocol warnings for channel-count, detector/modality, and optical changes;
- gain, offset, and laser-power changes retained as non-blocking variation.
- frozen protocol matches retained as candidates rather than confirmations;
- dye aliases and direct CLI input canonicalized to stable registry ids;
- explicit `unknown` and interactive candidate/brightfield selection;
- portable manifest and metadata channel-resolution records.
- standard versus review workflow routing;
- blocking before output creation for unacknowledged review cases;
- explicit review acknowledgement retained in the portable manifest.
- explicit 0.5 nm wavelength tolerance and boundary behavior;
- reference/variant protocol grouping with gain kept informational;
- portable `workflow_review.json` and explicit `continue_all` decision.
- review routing for equal channel counts with different channel-index layouts.
- metadata-only conversion planning with zero pixel-frame reads;
- portable expected-output paths and correct 2D/Z/T/ZT plane/byte estimates.
- safe callback-driven cancellation with a resumable interrupted project;
- single-channel contrast/LUT and BF-plus-fluorescence overlay rendering.
- recursive case-insensitive LIF discovery, path-space handling, and deduplication.
- Dry Run readiness/blocking checks with proof that no output is created.

## Linux GUI acceptance

The Tk desktop workbench passed a headless startup smoke test. A real Leica
`1. TEST.lif` was then opened through the GUI; metadata preflight completed and
real source pixels were rendered in both single-channel BODIPY and
BF-plus-fluorescence overlay modes. The workbench remained nonblank, correctly
framed, and exposed the full series/channel/Z/T controls at 1440 x 880.

Acceptance screenshots:

- `test_runs/gui_acceptance_v02/linux_gui_standard.png`
- `test_runs/gui_acceptance_v03/linux_gui_overlay.png`
- `test_runs/gui_acceptance_v04/linux_gui_overlay_final.png` (final regression)
- `test_runs/gui_acceptance_v05/linux_gui_folder_queue.png` (9-LIF folder queue)

The folder-queue acceptance supplied one real day folder rather than individual
files. The GUI found all 9 top-level LIFs, marked one preflighted file `Ready`,
kept the other 8 `Queued`, and rendered the first real source preview without
starting a conversion. The native `tkdnd` Tcl extension is also required by the
GUI startup smoke test.

A real public FRAP fixture then passed time-lapse viewer acceptance:

- 8 time-lapse series, T=5 for the selected series;
- playback loaded the next real source plane and advanced the control to T=1;
- cursor-centered viewport zoom rendered at 200%;
- Black/White numeric inputs, the dual-handle range, Play/FPS, Fit, Dry Run,
  Convert, and Cancel remained visible at 1280 x 880;
- a real-plan Dry Run reported 8 TIFF units and 40 planes and created no output.

Final time-lapse acceptance screenshot:

- `test_runs/gui_acceptance_v08/linux_gui_timelapse_zoom_final.png`

Conversion cancellation was exercised at the shared-core level: the current
TIFF unit completed, the project was marked interrupted, and the `.partial`
directory remained resumable. Closing the GUI during conversion now waits for
that same safe boundary before destroying the window.

## Conversion-plan acceptance

Two real LIFs were inspected through `liftool plan` without TIFF conversion:

- standard sample: 3 two-dimensional series, 9 TIFF units, 9 planes;
- review sample: 4 two-dimensional series, 12 TIFF units, 12 planes;
- both plans retained workflow/channel evidence and relative expected paths;
- only two JSON files were created (approximately 22 KB and 39 KB);
- neither plan contains an application-generated absolute path.

Acceptance output:

`test_runs/preflight_acceptance_v01/`

## Standard/review acceptance

The frozen 547-LIF snapshot routes 501 files (91.6%) to standard and 46 (8.4%)
to review. A real optical-protocol-variation LIF was refused before either a
complete or partial output was created. After explicit acknowledgement, all 12
TIFF arrays matched the source exactly and the acknowledgement was recorded in
the manifest.

The v0.3.2 review-payload acceptance reran the same 4-series LIF. It produced
one 2-series reference group and two 1-series variant groups, retained the
exact optical differences and acquisition-setting distributions, and recorded
the explicit `continue_all` decision. Exact source comparison passed 12/12
arrays and no application-generated absolute path was found.

Acceptance output:

`test_runs/acceptance_review_payload_v034/`

The 547-LIF tolerance audit compared 0.1, 0.5, and 1.0 nm. The selected 0.5 nm
rule preserved 501 standard and 46 review files; 0.1 nm added one review and
1.0 nm removed one review.

## Real LIF acceptance test

Source: `1. TEST.lif`

- source size: 9,895,614 bytes
- 3 real Leica series
- 3 channels per series
- 9 TIFF files
- output size: approximately 3.0 MB
- BODIPY and brightfield user overrides recorded
- PI assignment retained from Leica metadata for C2
- confirmed 384-well calibration copied and fingerprinted
- intensity summary generated
- no application-generated absolute path detected
- structural validation: passed
- exact source-pixel comparison: 9/9 arrays identical
- source LIF SHA-256 before and after conversion: unchanged

Acceptance output, renamed after conversion to verify relocation:

`test_runs/acceptance_real_2d_relocated/`

## v0.3 channel-identity acceptance

`1. TEST.lif` was converted again in the isolated v0.3 output with explicit
BODIPY, brightfield, and PI confirmations:

- manifest schema: 0.3;
- 3 series and 9 TIFF files;
- all three channel summaries and all per-series channel records are confirmed;
- `metadata/channel_resolution.json` is present and declared;
- no application-generated absolute path is present in the manifest;
- structural validation and exact source-pixel comparison: 9/9 arrays passed.

Acceptance output:

`test_runs/v0_3_channel_identity_real_test/`

An additional inferred-only conversion verified that no candidate becomes a
confirmation when the user supplies no answer. Per-series identity records were
then compacted while complete evidence remained in
`metadata/channel_resolution.json`; exact source comparison again passed 9/9.
The real terminal prompt accepted numbered candidates and typed aliases, while
non-interactive `--ask-channel-roles` exited with code 2 instead of waiting.

## Additional real-data tests

`0. one-well.lif`:

- 19 FOVs, 2 channels, 38 TIFF files
- output directories: `C00_green` and `C01_brightfield`
- filenames preserved as `Image001.tif` through `Image019.tif`
- calibrated stage map loaded all 384 wells
- all FOVs mapped to L15 as expected
- exact source-pixel comparison: 38/38 arrays identical

The v0.2 channel-first smoke-test output is:

`test_runs/v0_2_real_one_well/`

External process termination test:

- conversion was terminated while writing a TIFF;
- the partial project contained 13 complete TIFFs and one 8-byte temporary TIFF;
- `--resume` reused valid TIFFs, replaced the incomplete temporary file, and
  completed the remaining output;
- final exact source-pixel comparison: 38/38 arrays identical;
- no `.tmp` or `.partial` artifact remained.

## Isolation verification

- source LIF hashes remained unchanged;
- SHA-256 hashes of the existing `preprocessing/lif2tiff` core files remained
  equal to the recorded pre-development baseline;
- no Conda package was installed, removed, or upgraded;
- all generated outputs are under this sandbox.

## Real multidimensional fixtures

Public Leica LIF fixtures previously accepted against Bio-Formats were
converted with the new channel-first output:

- time-lapse: 8 OME-TIFF files and 40 source-verified planes;
- Z-stack/mixed 2D: 14 TIFF/OME-TIFF files and 129 source-verified planes;
- mosaic ZT: 4 mosaic positions, 8 OME-TIFF files, and 48 source-verified
  planes;
- all 217 exported T/Z/ZT plane hashes also matched the independent
  Bio-Formats 8.5.0 oracle.

The time-lapse fixture preserved millisecond timestamps in `metadata.json` and
wrote the uniform 1.627-second interval as OME `TimeIncrement`.

## Channel identity catalog audit

The read-only resolver was evaluated against the extracted metadata from 489
LIF files (34,516 channel rows):

- stable: 286;
- acquisition-value variation only: 171;
- protocol warning: 32;
- compatible with the standard path without a warning: 457/489 (93.5%).

The audit report contains relative catalog record names and no source-machine
absolute paths:

`reports/channel_identity_audit_v02/audit.md`

## Remaining evidence gap

The earlier project-specific Z/T/ZT evidence gap is now closed by four real
Candida LIF files: 63 TIFF/OME-TIFF files and 633 source-verified planes all
passed. See `CANDIDA_MULTIDIMENSIONAL_ACCEPTANCE_2026-08-02.md`.

The remaining acceptance work concerns product workflows and exceptional
acquisitions rather than ordinary 2D/Z/T/ZT pixel conversion.
