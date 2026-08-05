[English](README.md) | [中文](README_zh.md)

# LIF2TIFF

LIF2TIFF 是面向生命科学成像的 Leica LIF 检查、可视化、转换与验证工具。它以单个
LIF 文件为基本输入单元，只读打开原始文件，并输出路径可迁移、带结构化 metadata
的 TIFF project。

## 下载

GitHub Releases 提供 Linux 和 Windows 两套原生程序。每套程序同时包含 GUI 和 CLI，
下载后不需要另装 Python：

- `LIF2TIFF-Linux-x86_64-v2.0.0-beta.1.tar.gz`
- `LIF2TIFF-Windows-x64-v2.0.0-beta.1.zip`

## 主要功能

- 拖入一个或多个 LIF，或包含 LIF 的文件夹；
- 转换前预览 2D、Z-stack、time-lapse 和 ZT acquisition；
- 独立显示通道、使用源 LUT、调节对比度、缩放和平移；
- 播放时间序列并查看交互式 stage/plate map；
- 根据采集 metadata 推断通道身份，并要求用户明确确认；
- 可从用户配置目录加载项目私有 protocol registry，公开程序不内置内部采集参数；
- 对多个 LIF 执行 Dry Run、空间预估、转换、取消与断点恢复；
- 输出可迁移 TIFF project，并验证文件、hash、metadata 和原始像素；
- 批量导出单通道或多通道 overlay 汇报图片。

## CLI 示例

Linux：

```bash
./lif2tiff plan INPUT.lif
./lif2tiff convert INPUT.lif --ask-channel-roles
./lif2tiff validate INPUT_tiff --source INPUT.lif
```

Windows PowerShell：

```powershell
.\lif2tiff.exe plan "D:\data\experiment.lif"
```

## 安全原则

- 不修改原始 LIF；
- 不覆盖完整输出；
- 未完成转换保存在 `.partial`；
- manifest 使用相对路径，不保存本机绝对路径；
- 推断的染料身份与用户确认结果分开记录；
- 异常 acquisition 必须进入 review workflow。

仓库保留自动化测试、synthetic fixtures、公开 fixture 的精简过程结果、测试报告与
GUI 截图，但不上传原始实验 LIF 或大型 TIFF。旧版代码保存在
`legacy/readlif-v1/` 和 Git 历史中。

详细说明见 [English README](README.md)、[产品文档](docs/product-guide.md)与
[私有 registry 配置](docs/private-registry.md)。
