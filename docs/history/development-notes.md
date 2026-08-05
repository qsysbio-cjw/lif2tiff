# LIF2TIFF v0.3-alpha Development Notes

## Objective

Convert one Leica LIF into a safe, relocatable, non-duplicated, automatically
validated project directory without modifying source LIF files or the existing
`preprocessing/lif2tiff` implementation.

The CLI is an internal automation, testing, and agent surface. The final product
target is a shared-core desktop GUI for Linux and Windows; user-facing workflow
decisions should therefore be represented as reusable core data and functions,
not embedded only in terminal prompts.

## Isolation

During development, the new implementation lived in a separate sandbox. Its
reviewed source and tests are now published as `src/`, `qt_gui/`, `viewer/`,
and `tests/`. Generated smoke-test directories remain excluded from Git.

- development code: `src/`, `qt_gui/`, and `viewer/`
- tests: `tests/`
- selected non-sensitive evidence: `docs/testing/process-data/`
- source LIF files are opened read-only from their existing locations
- the existing Conda environment is used without package changes

## Baseline

- liffile: 2026.7.14
- tifffile: 2026.7.14
- numpy: 2.5.1
- existing core test suite: 12 tests passing before sandbox development
- locally available Candida original LIF smoke test: 2D
- public real Leica fixtures: time-lapse, Z-stack, and mosaic ZT
- metadata catalog records: 54 Z-stack, 90 time-lapse, and 2 ZT series, but
  their original LIF files were not locally connected at development start

Z-stack, time-lapse, and ZT will therefore require deterministic fake-image
tests before their original LIF files can be used for real pixel-level smoke
tests.

## Implemented alpha behavior

- `inspect`, `convert`, and `validate` CLI commands
- relocatable manifest with no source absolute path
- full LIF, Leica XML, and optional calibration SHA-256 fingerprints
- channel-first output directories
- original Leica series names used as filenames without synthetic index prefixes
- 2D grayscale TIFF and streamed Z/T/ZT grayscale OME-TIFF
- explicit `YX`, `ZYX`, `TYX`, and `TZYX` axes in the manifest and TIFF metadata
- millisecond timestamps in normalized metadata and uniform OME time increments
- deterministic expansion of Leica mosaic positions with source index tracking
- 8-bit and 16-bit TIFF support
- X/Y resolution and Z-spacing validation
- temporary TIFF writes and project-level `.partial` promotion
- resume after both simulated exceptions and an externally terminated process
- optional per-export-unit intensity statistics
- optional exact source-versus-TIFF pixel comparison
- calibrated interactive stage-map integration

The historical alpha was intentionally isolated from the earlier converter.

## v0.3 channel identity

- tolerant optical groups from the frozen acquisition audit are retained in a
  private, versioned companion registry rather than the public application;
- protocol matches are candidates and never proof of reagent presence;
- direct and optional interactive user input is stored separately as confirmed;
- `unknown` is a valid explicit decision;
- neutral physical channel directories remain unchanged;
- the manifest stores a compact summary and the metadata bundle stores complete
  per-series evidence.

## v0.3.2 workflow review contract

- optical consistency uses an explicit 0.5 nm min-to-max tolerance;
- the tolerance was selected from a 547-LIF 0.1/0.5/1.0 nm sensitivity audit;
- series are grouped into one most-frequent reference protocol and explicit
  variants without claiming that the reference is correct;
- review differences and informational setting variations are stored in
  `metadata/workflow_review.json` for the future shared-core GUI;
- explicit review acceptance records `continue_all`, accepted reason codes,
  and reviewed variant-group ids;
- the real 4-series review fixture passed 12/12 exact source-array comparisons.

## v0.3.3 conversion preflight

- `build_conversion_plan` inspects metadata without calling image-frame reads;
- the portable plan describes series, dimensions, channels, expected files,
  estimated uncompressed pixel payload, workflow review, and pending actions;
- source full-file hashing is deferred until conversion;
- `liftool plan` writes JSON only when an explicit new output path is supplied;
- real standard and review fixtures passed preflight acceptance.

## Linux GUI prototype

- the GUI reuses the same preflight, channel-resolution, review, conversion,
  resume, and validation contracts as the CLI;
- multiple files and recursively scanned folders enter a deduplicated input
  queue through native drag and drop, file/folder dialogs, or command-line paths;
- preview frames are read directly from the source LIF without creating TIFFs;
- single-channel LUT/grayscale display, automatic/manual contrast, Z/T browsing,
  BF-plus-fluorescence overlay, and full-resolution derived PNG export work;
- manual contrast uses one dual-handle range control plus exact Black/White
  numeric entry;
- real T planes can be stepped or played at 1/2/5/10 FPS, and the viewport
  supports cursor-centered zoom, drag-to-pan, and fit-to-view;
- Dry Run evaluates review, output collision, resume, and channel-confirmation
  state without reading pixels or creating an output;
- source files and quantitative grayscale outputs are never modified by display
  operations;
- conversion runs in a worker thread with progress reporting and safe
  output-unit-boundary cancellation;
- a real 3-channel Leica LIF passed single-channel and overlay visual acceptance.
- a real 9-LIF folder passed queue discovery and first-item preview acceptance;
- selected queue items run as a sequential batch; channel/review state is kept
  per LIF and Windows-compatible output collisions block the complete batch;
- complete converted projects can be validated and reopened through portable
  manifest-relative TIFF paths without the source LIF;
- presentation PNG batches are independent derived outputs with exact render
  recipes and never alter quantitative masters;
- mixed new/resume jobs and failure-continuation controls remain later batch
  hardening work.

## Remaining product direction

These are user needs, not yet frozen implementation decisions:

- add per-channel overlay visibility and opacity;
- add multi-FOV side-by-side comparison;
- extend presentation export only where real reporting workflows require it;
- add day-folder orchestration only after the one-LIF workflow is stable;
- package and acceptance-test the same shared core on Windows.

## Pending reader compatibility acceptance

- Keep `liffile 2026.7.14` as the intended unified pixel reader for now.
- Its public `LifFile` API has no permissive or `strict=False` mode; `squeeze`
  only controls removal of singleton dimensions.
- Re-test the older Leica files that previously triggered stride/dimension
  errors in an isolated output directory.
- Before adopting one reader for the whole workflow, require metadata parity,
  representative pixel hashes, and successful 2D/Z/T/ZT/mosaic conversion.
- If current `liffile` still rejects valid older files, decide explicitly
  between a narrowly tested compatibility patch and a documented metadata-only
  fallback. Do not silently reinterpret dimensions or strides.
