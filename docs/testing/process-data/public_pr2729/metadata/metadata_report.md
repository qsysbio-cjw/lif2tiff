# LIF Metadata Report

- Source file: `PR2729_frameOrderCombinedScanTypes.lif`
- Series count: 4
- Dimension types: ZT
- Image size: 64 x 64 px
- Channels per series: 2
- Pixel size: 3.968254 um/px
- Time span: 2017-01-30T07:56:07.817Z to 2017-01-30T07:56:07.820Z

## Channels

| C | Physical | Analysis | Stain | Biological role | Modality | Detector | Gain | Offset | Detection nm | Leica dye name | Assignment |
|---|---|---|---|---|---|---|---:|---:|---|---|---|
| 0 | green | unknown | None | unknown | fluorescence_like | None | None | None | None-None | None | user_override/manual |
| 1 | red | unknown | None | unknown | fluorescence_like | None | None | None | None-None | None | user_override/manual |

## Output Files

- `metadata.json`: full structured metadata for programmatic use
- `series_metadata.csv`: one row per series
- `channel_metadata.csv`: one row per series x channel
- `stage_positions.csv`: one row per series with stage coordinates
- `../views/stage_map.html`: interactive physical stage map with FOV rectangles and scale bar
- `source_metadata.xml`: raw Leica XML metadata
