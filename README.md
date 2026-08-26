# 对白剪辑台

> 一个零代码基础法律人，用自然语言和 AI 进行 vibe coding“搓”出来的本地视频效率工具。

对白剪辑台是一款 Windows 本地文字剪辑工具：先把对白转成带时间码的文字，再像修改普通文本一样删掉口误、重复、停顿和拖音，最后导出剪辑后的视频与独立 SRT 字幕。

它起源于我日常使用的开源字幕工具 [Subtitle Edit](https://github.com/SubtitleEdit/subtitleedit)，也建议与 Subtitle Edit 配合使用：

- Subtitle Edit 负责方便地下载、部署和调用各类开源语音识别引擎与模型，生成字幕和时间码。
- 对白剪辑台读取这些识别结果，完成“按文字剪视频”、节奏清理和字幕修改。
- 删除相应文字即可删除对应对白；修改字幕文字只影响导出的 SRT，不改变原声音和画面。

项目不会修改或删除源视频。视频、音频、字幕和项目记录均保存在本机，不会由本工具上传到网络。

## 为什么做它

很多视频剪辑软件已经提供文字剪辑，但这类功能常常需要订阅。Subtitle Edit 已经把本地开源语音识别的下载、部署和字幕制作做得很好，因此这个项目不重复制造一套模型商店，而是补上从“带时间码的字幕”到“按文字剪辑视频”的一段工作流。

它尤其适合对话、访谈、口播和法律知识类长视频的粗剪。识别与对齐精度和你在 Subtitle Edit 中选用的模型、音质及逐词时间码质量强相关，重要成片仍应人工试听复核。

中文场景下，本项目目前实测推荐 Qwen3 ASR；它对中文内容的识别表现较好，并可配合 Qwen3 Forced Aligner 生成逐词时间码。不同口音、录音环境和硬件下的速度与准确率会有差异，Faster Whisper 仍是速度和成熟度都很好的选择。

## 主要功能

- WebView2 独立桌面窗口，不触发 Edge、夸克及浏览器扩展的选中文字菜单
- 自动发现 Subtitle Edit 中已经安装的 Purfview Faster Whisper XXL 和 Qwen3 ASR CPP 模型
- Qwen3 配合 Forced Aligner 生成逐词时间码
- 按整句或逐词删除视频内容，Delete、Backspace、拖选和 Shift 连选均可使用
- 单独点击气口、长停顿和短字拖音标记，不必删除相邻文字
- 自动清理建议、一键应用和一次撤销
- 剪后即时播放；空格键控制当前视频暂停与继续
- “剪视频”和“改字幕”相互独立，字幕纠错不会改变声音或画面
- 自动保存项目；关闭桌面窗口时会先刷新待保存修改，再安全停止后台任务
- 生成低清预览，并使用 NVIDIA NVENC 或 CPU 导出 H.264 MP4
- 每次输出同时生成重新计时的独立 SRT，不把字幕烧录进画面
- 项目删除后先进入本地回收区，可恢复或永久清理

## AI 协作粗剪

完整字幕和时间码天然适合 AI 做语义理解。具备本机项目文件或本地接口访问能力的 AI 代理，可以：

1. 读取完整转录，理解对话脉络、主题和重复内容。
2. 按你的要求判断哪些句子、词语、停顿或赘述应删除。
3. 直接写入剪辑方案和字幕修改，再调用预览或导出。

这类工作不必依赖 Computer Use 模拟鼠标逐字点击，可以在代码或本地接口层完成；但普通网页聊天机器人不会自动获得你的电脑权限，仍需使用能够访问本地工作区或本地接口的 AI 代理。当前本地接口尚未承诺长期兼容，自动化前请保留项目备份。

## 运行条件

- Windows 10/11 x64
- Python 3.10 或更高版本，并可通过命令 `python` 启动
- .NET 8 Desktop Runtime（使用桌面版时需要）
- Microsoft Edge WebView2 Runtime（Windows 11 和多数 Windows 10 设备已自带）
- Subtitle Edit 及至少一个受支持的本地识别引擎和模型
- FFmpeg/FFprobe；优先复用 Subtitle Edit 已安装的版本，也支持系统 PATH 中的版本

当前已适配的识别组合：

- Purfview Faster Whisper XXL + Faster Whisper 模型，默认优先 `large-v3`
- Qwen3 ASR CPP + Qwen3 ASR GGUF + Qwen3 Forced Aligner

Subtitle Edit 中的其他引擎不会因为已经下载就自动可用；不同引擎仍需针对命令和逐词时间格式单独适配。

Subtitle Edit 官方的[语音转文字说明](https://github.com/SubtitleEdit/subtitleedit/blob/main/docs/features/speech-to-text.md)列出了其支持的本地及可下载引擎；Qwen 官方的 [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)项目说明了语言识别和 Forced Aligner 时间对齐能力。

## 开始使用

### 源码方式

1. 安装上述运行条件，并在 Subtitle Edit 中下载需要的引擎和模型。
2. 双击 `启动对白剪辑台.bat`。如果尚未构建桌面端，会回退到本地浏览器界面。
3. 也可以运行 `python app.py --open`。

### 构建桌面窗口

在项目根目录运行：

```powershell
.\构建桌面版.ps1
```

随后双击 `desktop\publish\对白剪辑台.exe`。桌面 EXE 依赖项目根目录中的 `app.py` 和 `static`，不能只复制一个 EXE 使用。

## 基本流程

1. 选择视频，或者粘贴视频完整路径。
2. 选择识别引擎和模型，点击“开始识别对白”。已有 SRT 时也可以直接导入。
3. 在“剪视频”中删除句子、字词、气口、停顿或拖音。
4. 在“改字幕”中修正错字、标点和表达。这里不会改变声音和画面。
5. 使用剪后即时播放或生成低清预览核对效果。
6. 导出成片；MP4 和重新计时的 SRT 会分别生成。

项目及输出默认保存在：

```text
.data\projects\项目编号
```

成片与字幕位于项目目录下的 `outputs`。`.data` 包含视频路径、字幕、剪辑记录和可能复制进来的媒体文件，已经被 Git 忽略，发布仓库时不要手动添加。

## 开发验证

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m py_compile app.py
node --check static\app.js
.\构建桌面版.ps1
```

发布仓库时不要提交 `.data`、`output`、`desktop\bin`、`desktop\obj` 或 `desktop\publish`。编译后的完整桌面包应作为单独的 GitHub Release 附件发布。

## 当前边界

- 当前只面向 Windows x64。
- 直接导入普通 SRT 时通常只有句级时间码，不能达到识别引擎逐词时间码的精细程度。
- 气口、长停顿、拖音和重复字建议主要由逐词起止时间、相邻间隔及文本规则推断，并不等于模型准确理解了语气；应用建议后需要试听。
- 文字剪辑的切点精度受识别和强制对齐结果影响，定位不准时请先在 Subtitle Edit 中调整字幕或更换模型。
- 暂不生成 Premiere Pro 或 DaVinci Resolve 时间线。

## 参与和修改

这是一个从真实需求出发、持续用自然语言迭代出来的小工具，代码结构仍有很多可以改进的地方。欢迎试用、提出问题、提交修改，或者直接 fork 成更适合你自己工作流的版本。

本项目采用 [MIT License](LICENSE)。
