# 从源码运行 Framo

2026-11-03 起，本仓库包含 Framo 实际桌面运行核心、渲染工具、模型网关、测试与构建源码。第一方代码采用 MIT；第三方资源保留原许可。旧文档中的 2026-10-20 测试结果仍是历史证据，不代表此次进行了新电脑完整剪辑验收。

## 环境

建议 Windows 10/11、Python 3.12（含 Tcl/Tk）、FFmpeg/FFprobe、Node.js 22。源码目录与工作数据使用短英文路径；例如 `D:/Framo`。Python 运行时、模型权重、FFmpeg 和第三方依赖不进入 Git 历史。

```powershell
git clone https://github.com/lashawnaplikerd710-del/Framo.git D:/Framo
cd D:/Framo
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r app/requirements.txt
Copy-Item app/config.example.json app/config.native.json
.\.venv\Scripts\python.exe scripts/fetch_fonts.py
```

将有权使用的 `ffmpeg.exe` 和 `ffprobe.exe` 放入 `app/toolchain/`。需要完整字体目录时运行 `scripts/fetch_fonts.py --all`；下载按冻结 URL 和 SHA256 校验，随字体的 OFL 声明已经保留。

## 两种模型接入方式

**CLI 直接使用自己的模型密钥**，不需要项目作者发放访问码：

```powershell
$env:DEEPSEEK_API_KEY = Read-Host '输入你自己的模型密钥'
cd app
..\.venv\Scripts\python.exe -B -m agent_core --config config.native.json --state-dir UserData doctor
..\.venv\Scripts\python.exe -B -m agent_core --help
```

`doctor` 只做配置检查，不验证联网模型可用性。编辑 `provider`、`vision_provider` 和 `complex_vision_provider` 可使用对应提供方的兼容接口；具体字段见 `agent_core/runtime.py`、`model_router.py` 和 `config.example.json`。CLI 支持的工作流和参数以 `--help` 为准。密钥只保留在当前进程环境或自己的凭据管理器中，不提交配置文件。

**桌面 UI 使用自行部署的网关**。原桌面设置流程使用网关访问码；开源不把作者的运营网关变成免费公共服务。网关完整实现和管理源码见 [gateway](../gateway/README.md)，部署到自己的 Cloudflare 账号并配置自己的上游密钥，生成自己的访问码。

```powershell
cd D:/Framo/app
..\.venv\Scripts\python.exe -B run_desktop.py
```

在设置中填写自己的网关 `/v1` 地址和自己的访问码。工作数据默认进入源码目录的 `UserData`，不会加载作者的任务记录。初次运行无任务是正常情况。

## 语音、转写与可选功能

- SenseVoice：下载 Sherpa-ONNX 对应的 SenseVoice 模型包，按其 `LICENSE`／`MODEL_LICENSE` 使用，解压到 `app/models/sensevoice`；适配代码见 `app/toolchain/scripts/local_asr.py`。
- 配音：`app/Resources/voice-library/catalog.json` 保存引擎、相对文件名、文件校验值和来源；`NOTICE.md` 保存音色归属／署名。按清单从上游下载并保留模型许可，配置 `direct_media.local_voice_library`。仓库不含数 GB 的权重、音色预览或依赖解释器；缺失时会明确提示，不能把无权重的克隆当作完整离线安装包。
- 动效模板：MIT 的 `adu-motion-video` 源码位于 `app/skills/adu-motion-video`，第三方包通过该目录的 `npm ci` 单独安装；Chrome／Edge 不随源码分发。
- 原创音乐生成器：在 `app` 下执行 `python -c "from release.builtin_audio import build; build('Resources/DirectBuiltin')"`。
- 剪映原生兼容、去字幕模型与 Windows 本地语音是可选依赖，不需要它们即可研究、修改和测试核心规划／编译代码。原生兼容需用户自己的合法安装，不包含专有编辑器程序或 DLL。

## 检查与贡献

仓库根目录运行 `python scripts/validate_docs.py`。默认离线回归使用 `python scripts/run_core_tests.py`，无需真实密钥，不调用付费模型。网关执行 `node --test gateway/tests/*.test.mjs`。其他 `app/tests` 包含媒体、Windows 设备与外部依赖测试，请阅读测试前提后运行，不将跳过或环境不满足写成完整验收通过。

修改请提交 PR；报告问题前移除模型密钥、访问码、私人视频与完整任务数据库。安装包、线上模型调用和端到端成片仍需分别验证。
