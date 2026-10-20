# 当前能力矩阵

核验日期2026-10-20。实现依据来自当前本机运行模块；界面证据为本轮实际打开的软件。测试和案例有各自范围，不能互相替代。

| 功能 | 实现依据（私有模块名） | 证据与边界 |
|---|---|---|
| 五类创作设置 | desktop_workbench、desktop_studio | 当前截图直接可见；不是历史CLI全部工作流都在界面曝光 |
| 20素材／单条7分钟／输出10分钟 | video_limits、task_service | 共享常量和离线边界测试；不代表任意编码都能导入 |
| 本地转写与真实抽帧 | task_service、analysis_cache、direct_vision | 使用语音与帧证据；静帧不能证明全部连续动作 |
| 结构化规划 | deepseek、commerce、long_video、original_montage | 模型输出受JSON和编译检查约束 |
| 来源和高亮证据补充 | framo_supplements（运行目录） | 保守证据ID、信号与主体布局提示；无检测结果不会虚构检测 |
| 四十秒连续开头 | locked_opening | 约束源从零开始、连续顺序和原速；句中可延到句末 |
| 批次不同开头 | locked_opening | 本机后续修改，未进入现有1.3.0安装版；明确要求触发，不足来源不保证全不同 |
| 原声混剪 | original_montage、original_montage_render | 保留原口播和画面已有字幕；不做新口播、字幕、BGM |
| 新口播与本地音色 | ad_narration、narration_timing、local_voice | 69个目录条目，不代表69个声音均在本轮试听 |
| 文字动效、转场和特效 | caption_motion、compound_transitions、visual_presets | 受具体预设、工作流和用户开关限制 |
| 贴纸和音效 | auto_decorations、sound_design、direct_resources | 目录与来源可查；存在已知开关误判 KI-01 |
| 独立MP4渲染 | direct_renderer、independent_export | 技术检查和解码检查；不等于完整人工审片 |
| 有声预览 | output_preview、desktop_studio | 入口已实现；本轮界面采集未播放私人素材 |
| 任务与检查点 | task_service、task_store、task_recovery、background | 版本／素材变化、鉴权和不确定请求可能阻止继续 |
| 访问码与额度 | gateway_credentials；网关policy、recovery | 本地16项网关测试；未在本轮新建或变更访问码 |
| 模板库和字幕清除 | desktop_templates、desktop_subtitle_removal | 代码存在；本轮没有执行字幕清除质量测试 |

当前主路由为直接FFmpeg成片。历史原生草稿流程保留为兼容路径，不把草稿登记视为直接成片成功。详细限制见[已知问题](LIMITATIONS.md)。
