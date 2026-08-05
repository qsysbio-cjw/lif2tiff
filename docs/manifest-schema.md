# Manifest v0.3

`manifest.json` is the relocatable project index. It does not duplicate the
full Leica metadata stored under `metadata/`.

## Responsibilities

- identify the source LIF without storing its absolute path;
- record converter and schema versions;
- list every series, channel, multidimensional shape, and exported TIFF;
- store only project-relative output paths;
- expose per-file and per-series completion states;
- retain inferred and user-confirmed channel identity as separate values;
- record conversion options, warnings, errors, and the last validation result.

## Project states

- `converting`: conversion is active;
- `interrupted`: a keyboard interruption left resumable partial output;
- `failed`: an error left partial output for inspection or resume;
- `ready_for_validation`: pixel export finished;
- `validation_failed`: exported files did not satisfy the manifest;
- `complete`: validation passed and the project was promoted from `.partial`.

The converter never writes into an existing complete project. `--resume` only
accepts the matching sibling `<output>.partial` directory and verifies the
source filename, size, full-file SHA-256, Leica XML SHA-256, channel options,
intensity option, and plate-calibration SHA-256.

## Path contract

Every path in `metadata_files` and every TIFF `path` must:

- be relative to the project root;
- use POSIX `/` separators in JSON;
- contain no `..` component;
- remain valid after moving the complete project directory.

The unmodified `source_metadata.xml` is vendor source material rather than an
application-generated path index. All paths created by this tool are relative.

## Default image contract

- output layout is channel-first;
- Leica series names are preserved as filenames unless a character is illegal
  on Windows or Linux;
- the series index remains internal manifest data and is not added to filenames;
- one series and one channel produce one file;
- 2D data use grayscale TIFF with `YX` axes;
- Z, T, and ZT data use grayscale OME-TIFF with `ZYX`, `TYX`, or `TZYX` axes;
- multidimensional files are streamed plane by plane in T-then-Z order;
- uniform T spacing is written as OME `TimeIncrement`; millisecond source
  timestamps remain in the full metadata bundle;
- Leica mosaic `M` positions are expanded into explicit virtual series with
  `source_series_index` and `mosaic_index`;
- original integer dtype and pixel values are preserved.

Each file record contains the minimum machine-readable mapping needed by later
tools:

- `path`
- `channel_index`
- `axes`
- `shape`
- `dtype`
- `z_count`
- `t_count`
- `page_count`
- `container`
- physical X/Y/Z pixel sizes when available
- uniform time increment when available
- conversion `status` and final `size_bytes`

`metadata/files.csv` exposes the same mapping as a flat table. Full Leica
acquisition metadata remains in `metadata/metadata.json` and the CSV metadata
tables instead of being duplicated into the manifest.

## Channel identity contract

`channel_resolution` contains the LIF-level summary and candidate evidence.
Each series/channel contains a compact `identity` object with modality,
inferred/confirmed ids, `review_status`, and protocol-match ids. The complete
per-series evidence is stored once in `metadata/channel_resolution.json` rather
than repeated throughout the manifest and metadata bundle.

Automatic protocol or filename matching only fills `inferred_dye`. Only an
explicit `--channel-role` value or an answer collected by
`--ask-channel-roles` fills `confirmed_dye`. `unknown` is a valid explicit user
decision. TIFF directory names never change to an inferred or confirmed dye.

## Workflow decision

`channel_resolution.workflow` records `standard` or `review`, its reason codes,
whether acknowledgement is required, and whether it was explicitly supplied.
Stable acquisitions are `ready`; gain/offset/laser-power-only changes are
`ready_with_variation`. Channel-count/layout, modality/detector, excitation/emission,
or incomplete fluorescence-optics cases are `review_required`.

Review-route conversion creates no complete or partial output until the caller
acknowledges the review. The manifest then records `review_acknowledged: true`
and the acknowledgement source. Confirming a dye does not implicitly accept an
acquisition-structure warning.

`metadata/workflow_review.json` is the detailed, GUI-ready review payload. It
contains the most frequent acquisition-protocol group as a reference, all
variant groups with their series names, review-relevant parameter differences,
informational gain/offset/laser-power variations, and the explicit user
decision. The reference group is a comparison baseline, not a claim that its
settings are biologically or technically correct.

Excitation and emission values use an explicit 0.5 nm tolerance: the combined
minimum-to-maximum span must be no greater than 0.5 nm to remain in one group.
The manifest stores only a compact summary plus the relative
`metadata/workflow_review.json` path.
