# 更新记录

## v0.1.0 - 2026-08-26

首个公开版本。

### 已实现

- 结合 Subtitle Edit 已安装的 Purfview Faster Whisper XXL、Qwen3 ASR CPP 和相应模型进行本地识别
- 支持导入现有 SRT，并按句子或逐词时间码剪辑
- 支持删除对白、气口、长停顿、拖音和重复内容
- 支持自动清理建议、一键应用和撤销
- 支持剪后即时播放、低清预览和 NVIDIA NVENC/CPU 导出
- 支持独立修改字幕，导出重新计时的 SRT，不烧录字幕
- 提供 WebView2 桌面窗口、自动保存、项目回收区和安全退出

### 已知边界

- 目前仅面向 Windows x64
- 桌面程序仍需要本机 Python、.NET 8 Desktop Runtime 和 WebView2 Runtime
- 普通 SRT 通常只有句级时间码；逐词剪辑建议使用带逐词时间信息的识别结果
- 节奏类建议是基于时间码和文本规则的辅助判断，最终结果仍需试听
