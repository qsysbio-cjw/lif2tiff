# LIF2TIFF Audit Sandbox - 2026-08-03

This directory contains read-only-input audit tools for the Qt workbench and
conversion preflight. It does not write beside source LIF files and does not
modify historical conversion outputs.

Human-readable findings and the next manual test sequence are in
`AUDIT_REPORT.md`.

## Commands

Run the edge-case suite from the repository root:

```bash
QT_QPA_PLATFORM=offscreen python -m pytest -q tests/test_edge_cases.py
```

Run metadata-only stress inspection against files or directories:

```bash
python scripts/stress_metadata_preflight.py \
  INPUT [INPUT ...] --output results/preflight.json
```

The stress script opens LIF metadata through the production planning path. It
does not read image pixels, create TIFFs, or create output directories.
