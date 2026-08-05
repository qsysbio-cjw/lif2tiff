[English](README.md) | [中文](README_zh.md)

# lif2tiff

将莱卡 `.lif` 共聚焦显微镜文件转换为逐通道 TIFF 图像，并提取完整元数据。

在 **莱卡 DMI8-CS**（STED 平台，标准共聚焦模式）上开发与测试，采用 **2D 单层多通道**采集方式。Z-stack 和 time-lapse 代码路径已实现但未经验证，详见[已知限制](#已知限制)。

---

## 功能特性

- 将 LIF 文件中的每个 series 转换为独立的逐通道 TIFF 文件（LZW 无损压缩）
- 从内嵌 LIF XML 中提取完整元数据，生成每个 series 的 `metadata.json`
- 自动从 XML 中的 LUT 名称识别通道身份，无需硬编码
- 按 series 和通道整理输出（`by_channel/` 扁平视图）
- 转换完成后生成 `metadata_summary.csv` 和 `validation_report.json`
- 提供图形界面（GUI），支持独立可执行文件（无需安装 Python）
- 提供命令行（CLI），适合批量和脚本化工作流

---

## 环境要求

**Python：** 3.11（其他 3.x 版本未测试）

**依赖包：**

| 包名 | 版本要求 | 用途 |
|------|---------|------|
| `readlif` | ≥ 0.6.5 | CLI + GUI |
| `tifffile` | ≥ 2023.1.1 | CLI + GUI |
| `numpy` | ≥ 1.24 | CLI + GUI |
| `imagecodecs` | ≥ 2023.1.1 | CLI + GUI（LZW 压缩） |
| `customtkinter` | ≥ 5.2.0 | 仅 GUI |

安装全部依赖：
```bash
pip install -r requirements.txt
```

仅安装 CLI 依赖（无 GUI）：
```bash
pip install readlif tifffile numpy imagecodecs
```

---

## 快速开始

### 命令行

```bash
# 转换目录下所有 LIF 文件
python lif2tiff.py /path/to/data/

# 转换单个文件
python lif2tiff.py "experiment.lif"

# 预览 series 信息，不导出文件（dry-run）
python lif2tiff.py "experiment.lif" --dry-run

# 指定输出目录
python lif2tiff.py "experiment.lif" -o ./converted

# 导出原始 LIF XML（用于调试元数据解析）
python lif2tiff.py "experiment.lif" --dump-xml

# 详细日志
python lif2tiff.py "experiment.lif" -v
```

转换完成后，生成元数据摘要和验证报告：
```bash
python summarize_metadata.py ./output -o ./output/metadata_summary.csv
python validate_output.py ./output -o ./output/validation_report.json
```

### 图形界面

```bash
python gui_app.py
```

1. 点击 **Browse** 选择 LIF 文件
2. 设置输出目录（默认：`./output`）
3. 勾选 **Dry-run** 可预览 series 而不写入文件
4. 选择要导出的通道（从文件中自动读取）
5. 点击 **Convert**

转换成功后自动生成验证报告和元数据摘要。

---

## 输出结构

```
output/
  {lif名称}/
    {series名称}/
      {series名称}_metadata.json
      {series名称}_C0_green.tif
      {series名称}_C1_brightfield.tif
      {series名称}_C2_red.tif
    by_channel/
      green/          ← 所有 series 绿色通道的扁平视图
      brightfield/
      red/
      metadata/
  metadata_summary.csv     ← 每个 series 一行，包含关键采集参数
  validation_report.json   ← 每个 series 的验证结果与汇总统计
```

`{lif名称}` 由 LIF 文件名派生，特殊字符替换为下划线。

### 维度处理

| 采集类型 | 输出方式 |
|---------|---------|
| 2D（单层） | 每通道一个 TIFF |
| Z-stack | 多页 TIFF（Z 切片为页），ImageJ 元数据中嵌入 Z 间距 |
| Time-lapse | `T000/`、`T001/`… 子目录，每时间点每通道一个 TIFF |
| ZT（Z + 时间） | 按时间点子目录 + 多页 Z-stack TIFF |

> **Z-stack 和 time-lapse 未经验证。** 详见[已知限制](#已知限制)。

---

## 通道识别

通道从 LIF XML（`ChannelDescription`）中的 `LUTName` 字段自动识别，通道顺序由 XML 中的 `BytesInc` 决定。

| XML 中的 LUT 名称（大小写不敏感） | 输出文件名中的标签 |
|----------------------------------|------------------|
| Gray / Grey | brightfield |
| Green | green |
| Red | red |
| Blue | blue |
| Cyan | cyan |
| Magenta | magenta |
| Yellow | yellow |
| *（其他）* | *（原样使用）* |

---

## metadata.json 字段说明

每个 series 生成一个 `{series名称}_metadata.json`，包含以下字段：

| 分类 | 字段 |
|------|------|
| 基本信息 | `source_file`、`series_name`、`series_index`、`dimension_type` |
| 维度 | `x`、`y`、`z`、`t`、`channels` |
| 像素尺寸 | `x_um_per_px`、`y_um_per_px`、`z_um_per_px` |
| 逐通道 | `label`、`lut`、`bit_depth`、`detector_name`、`detector_type`、`gain`、`offset`、`detection_range_begin_nm`、`detection_range_end_nm`、`dye_name`、`acquisition_mode`、`sequential_index` |
| 物镜 | `name`、`magnification`、`numerical_aperture`、`immersion` |
| 共聚焦参数 | `scan_mode`、`line_averaging`、`frame_averaging`、`pinhole_um`、`pinhole_airy`、`zoom`、`scan_speed`、`scan_direction`、`pixel_dwell_time_us` |
| 显微镜 | `model`、`serial` |
| 载物台位置 | `x_m`、`y_m`、`z_m` |
| 激光 | `wavelength_nm`、`intensity_percent`（仅活跃激光线，即 `IsVisible='1'`） |
| 时间戳 | ISO 8601 UTC 列表（由 Windows FILETIME 转换） |
| 光学参数 | `refraction_index`、`tld_mode`、`emission_wavelength_for_pinhole_airy_nm` |

---

## 独立可执行文件（无需安装 Python）

独立可执行文件使用 PyInstaller 打包。**不支持交叉编译**，需在各目标平台上分别构建。

### Linux / macOS

需要 conda（Miniconda 或 Anaconda）：

```bash
conda activate <你的环境>   # 环境中须已安装依赖
bash build_app.sh
# 输出：dist/LIF2TIFF
```

macOS 上应用可能被 Gatekeeper 隔离，移除隔离标记：
```bash
xattr -cr dist/LIF2TIFF
```

### Windows

需要已安装 Python（python.exe 在 PATH 中）：

1. 将 `lif2tiff.py`、`gui_app.py`、`summarize_metadata.py`、`validate_output.py` 和 `build_app_windows.bat` 复制到同一文件夹
2. 双击运行 `build_app_windows.bat`
3. 输出：`dist\LIF2TIFF.exe`

bat 脚本会自动安装所有依赖并运行 PyInstaller。

---

## 已知限制

- **仅在 2D 单层数据上测试过。** Z-stack 和 time-lapse 代码路径已编写但未经真实数据验证。
- **`by_channel/` 不递归处理 time-lapse 子目录**（`T000/`、`T001/`…），time-lapse 文件不会出现在通道扁平视图中。
- **大型 Z-stack** 在写入前会全部载入内存，大体积数据可能导致内存不足。
- **顺序扫描模式**下 `laser_settings` 中可能出现重复激光波长条目，探测器增益值可能不准确（每个探测器名称取 XML 中第一次出现的值）。
- **仅支持 LIF 格式。** `.lof`、`.xlef` 及其他莱卡格式不受支持。
- 在 **莱卡 DMI8-CS** 上开发，其他能生成标准 LIF 文件的莱卡系统理论上可用，但未经测试。
- GUI 每次只接受一个 LIF 文件，批量转换多个文件需使用 CLI。

---

## 测试环境

| 组件 | 详情 |
|------|------|
| 显微镜 | 莱卡 DMI8-CS（STED 平台，共聚焦模式） |
| 物镜 | HC PL APO CS2 63×/1.40 OIL |
| 图像尺寸 | 1024 × 1024 px，0.1804 µm/px |
| 采集方式 | 2D，3 通道，线平均 3× |
| 探测器 | HyD S（SiPM）× 2，Trans PMT × 1 |
| 测试系统 | Linux Ubuntu 22.04，Windows 11 |
| Python | 3.11 |

---

## 许可证

MIT
