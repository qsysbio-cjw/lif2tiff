# LIF2TIFF Changelog And Test Ledger

Status: living development record
Last reviewed: 2026-08-05

## How To Use This File

This is the short chronological record for human review. It records what
changed, why it changed, what evidence was used, and what remains unverified.
Detailed machine outputs stay in `test_runs/`, `reports/`, and dated audit
directories.

## Current Baseline

- basic input unit: one Leica LIF;
- primary reader: `liffile 2026.7.14`;
- quantitative output: native-intensity grayscale TIFF/OME-TIFF;
- interaction layer: PySide6 Qt GUI on Linux;
- intended delivery: Linux and Windows desktop GUI with one shared core;
- frozen acquisition evidence: 547 LIFs through 2026-08-01;
- source-data policy: read-only;
- output policy: relocatable, relative-path project with no overwrite.

## Development History

### Isolated Conversion Core

Established one-LIF conversion, relative manifests, source/metadata hashes,
channel-first directories, atomic TIFF writes, `.partial` projects, resume,
validation, and optional exact source-pixel comparison. This work remained
isolated from the historical `preprocessing/lif2tiff` implementation.

### Multidimensional And Mosaic Support

Added deterministic 2D, Z, T, ZT, and TileScan handling. Z/T/ZT are streamed
into one OME-TIFF per series and channel with explicit axes and physical
metadata. Public Bio-Formats-checked fixtures and real Candida files closed the
earlier multidimensional evidence gap.

### Channel Identity And Workflow Review

Separated physical channel labels, protocol-derived candidates, confidence,
and user confirmation. Added standard/review routing and a portable review
payload. The frozen protocol evidence is project-specific and is not presented
as a universal spectral classifier.

### Metadata-Only Preflight

Added conversion planning without image-plane reads. Plans describe series,
dimensions, expected files, plane counts, estimated uncompressed bytes,
channel actions, and review actions without creating outputs.

### Qt Linux GUI

Added multi-file/folder queueing, native drag/drop, source-plane preview,
series and Z/T controls, playback, channel overlays, per-channel contrast,
touchpad/mouse navigation, stage-map preview, channel confirmation, multi-LIF
Dry Run, sequential selected-LIF batch conversion, progress, logs, resume, and
safe cancellation.

Display operations remain in memory and do not modify source or quantitative
output. The GUI requires every channel to be explicitly resolved before
conversion.

Converted projects now keep the Convert tab visible while clearly explaining
that conversion requires an original `.lif`. For original inputs, the disabled
Convert button reports its exact gate: metadata inspection, unresolved channel
identity, pending acquisition review, existing output, or another Dry Run
blocker. This avoids presenting intentional no-overwrite protection as a dead
button.

### 2026-08-03 Edge And Stress Audit

An isolated audit added Unicode/path discovery tests, malformed calibration and
stage-position tests, Dry Run state tests, long-text layout tests, frozen
resource checks, malformed LIF fixtures, and metadata stress inspection.

One real compatibility defect was fixed: a Leica derived image with no
acquisition timestamp returned scalar `None`, which the adapter incorrectly
passed to NumPy's datetime formatter. Missing timestamps now normalize to an
empty list; valid datetime handling is unchanged.

PyInstaller frozen-resource lookup was added for the GUI, viewer, channel
registries, and plate calibration. A Windows `onedir` build scaffold was
prepared, but no Windows binary has yet been accepted.

### 2026-08-03 Output Naming Decision

The 547-LIF frozen inventory was audited before changing the naming policy:

- 467 filenames contain spaces;
- `~` appears 296 times and `+` appears 50 times;
- the older strict rule changes 497 of 547 stems;
- no same-parent raw-name, case-insensitive, strict-rule, or proposed-rule
  collision was found;
- no Windows-invalid or reserved basename was found;
- the maximum LIF basename is 83 UTF-8 bytes;
- 18 stems end in spaces or periods before `.lif`.

The accepted policy preserves valid human-readable characters, removes only
trailing spaces/periods, replaces Windows-invalid/control characters, appends
`_tiff`, and blocks batch collisions instead of silently adding numeric
suffixes.

The policy is implemented in the shared output-name helper and Qt batch
preflight. Oversized output components are shortened deterministically with a
content-derived suffix while retaining `_tiff`; normal frozen names do not
trigger that fallback.

## Verification Ledger

### Earlier Core Acceptance

- 2D, Z, T, ZT, and mosaic conversion exercised;
- 8-bit and 16-bit sources exercised;
- 217 multidimensional public-fixture planes matched an independent
  Bio-Formats oracle;
