# Public Fixture Provenance

The full public LIF binaries are intentionally not committed. Local acceptance
used CC BY 4.0 fixtures from the Open Microscopy Environment and Zenodo:

- OME Bio-Formats PR 2729 frame-order fixture;
- OME FRAP 150519 fixture by Sean Warren;
- Image.sc FRET fixture 30856;
- Zenodo record 6606445 by Peter Zentis;
- Zenodo record 8252368 FLIM tile-scan fixture.

The compact naming-collision fixtures committed in
`tests/fixtures/public/ome_pr2729/output_collision/` are byte-identical renamed
copies of the OME Bio-Formats PR 2729 fixture. The original sample is copyright
Michael Goelzer and licensed under CC BY 4.0. The files are renamed only to test
output naming and Windows-compatible collision behavior; their image content
is unchanged.

- Source context: <https://github.com/ome/bioformats/pull/2729>
- License: [`CC-BY-4.0.txt`](CC-BY-4.0.txt)

`tests/fixtures/synthetic/malformed/not_a_lif.lif` is a small text fixture
created for this project and contains no microscopy data.

See the machine-readable reader comparison and hashes in
`docs/testing/process-data/audit-results/`.
