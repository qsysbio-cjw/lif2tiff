# LIF2TIFF Manual Acceptance Checklist

Status: active manual review
Last reviewed: 2026-08-05

This checklist separates automated correctness from visual and interaction
acceptance. Do not repeat a completed heavy export unless a relevant code path
changes.

## Current Automated Baseline

- [x] Full suite: 116 passed, 1 expected xfail.
- [x] Core cancellation inside a real 4,179-plane acquisition retained three
  completed TIFFs, removed the interrupted temporary OME-TIFF, and produced a
  resumable `interrupted` manifest.
- [x] GUI cancellation and resume completed a real 4,179-plane acquisition;
  all 9 TIFFs and all 4,179 source planes passed exact validation.
- [x] Converted-project page order, relative-path safety, handle cleanup, and
  12-handle LRU limit.
- [x] Real 1,391-frame playback, bounded scheduling, intermediate scrub samples,
  stable memory, and near-zero paused CPU.
- [x] Presentation file count, dimensions, RGB format, exact render recipe, and
  no generated absolute path.

The expected xfail is the unsupported combination of new jobs and resumed
`.partial` jobs in one batch. The GUI blocks this case rather than risking data.

## Existing Manual Or File Evidence

- [x] Converted `_tiff` project opens without the source LIF.
- [x] Windows-compatible output collision is shown and blocks conversion.
- [x] Tools dock can be resized beyond its compact default.
- [x] A complete `all_planes` presentation export exists for `4_tiff`:
  1,393 RGB PNGs, channels C00+C02, source display contrast, approximately
  1.8 GiB, with a portable recipe.
- [x] A `Current frame` export exists for T=665: one 1024 x 1024 RGB PNG,
  channels C00+C01+C02, current-adjusted contrast, and a matching portable
  recipe.
- [ ] Visual scientific quality of that overlay export has not been recorded as
  accepted.

## Verify Now

- [x] **Convert gate feedback:** in an already converted project, open the
  Convert tab and confirm it explains that an original `.lif` is required. For
  an original LIF whose complete `_tiff` output already exists, confirm the
  status explicitly reports the existing-output block; choosing a fresh output
  must enable Convert after channel confirmation and review acknowledgement.

- [ ] **Playback after the final fix:** open `4_tiff`, select `002 Series001`,
  play at 10, 30, and 60 FPS, then pause. Image, time controls, and the
  `Displayed T` footer must remain coherent.
- [ ] **Timeline scrubbing:** slow drag should show many intermediate frames;
  fast drag may sample intermediate frames but must finish on the released
  target without continuing an old backlog.
- [ ] **Display controls after reopening:** toggle C00/C01/C02, LUT, per-channel
  contrast, sync across series, and reset. No action may modify master TIFFs.
- [ ] **Small presentation comparison:** export only `Current frame` or
  `All series, first Z/T plane` once with `Current adjusted display` and once
  with `Leica source display`; compare both with the on-screen view.

## Verify Before Linux Beta

- [ ] Add multiple LIFs and a folder by drag/drop; remove selected and clear all.
- [ ] Confirm channel suggestions, explicit `unknown`, and review acknowledgement.
- [ ] Run batch Dry Run and inspect storage, collision, and blocked-job messages.
- [ ] Convert one small standard LIF, validate it, reopen it, and export an overlay.
- [x] In the GUI, cancel and resume a multi-plane conversion. Keep new and
  resumed jobs in separate batches until the known xfail is implemented.
- [x] Visually review one 2D, one Z-stack, one time-lapse, and one ZT acquisition.
  The ZT acceptance used a real 1024 x 1024, Z=10, T=13, three-channel Candida
  acquisition; all 408 planes matched its source LIF exactly after conversion.
- [ ] Review Stage Map for one and multiple LIFs in Acquisition and 384-well views.

## Deferred Release Acceptance

- [ ] Native Windows build and clean-machine installation.
- [ ] Windows file/folder drag/drop, Unicode paths, long names, playback, export,
  conversion, cancellation, resume, moved-project reopening, and SmartScreen.
- [ ] Multi-FOV comparison and downstream segmentation-result visualization after
  their product designs are implemented.