- 63 real Candida TIFF/OME-TIFF outputs and 633 source planes matched exactly;
- a 19-FOV real LIF produced and source-verified 38 TIFF arrays;
- externally interrupted conversion resumed and completed without stale
  temporary artifacts;
- moved project validation and no-absolute-path checks passed.

See `TEST_REPORT.md` and
`CANDIDA_MULTIDIMENSIONAL_ACCEPTANCE_2026-08-02.md` for fixture-level details.

### 2026-08-03 Audit Baseline

- automated suite: 87 passed, 3 expected design-gap xfails;
- local GUI dataset: 54 LIFs, 1,480 series, 16,939 planes, all preflighted;
- NileRed set: 16 LIFs, 319 series, 957 planes, all preflighted;
- public heterogeneous set: 5 LIFs repeated 3 times after the timestamp fix,
  all 15 planning runs passed;
- file descriptors remained `4 -> 4` in stress runs;
- peak resident memory was approximately 127-150 MiB;
- one public ZT/mosaic fixture converted to 8 OME-TIFF files and validated;
- malformed input failed without creating a conversion project;
- generated project text contained no application-generated source absolute
  path.

Detailed evidence: `audit_sandbox_2026-08-03/AUDIT_REPORT.md` and its
`results/` directory.

### Output Naming Regression

Completed on 2026-08-03:

- preserve spaces, Unicode, parentheses, commas, `~`, and `+`;
- remove stem-ending spaces and periods;
- replace Windows-invalid and control characters;
- deterministic empty/reserved edge behavior;
- detect case-insensitive same-parent target collisions;
- permit identical basenames in different parent directories;
- block collision in multi-LIF Dry Run and conversion gate;
- preserve refusal to overwrite complete output;
- rerun the full Qt/core/audit suite.

Result: **96 passed, 2 expected design-gap xfails**. The 547-name frozen audit
cleaned only the 18 stem-ending space/period cases and found zero proposed
output collisions. An offscreen Qt integration test confirmed that
`Sample.lif` and `sample.lif` are both marked blocked and that Convert remains
disabled. `A B.lif` and `A_B.lif`, which collided under the old strict rule,
now retain distinct readable output names.

### 2026-08-03 GUI Acceptance Corrections

Manual naming acceptance identified three presentation/entry-point issues:

- collision text now lists each source and its displayed target and explicitly
  states that Windows-compatible comparison caused the block;
- the Tools dock still starts near 305 px but no longer has a 320 px maximum,
  so users can resize it to use a large or full-screen display;
- the top-level `run_gui.sh` now launches the current Qt workbench. The
  historical Tk prototype remains available as `run_legacy_tk_gui.sh`.

The complete regression result remains **96 passed, 2 expected xfails**. An
offscreen Qt test programmatically expands the Tools dock beyond 500 px.

Manual Qt acceptance then confirmed both behaviors on the live desktop:

- `sample.lif` and `Sample.lif` were shown with their distinct Linux target
  spellings and blocked under the documented Windows-compatible comparison;
- the Tools dock could be dragged wider than its compact default on a
  full-screen window.

### 2026-08-03 Output Storage Preflight

Dry Run now finds the nearest existing destination parent without creating any
directory, checks write/traverse permission, and reads filesystem capacity.
Each job reserves its uncompressed pixel estimate plus `max(64 MiB, 5%)` for
metadata and temporary-write overhead. Selected jobs targeting the same
filesystem are also checked as one batch. Conversion repeats the per-job check
before creating its `.partial` directory. Resume remains deliberately
conservative: existing partial files are not subtracted from the estimate.

Deterministic tests cover an unwritable parent, a missing nested destination,
insufficient free space, aggregate batch overcommit, and core conversion
blocking before any output is created. Result: **101 passed, 1 expected xfail**.

### 2026-08-03 Converted Project And Presentation Layer

Added a thin, read-only adapter for complete `_tiff` projects. The Qt GUI now
validates and reopens a movable project without its source LIF and reuses the
existing series, Z/T, channel, contrast, overlay, and stage-map viewer.

Added an independent batch presentation renderer. It exports selected channel
combinations for the current frame, every series first plane, or all Z/T planes
using current, Leica-source, or full-range contrast. Each new output contains
RGB PNGs plus a portable recipe recording the exact per-image render settings;
quantitative TIFFs and manifests remain unchanged.

Synthetic tests cover page ordering, unsafe relative paths, incomplete
projects, exact contrast recipes, missing channels, and overwrite refusal. A
real 3-series converted project reopened in offscreen Qt and produced three
green/red overlays with no absolute paths in the recipe. Current result:
**106 passed, 1 expected xfail**.

