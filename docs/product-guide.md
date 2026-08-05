# LIF2TIFF Product Guide

Status: active alpha development
Last reviewed: 2026-08-03

## Purpose

LIF2TIFF turns Leica LIF acquisitions into safe, relocatable image projects.
It is intended for laboratory users who need both:

- quantitative grayscale TIFF/OME-TIFF masters for downstream analysis; and
- fast human inspection, comparison, presentation, and metadata review.

The command-line interface is the automation and testing surface. The intended
delivery is the same shared core behind desktop GUIs for Linux and Windows.
Segmentation and single-cell analysis are downstream workflows, not part of
the LIF conversion core.

## Confirmed Product Principles

These principles were recovered from the development history and confirmed in
the project discussions:

1. One LIF is the basic input and provenance unit.
2. Original LIF files are read-only and are never edited in place.
3. Quantitative masters retain native grayscale intensities. LUT color,
   contrast, overlays, and presentation exports are derived views.
4. Inferred channel identity and user-confirmed identity are separate facts.
   A matching acquisition protocol is not proof that a reagent was used.
5. Routine acquisitions use a standard path; unusual acquisition layouts use
   a review path instead of silent reinterpretation.
6. Generated metadata and manifests use relative paths and remain movable.
7. Human-readable outputs and machine-readable outputs are both first-class.
8. The normal workflow should not require an AI agent or network access.

## Workflow

```text
Add LIF files/folders
        |
        v
Metadata preflight and series discovery
        |
        v
Preview image planes and stage positions
        |
        v
Confirm every channel and acknowledge review cases
        |
        v
Batch Dry Run
  - verify destination writability and free space
  - aggregate storage demand per filesystem
        |
        v
Sequential conversion to sibling project directories
        |
        v
Structural validation and optional source-pixel validation
        |
        v
Reopen project for review and derived presentation export
```

The GUI accepts multiple files and recursively scans dropped folders. Each LIF
remains an independent job even when several jobs are selected and run as one
batch.

## Supported Acquisition Shapes

The shared reader and converter support:

- ordinary two-dimensional series;
- Z-stacks;
- time-lapse series;
- combined ZT series; and
- Leica mosaic/TileScan positions expanded deterministically into FOV records.

One series produces one grayscale file per physical channel. Plain 2D data use
TIFF. Z, T, and ZT data use OME-TIFF with explicit `ZYX`, `TYX`, or `TZYX`
axes. Pixel values remain 8-bit or 16-bit according to the source.

## Output Location And Naming

By default, each project is created beside its source LIF:

```text
experiment/
|-- sample.lif
`-- sample_tiff/
```

The output directory preserves the LIF stem as far as the target filesystems
allow. Spaces, Chinese text, parentheses, commas, `~`, `+`, and similar valid
characters are retained. Only control characters and characters forbidden on
Windows are replaced. Trailing spaces and periods in the stem are removed
before `_tiff` is appended.

Batch Dry Run compares all final output paths using Windows-compatible
case-insensitive semantics. A collision blocks the complete batch and lists
the conflicting source files. The application does not silently add `_2` or
`_3` suffixes.

Internal TIFF names remain conservative and deterministic. Scientific source
names and identity evidence are retained in the manifest and metadata tables
rather than depending only on filenames.

## Project Contents

```text
PROJECT/
|-- manifest.json
|-- C00_<physical-label>/
|   `-- TIFF or OME-TIFF masters
|-- C01_<physical-label>/
|-- metadata/
|   |-- metadata.json
|   |-- series_metadata.csv
|   |-- channel_metadata.csv
|   |-- channel_resolution.json
|   |-- workflow_review.json
|   |-- stage_positions.csv
|   |-- files.csv
|   |-- source_metadata.xml
|   `-- metadata_report.md
|-- views/
|   `-- stage_map.html
`-- logs/
    `-- conversion.log
```

Optional calibration and intensity-summary files are added only when the user
requests those functions. The default project does not duplicate the image
tree as color PNGs or LUT TIFFs.

## Image Viewer

The Qt viewer reads source planes directly without first converting the LIF.
It provides series selection, independent channel visibility, BF/fluorescence
overlays, Z/T navigation, time-lapse playback, pan, fit, zoom percentage, and
touchpad/mouse zoom.

Each physical channel has its own contrast state. Leica Black/White/Gamma
settings can be restored, full acquired range can be selected, and manual
Black/White values can be entered exactly. Display settings do not modify the
source LIF or quantitative output. `Sync across series` propagates an adjusted
channel only to the same channel number in other series of the same LIF.

