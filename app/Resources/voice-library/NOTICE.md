# 本地音色库

共 59 种独立说话人音色：美式英语 50 种（Kokoro 20 + LibriTTS 30），日语 9 种（Kokoro 5 + VOICEVOX 4）。不是 55 段固定口播；可离线朗读新文案。同一成品使用一个音色，避免句间换人。

## 来源与署名

- Kokoro-82M：hexgrad，Apache-2.0。https://huggingface.co/hexgrad/Kokoro-82M
- Kokoro ONNX 转换与推理：thewh1teagle，MIT。https://github.com/thewh1teagle/kokoro-onnx
- Piper en_US-libritts-high：Michael Hansen / Rhasspy；模型仓库 MIT，LibriTTS 数据集 CC BY 4.0；该模型从 train-clean-360 从头训练。https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/libritts/high
- LibriTTS：Heiga Zen et al., Google，CC BY 4.0。https://www.openslr.org/60/
- Sherpa-ONNX 推理与转换模型：k2-fsa，Apache-2.0。https://github.com/k2-fsa/sherpa-onnx
- 依赖包与字典的许可保留在 python 各自的 dist-info/licenses 或包目录中。分发时保留本文件和 licenses 目录。

## 使用与限制

在工作台右侧“音色库 · 选择与试听”中选择。日区默认 Alpha，美区默认 Heart。自定义任务只有提示词要求新配音时才会生成；音色应与文案语言相符。配音失败保持待补资源，禁止换成错误语言的系统声音。

试听样本只经过完整解码、音量、削波与 ASR 辅助检查；未由母语者逐条验收自然度。不承诺与真人录音相同。日语专名、多音词和产品名需要试听复核。技术检查不能代替听感验收。

模型、依赖、缓存和试听文件均保存在本目录；无需第三方配音 API Key。剪辑规划仍使用现有 DeepSeek 网关。迁移软件到其他电脑时还需复制此目录并设置 direct_media.local_voice_library。


## 新增日语可爱口播（VOICEVOX）

四国めたん、ずんだもん、春日部つむぎ、春歌ナナ：4 个独立模型说话人，不是同一声音调音高后的重复音色。每个声音均生成了试听样本及另一段新日语文案，通过完整解码、非静音和削波检查；尚未由日语母语者验收自然度。原日区默认 Alpha 保持。

上述音色发布时必须在视频说明或视频内署名。选择的音色会随配音生成“配音署名.txt”，其中包含需要复制到发布说明的精确署名和对应条款。

- VOICEVOX:四国めたん：https://zunko.jp/con_ongen_kiyaku.html
- VOICEVOX:ずんだもん：https://zunko.jp/con_ongen_kiyaku.html
- VOICEVOX:春日部つむぎ：https://tsumugi-official.studio.site/rule
- VOICEVOX:春歌ナナ：https://nanahira.jp/haruka_nana/guideline.html

VOICEVOX CORE 0.16.3：MIT；VVM 模型 0.16.0：允许嵌入应用再分发，生成音频须遵守对应音色条款。VOICEVOX ONNX Runtime 与模型完整条款保留在 models/voicevox/licenses/。Open JTalk 字典复用已内置 pyopenjtalk 字典，许可位于 python/pyopenjtalk/dictionary/COPYING。