Manual acceptance then exposed a converted-project playback defect: the timer
could advance faster than background TIFF reads, causing every completed frame
to be discarded as stale until playback stopped. Frame requests now coalesce to
the latest target, playback applies backpressure while one frame is loading,
and converted TIFF handles are reused lazily and closed with the project. A
real 1,391-frame series displayed 39 consecutive completed frames in a 2-second
offscreen playback run. Timeline scrubbing now displays completed intermediate
samples while retaining only the latest pending target, so it remains useful
for visual exploration without creating an unbounded read queue.

A 12-second real-project playback audit stabilized at 155.2-155.4 MiB RSS after
the initial display/decode allocation, displayed 267 frames, and returned to
near-zero CPU use after pause. A project-level LRU now limits lazy TIFF handles
to 12, preventing resource growth when many series are browsed; all handles are
released when the project closes. A real playback recheck displayed 40 frames
in 2 seconds with six TIFFs open, then returned to zero after close. Current
result: **109 passed, 1 expected xfail**.

### 2026-08-05 Frame Navigation And Export Destination

Large time series now expose a one-based editable `Frame N / total` control.
Keyboard tracking is disabled so typing `665` requests frame 665 once rather
than loading partial entries. A real 1,391-frame project resolved input 665 to
internal index 664 and displayed `T 665/1391` consistently.

Presentation export now shows an editable existing parent, new folder name, and
resolved destination in one dialog. The default is a sibling of the converted
project rather than a folder inside the quantitative project. Nested parents
are supported and covered by both GUI and renderer tests. Current result:
**111 passed, 1 expected xfail**.

The platform spin-box arrows were visually ambiguous under the active Qt theme.
All numeric fields now hide the unclear embedded button strip. Frame navigation
uses the editable one-based frame field for exact jumps and the timeline for
coarse movement; FPS, Black/White, and Zoom retain exact entry and keyboard
stepping. A real T=665 screenshot passed layout review.

### 2026-08-05 Plane Progress And Responsive Cancellation

Conversion progress now follows image planes rather than only completed TIFF
containers. GUI updates are limited to approximately 100 per conversion, while
the non-blocking cancellation check runs before each plane and each 8 MiB source
fingerprint block. Non-progress log messages no longer reset the progress bar.

Cancellation inside a multi-plane OME-TIFF removes the incomplete temporary
file, retains completed TIFF units, and resumes by rewriting only the interrupted
TIFF unit. A real 4,179-plane LIF cancelled at plane 42 in 10.8 seconds: three
completed 2D TIFFs remained, no temporary TIFF remained, and the manifest was
`interrupted`. The same 126-plane real conversion took 4.47 seconds after the
change, with no measurable slowdown relative to the earlier approximately
4-second run, and all 126 planes matched the source exactly. Current result:
**116 passed, 1 expected xfail**.

The GUI subsequently resumed a user-cancelled real conversion with five of
nine TIFF units already complete. It finished the remaining work in about 12
seconds; all 4,179 planes matched the source exactly. Selecting resume mode now
renames the primary action from `Convert` to `Resume`.

### 2026-08-05 Multidimensional Linux Acceptance

Public acceptance fixtures passed machine and GUI review for Z-stack and ZT
navigation. The public ZT fixture is a 64 x 64 Leica `SIMULATOR SP` frame-order
test and is retained as technical evidence rather than biological visual
evidence.

A real Candida acquisition was then copied read-only from the external archive
and verified against the copy by SHA-256. It contains six 2D series and one
1024 x 1024 ZT series with Z=10, T=13, and BODIPY, brightfield, and FM4-64
channels. Source preview and converted-project Z/T navigation passed manual
review. Conversion produced 21 TIFF units; all 408 planes matched the copied
source exactly with no errors or warnings.

## Known Limitations

The following are recorded gaps, not completed features:

- Resume is one batch-wide switch, so new and partial jobs cannot yet be mixed;
- after a mid-batch failure, jobs that never started need a clearer `Not run`
  state and retry workflow;
- source replacement between preflight and conversion is not yet guarded by a
  pre-start metadata snapshot comparison;
- stale metadata workers are ignored by token but not actively cancelled;
- multi-FOV comparison remains open;
- the Windows package has not been built or tested on a clean Windows system.

## Documentation Sources

This ledger consolidates the existing project README, development notes,
manifest and channel-identity contracts, test reports, dated audit results,
and the relevant local Codex session history. Tentative discussion was excluded
unless it was implemented or explicitly accepted by the user.
