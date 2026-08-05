# LIF2TIFF 2.0.0-beta.1

Version 2 is a ground-up replacement of the original readlif/customtkinter
prototype. It uses liffile for pixel access and PySide6 for the desktop GUI.

## Included

- Native Linux and Windows release packages, each with GUI and CLI.
- 2D, Z, T, and ZT preview and conversion.
- Multi-LIF queue, folder discovery, batch Dry Run, and storage preflight.
- Per-channel display controls, source LUT support, zoom, pan, and playback.
- Interactive acquisition and calibrated 384-well stage maps.
- Explicit channel confirmation with separate inferred and confirmed identity.
- Optional external protocol registry; the public package includes only a
  non-sensitive empty template.
- Portable project manifests, relative paths, hashes, cancellation, and resume.
- Single-channel and overlay presentation export.

## Validation evidence

- 116 automated tests passed with one documented expected failure.
- Public Z and ZT fixtures were converted and validated.
- A real 408-plane Candida ZT acquisition was validated exactly.
- A 4179-plane conversion passed cancellation, cleanup, and resume testing.

## Known beta limitation

A single batch cannot mix new outputs with resumable partial outputs because
Resume is currently a global batch switch. Run those groups separately.
