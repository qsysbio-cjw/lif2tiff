# Channel Identity Design

## Practical conclusion

`liftool channels` and the conversion manifest share one channel-resolution
layer. It answers
three separate questions:

1. Is a channel transmitted light, fluorescence, or still unknown?
2. Which stain or fluorophore is plausible?
3. Does one LIF use a consistent channel protocol across its series?

These answers are deliberately not collapsed into one label. A green LUT is a
display choice, not proof of BODIPY or SYTOX Green. A Leica dye preset is useful
evidence, but it is not a user-confirmed biological assignment.

## Standard and review paths

Standard mode accepts a LIF when channel count, detector/modality mapping, and
excitation/emission settings remain consistent across series. Changes in gain,
offset, or laser power are retained as acquisition variation but do not force
the review path.

Review mode is recommended when:

- channel count or channel-index layout changes between series;
- a channel index changes detector or modality;
- excitation or emission settings change materially.
- a fluorescence channel lacks excitation or emission metadata.

"Materially" currently means that the combined excitation or emission range
across compared series exceeds 0.5 nm. A frozen-snapshot sensitivity audit
showed that 0.1 nm adds one likely fractional-detector false alarm, while 1.0
nm suppresses one real 561-to-562 nm acquisition change. The 0.5 nm rule keeps
the established 501 standard / 46 review split and is recorded in every review
payload rather than hidden in code.

An internal frozen-snapshot audit found that most project files follow the
standard route and a smaller subset requires review. This is empirical project
evidence, not a universal Leica-format guarantee. Exact inventories and
project-specific acquisition signatures are kept in a private companion
repository.

Review is a workflow gate, not a conversion failure. The core refuses to create
an output until review is explicitly acknowledged; after acknowledgement, the
same lossless converter and validator are used and the decision is retained in
the manifest.

## Structured review payload

`metadata/workflow_review.json` is designed as the shared contract for the CLI
and future Linux/Windows GUI. It contains:

1. `groups`: the most frequent acquisition protocol (`RG-001`) and every
   variant (`VG-...`), including the affected series;
2. `review_differences`: channel structure, detector/modality, optical, or
   incomplete-metadata differences relative to the reference;
3. `informational_variations`: gain, offset, and laser-power distributions that
   are preserved but do not alone force review;
4. `decision`: pending before review, or the explicit `continue_all` decision
   and accepted reason/group ids after acknowledgement.

The reference is only a compact comparison baseline. The software does not
label it as correct and does not automatically split or discard variant series.

## Evidence hierarchy

Modality:

1. Transmission detector/scan metadata (`TLD`, `Trans PMT`, or transmission)
   gives high-confidence brightfield.
2. Internal fluorescence detector metadata (`HyD`, internal PMT) gives
   high-confidence fluorescence.
3. Active excitation wavelengths without detector evidence give
   medium-confidence fluorescence.
4. LUT color is never used by itself to decide modality.

Stain identity:

1. Explicit user assignment is `confirmed`.
2. An exact Leica `DyeName` registry match is a `high` candidate.
3. Filename or series-name matches are only hints. They become `medium` when
   the fluorescence channel is compatible with the registry's broad optical
   range, `low` when optical metadata is incomplete, and are suppressed when
   optics conflict.
4. Instrument presets such as Alexa 488 are recorded separately and are not
   reported as biological stains.

The optical ranges in `channel_registry.json` are soft routing hints. They are
intentionally broad and must not be interpreted as spectral unmixing,
quantitative dye identification, or proof of staining biology.

## Output contract

The compact terminal table reports one summary row per channel index:

- stable directory suggestion such as `C00_green` or `C01_brightfield`;
- modality and confidence;
- plausible stain candidates and confidence;
- overall consistency status and warning text.

With `--json-output`, the complete per-series evidence is retained for later
GUI use. The report stores the source filename, not an absolute source path.
The command reads metadata only and never writes beside the LIF unless the user
explicitly supplies a new JSON output path.

## Current boundary

The resolver is connected to `convert`, but it does not automatically confirm a
dye. The public package contains a generic channel registry and an empty
protocol-registry template. Authorized users may install a private registry;
exact tolerant matches then provide project-specific acquisition evidence, not
proof of reagent presence.

Users may confirm assignments with repeatable `--channel-role` arguments or opt
into `--ask-channel-roles`. Without either option, conversion stays
non-interactive and preserves candidates as unconfirmed. Neutral channel-first
TIFF folders are unchanged.

Configuration is documented in [`private-registry.md`](private-registry.md).
Detailed internal audit outputs are not distributed in the public repository.
