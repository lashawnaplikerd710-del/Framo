# 第三方组件与资源说明

当前仓库公开第一方源码、贴纸／音效资源及逐项许可、字体目录与下载工具，不分发模型权重、Python运行时或FFmpeg二进制。第一方MIT许可不覆盖第三方材料。下表是当前应用依赖的来源清单摘要，不替代正式安装包中的完整许可证和具体音色条款。

| 组件／资源 | 来源与应保留内容 |
|---|---|
| Python 3.12、Tcl/Tk | PSF及各自许可，随实际运行包保留 |
| FFmpeg／FFprobe | 实际构建为GPL v3构建，保留构建标识、许可、对应源代码／构建信息；[FFmpeg](https://ffmpeg.org/)与[BtbN构建](https://github.com/BtbN/FFmpeg-Builds) |
| SenseVoice | 以实际模型包LICENSE、MODEL_LICENSE、ORIGIN为准；[项目](https://github.com/FunAudioLLM/SenseVoice) |
| Kokoro、Sherpa-ONNX及字典 | 以实际模型、字典和软件包的分别许可为准；不将一个仓库许可扩展到全部声音资源 |
| VOICEVOX | 核心、ONNX和人物音色条款分开保留；需要署名的音色必须给出对应署名 |
| Fluent Emoji | 当前目录记录Microsoft来源与MIT声明；[项目](https://github.com/microsoft/fluentui-emoji) |
| IconPark国内电商贴纸 | 当前目录记录36款、Apache-2.0来源；[项目](https://github.com/bytedance/IconPark) |
| 高级立体贴纸 | 当前分类15款、3dicons来源及CC0记录；[项目](https://github.com/realvjy/3dicons) |
| 字体 | 逐字体保留来源与OFL等对应许可，不能统一声称所有字体适用同一条款 |
| 其他音效／图像／模板 | 按安装包实际目录的来源、版本、修改与许可信息保留 |
| NSIS及媒体处理依赖 | 按实际构建与包元数据保留许可，不把Framo第一方代码的声明套用于这些依赖 |

真实界面截图中可见贴纸预览，保留其来源类别说明；截图不是授予用户将全部第三方资源自由再分发的授权。正式商业分发仍需对照实际包完成逐项复核。

软件不是任何外部编辑器、模型或资源项目的官方产品；提及依赖名称只为说明实现和归属。私人测试素材和含人物的成片没有放入公开案例附件。


## 已纳入的第三方实现

- `app/skills/adu-motion-video`：MIT，Copyright 2026 阿杜Next；保留 LICENSE、THIRD_PARTY.md 及其依赖／嵌入字体许可。仅源码，演示成片及安装依赖不随仓库提供。
- `app/toolchain/scripts/vendor/jianying_crypto_bridge.py`：Apache-2.0 上游适配，出处及变更说明在 `app/toolchain/third_party/README.md`，附原许可证。未分发专有编辑器DLL。
- `app/desktop-assets/media-library`：字体／贴纸／音效各自的许可证和来源与目录一并保存；较大字体按冻结URL和SHA256下载。
- `app/Resources/voice-library`：仅音色目录、依赖清单与来源／署名说明，未分发模型权重或第三方推理解释器。