A complete converted project can be reopened without its original LIF. The
application validates the project and reads TIFF planes only through relative
manifest paths, preserving the same series, channel, Z/T, contrast, overlay,
and stage-map interactions.

## Presentation Export

The `Export` workspace is separate from quantitative conversion. It can export
the current frame, the first Z/T plane of every series, or every plane. Any
available channel combination can be selected. Contrast can use the current
manual display, the Leica source display, or the full acquired range; LUT color
is optional.

Every run creates a new `<source>_presentation_<timestamp>` directory containing
full-resolution RGB PNGs and `presentation_recipe.json`. The recipe records the
selected scope and exact per-image channel Black/White/Gamma/LUT settings using
portable relative paths. It does not modify the LIF, grayscale TIFF masters, or
project manifest. `All Z/T planes` can create many PNGs and therefore remains
an explicit choice rather than the default.

The GUI shows the existing output parent, editable new-folder name, and exact
resolved destination before export. Converted projects default to a sibling
presentation folder, while users may browse to any existing nested parent.

## Stage Map

Stage metadata can be previewed before conversion for one or multiple selected
LIFs. FOV rectangles use physical pixel dimensions. Multiple LIFs use distinct
colors. The confirmed Cellvis 384-well calibration is an optional overlay;
raw acquisition coordinates remain available without assuming a plate.

The map is intended primarily for rapid human orientation and record recovery,
not for inferring experimental conditions that were not recorded.

## Channel Identity And Review

Every channel must be accepted, manually assigned, or explicitly marked
`unknown` before GUI conversion. Candidate dyes are inferred from frozen
project acquisition evidence through 2026-08-01, but confirmation remains a
user action.

The standard route covers ordinary acquisition variation. Changes in channel
layout, detector/modality mapping, excitation/emission configuration, or
incomplete fluorescence optics require review acknowledgement. Gain, offset,
and laser-power changes remain recorded but are not alone treated as evidence
of a different dye.

## Safety Contract

- Complete outputs are never overwritten.
- Active conversion uses `<output>.partial`.
- TIFF units are written through temporary files and promoted atomically.
- Cancellation is checked while fingerprinting each 8 MiB source block and
  before every image plane. A partially written temporary TIFF is removed;
  already completed TIFF units remain available for resume.
- Resume restarts the interrupted TIFF unit rather than appending inside a
  partially written OME-TIFF.
- Resume validates source, metadata, options, and calibration fingerprints.
- Complete projects are structurally validated before final promotion.
- Dry Run checks the nearest existing output parent without creating files.
- Required space is estimated as uncompressed pixels plus the larger of 64 MiB
  or 5%; conversion repeats the check immediately before writing.
- A batch is blocked when its combined estimated demand exceeds free space on
  any destination filesystem, and warns when less than 1 GiB would remain.
- Manifests and metadata do not store machine-specific source paths.
- A moved project can be validated without the original LIF.
- Strong validation can compare every output plane with the original source.

## Running The Current Linux GUI

From the project root:

```bash
./run_gui.sh
```

Files and folders can then be dragged into the queue. Use Dry Run before every
conversion batch.

## Windows Status

`packaging/windows/` contains the native PyInstaller build. GitHub Actions
builds and smoke-tests it on Windows; the downloaded package should also pass
the clean-machine checklist before a stable release.

## Open Product Work

- multi-FOV visual comparison;
- optional segmentation/measurement results as a downstream project module;
- a configurable common batch-output root;
- per-job automatic resume decisions and clearer failure recovery;
- Windows native build, clean-machine acceptance, licenses, and signing;
- final user-facing terminology and installer design.

## Detailed References

- `MANIFEST_SCHEMA.md`: portable project and path contract
- `CHANNEL_IDENTITY.md`: channel inference and confirmation design
- `DEVELOPMENT_NOTES.md`: engineering history
- `TEST_REPORT.md`: earlier acceptance evidence
- `CANDIDA_MULTIDIMENSIONAL_ACCEPTANCE_2026-08-02.md`: real Z/T/ZT evidence
- `audit_sandbox_2026-08-03/AUDIT_REPORT.md`: current edge and stress audit
- `CHANGELOG_AND_TESTS.md`: current release history and verification ledger
- `MANUAL_ACCEPTANCE_CHECKLIST.md`: completed evidence and remaining hands-on checks
