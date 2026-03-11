# lif2tiff

Convert Leica `.lif` confocal microscopy files to per-channel TIFF images with full metadata extraction.

Developed and tested on a **Leica DMI8-CS** (STED platform, standard confocal mode) using **2D single-plane, multi-channel** acquisitions. Z-stack and time-lapse code paths are implemented but unvalidated — see [Known Limitations](#known-limitations).

---

## Features

- Converts every series in a LIF file to individual per-channel TIFF files (LZW lossless compression)
- Extracts comprehensive metadata from the embedded LIF XML into per-series `metadata.json`
- Auto-detects channel identity from LUT names in the XML (no hardcoding required)
- Organises output by series and by channel (`by_channel/` flat view)
- Generates `metadata_summary.csv` and `validation_report.json` after conversion
- GUI application for non-technical users, with standalone executable builds (no Python required)
- CLI for batch and scripted workflows

---

## Requirements

**Python:** 3.11 (other 3.x versions untested)

**Dependencies:**

| Package | Version | Required for |
|---------|---------|-------------|
| `readlif` | ≥ 0.6.5 | CLI + GUI |
| `tifffile` | ≥ 2023.1.1 | CLI + GUI |
| `numpy` | ≥ 1.24 | CLI + GUI |
| `imagecodecs` | ≥ 2023.1.1 | CLI + GUI (LZW compression) |
| `customtkinter` | ≥ 5.2.0 | GUI only |

Install all:
```bash
pip install -r requirements.txt
```

Install CLI only (no GUI):
```bash
pip install readlif tifffile numpy imagecodecs
```

---

## Quick Start

### Command-line

```bash
# Convert all LIF files in a directory
python lif2tiff.py /path/to/data/

# Convert a single file
python lif2tiff.py "experiment.lif"

# Preview series info without exporting (dry-run)
python lif2tiff.py "experiment.lif" --dry-run

# Specify output directory
python lif2tiff.py "experiment.lif" -o ./converted

# Dump raw LIF XML for debugging metadata parsing
python lif2tiff.py "experiment.lif" --dump-xml

# Verbose logging
python lif2tiff.py "experiment.lif" -v
```

After conversion, generate metadata summary and validation report:
```bash
python summarize_metadata.py ./output -o ./output/metadata_summary.csv
python validate_output.py ./output -o ./output/validation_report.json
```

### GUI

```bash
python gui_app.py
```

1. Select a LIF file using **Browse**
2. Set output directory (default: `./output`)
3. Tick **Dry-run** to preview series without writing files
4. Select which channels to export (populated automatically from the file)
5. Click **Convert**

Validation and metadata summary are generated automatically after a successful conversion.

---

## Output Structure

```
output/
  {lif_name}/
    {series_name}/
      {series_name}_metadata.json
      {series_name}_C0_green.tif
      {series_name}_C1_brightfield.tif
      {series_name}_C2_red.tif
    by_channel/
      green/          ← symlinked copies of all green-channel TIFFs
      brightfield/
      red/
      metadata/
  metadata_summary.csv     ← one row per series, key acquisition parameters
  validation_report.json   ← pass/fail per series with summary statistics
```

`{lif_name}` is derived from the LIF filename with special characters replaced by underscores.

### Dimension handling

| Acquisition type | Output |
|-----------------|--------|
| 2D (single plane) | One TIFF per channel |
| Z-stack | Multi-page TIFF (Z slices as pages), Z spacing embedded in ImageJ metadata |
| Time-lapse | `T000/`, `T001/` … subdirectories; one TIFF per timepoint per channel |
| ZT (Z + time) | Per-timepoint subdirectories + multi-page Z-stack TIFFs |

> **Z-stack and time-lapse are unvalidated.** See [Known Limitations](#known-limitations).

---

## Channel Detection

Channels are identified automatically from the `LUTName` field in the LIF XML (`ChannelDescription`). Channel order is determined by `BytesInc` in the XML.

| LUT name in XML (case-insensitive) | Label used in output filenames |
|------------------------------------|-------------------------------|
| Gray / Grey | brightfield |
| Green | green |
| Red | red |
| Blue | blue |
| Cyan | cyan |
| Magenta | magenta |
| Yellow | yellow |
| *(anything else)* | *(used as-is)* |

---

## metadata.json Fields

Each series produces a `{series_name}_metadata.json` with the following fields:

| Category | Fields |
|----------|--------|
| Basic | `source_file`, `series_name`, `series_index`, `dimension_type` |
| Dimensions | `x`, `y`, `z`, `t`, `channels` |
| Pixel size | `x_um_per_px`, `y_um_per_px`, `z_um_per_px` |
| Per-channel | `label`, `lut`, `bit_depth`, `detector_name`, `detector_type`, `gain`, `offset`, `detection_range_begin_nm`, `detection_range_end_nm`, `dye_name`, `acquisition_mode`, `sequential_index` |
| Objective | `name`, `magnification`, `numerical_aperture`, `immersion` |
| Confocal | `scan_mode`, `line_averaging`, `frame_averaging`, `pinhole_um`, `pinhole_airy`, `zoom`, `scan_speed`, `scan_direction`, `pixel_dwell_time_us` |
| Microscope | `model`, `serial` |
| Stage position | `x_m`, `y_m`, `z_m` |
| Lasers | `wavelength_nm`, `intensity_percent` (active lines only, i.e. `IsVisible='1'`) |
| Timestamps | ISO 8601 UTC list (converted from Windows FILETIME) |
| Optical | `refraction_index`, `tld_mode`, `emission_wavelength_for_pinhole_airy_nm` |

---

## Standalone Executable (no Python required)

Standalone binaries are built with PyInstaller. **Cross-compilation is not supported** — you must build on each target platform separately.

### Linux / macOS

Requires conda (Miniconda or Anaconda):

```bash
conda activate <your-env>   # env must have the dependencies installed
bash build_app.sh
# Output: dist/LIF2TIFF
```

On macOS, the app may be quarantined by Gatekeeper. To remove the quarantine flag:
```bash
xattr -cr dist/LIF2TIFF
```

### Windows

Requires conda installed ([Miniconda download](https://docs.conda.io/en/latest/miniconda.html)):

1. Copy `lif2tiff.py`, `gui_app.py`, `summarize_metadata.py`, `validate_output.py`, and `build_app_windows.bat` to the same folder
2. Double-click `build_app_windows.bat` (or run it from an Anaconda Prompt)
3. Output: `dist\LIF2TIFF.exe`

The bat file creates a fresh conda environment, installs all dependencies, and runs PyInstaller automatically.

---

## Known Limitations

- **Tested only on 2D single-plane data.** Z-stack and time-lapse code paths are written but have not been validated against real acquisitions.
- **`by_channel/` does not recurse into time-lapse subdirectories** (`T000/`, `T001/`, …) — time-lapse files will be missing from the flat channel view.
- **Large Z-stacks** are loaded entirely into memory before writing — may cause out-of-memory errors on large volumes.
- **Sequential scan mode** may produce duplicate laser wavelength entries in `laser_settings`, and detector gain values may be inaccurate (first XML occurrence per detector name is used).
- **LIF format only.** `.lof`, `.xlef`, and other Leica formats are not supported.
- Developed on **Leica DMI8-CS**. Other Leica systems that produce standard LIF files should work, but have not been tested.
- The GUI accepts one LIF file at a time. Batch conversion of multiple files requires the CLI.

---

## Tested Environment

| Component | Detail |
|-----------|--------|
| Microscope | Leica DMI8-CS (STED platform, confocal mode) |
| Objective | HC PL APO CS2 63×/1.40 OIL |
| Image size | 1024 × 1024 px, 0.1804 µm/px |
| Acquisition | 2D, 3 channels, line averaging 3× |
| Detectors | HyD S (SiPM) × 2, Trans PMT × 1 |
| OS (tested) | Linux Ubuntu 22.04, Windows 11 |
| Python | 3.11 |

---

## License

MIT
