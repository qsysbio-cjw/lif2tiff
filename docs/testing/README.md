# Testing Evidence

This directory records the engineering process without publishing original
experimental LIF files.

- `test-report.md` records the initial conversion-core validation.
- `audit-2026-08-03.md` records edge, naming, storage, and portability tests.
- `candida-multidimensional-acceptance-2026-08-02.md` records real-data 2D,
  Z, T, and ZT acceptance without retaining the source data.
- `process-data/` contains selected machine-readable output from synthetic or
  publicly licensed fixtures.

Valid LIF samples under `tests/fixtures/public/` use documented public licenses.
`tests/fixtures/synthetic/` contains only intentionally malformed bytes. Public
fixture provenance is documented in `tests/fixtures/PUBLIC_FIXTURES.md`.

Large generated TIFF projects, virtual environments, and original laboratory
data remain outside Git. Project-specific inventories and exact acquisition
registries are retained separately in a private companion repository.
