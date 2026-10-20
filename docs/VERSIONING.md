# 版本与证据快照

本仓库资料标识为 **framo-docs-20261020**。它是文档／证据快照，不是新的Framo安装版。

| 对象 | 实际标记 | 说明 |
|---|---|---|
| 私有源码 | 1.3.0 | 从当前version模块读取 |
| 本机运行目录 | 1.2 | 从实际桌面运行模块读取，不因功能较新而擅改版本 |
| 既有公开目录 | 1.2.1 | 旧项目材料，不作为本轮最新实现依据 |
| 现有安装构建 | 1.3.0 | 安装文件SHA256本轮已核对，构建内容与本机运行目录另有7个模块项差异 |
| 本轮安装包 | 未新建 | 没有构建、上传或声称完整新版本安装包 |

源目录有90个agent_core Python模块，实际运行目录91个；共16个模块项在内容或存在性上不同。版本号不足以说明全部代码关系，机器可读指纹见[软件快照](../evidence/software-snapshot.json)。

| 模块项 | 差异 |
|---|---|
| `agent_core/auto_decorations.py` | 文件内容不同 |
| `agent_core/direct_renderer.py` | 文件内容不同 |
| `agent_core/direct_vision.py` | 文件内容不同 |
| `agent_core/framo_supplements.py` | 仅运行目录存在 |
| `agent_core/independent_export.py` | 文件内容不同 |
| `agent_core/local_voice.py` | 文件内容不同 |
| `agent_core/long_video.py` | 文件内容不同 |
| `agent_core/montage.py` | 文件内容不同 |
| `agent_core/original_montage.py` | 文件内容不同 |
| `agent_core/original_montage_render.py` | 文件内容不同 |
| `agent_core/provider.py` | 文件内容不同 |
| `agent_core/sound_design.py` | 文件内容不同 |
| `agent_core/task_service.py` | 文件内容不同 |
| `agent_core/task_store.py` | 文件内容不同 |
| `agent_core/version.py` | 文件内容不同 |
| `agent_core/visual_presets.py` | 文件内容不同 |

这些差异不能被统一判为错误；运行目录保留了证据和布局补充能力。下一版应审查每项差异、形成单一经过验证的构建输入，并重新打包验证。不能直接整体覆盖某个目录，也不能只修改VERSION来伪造一致性。

当前已有安装版的构建内容也已经读取：它与运行目录在direct_renderer、independent_export、locked_opening、original_montage、original_montage_render、task_service、version共7项不同，尚无本机后加的批次开头多样性逻辑。源码与运行目录的16项差异和安装构建与运行目录的7项差异是两组不同对照，不能相加当作23个独立缺陷。详见[安装包证据](../evidence/installer-1.3.0.json)。

本轮没有修改上述业务代码。任务恢复仍遵守原有输入和版本绑定；文档的日期更新不授予旧检查点跨版本继续的权限。
