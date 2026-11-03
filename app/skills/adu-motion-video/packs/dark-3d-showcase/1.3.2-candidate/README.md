# 深色 3D · 多屏展陈 · 1.3.2-candidate

2026-10-11 竖屏人物窗口候选。大人物窗缩到小窗时同步平滑位置和尺寸；跨场从上一场的实际窗口接续。

选择 `anim4-showcase-macro@1.3.2-candidate`，在 brief / spec 顶层设置 `layout: "portrait"`，沿用 macro-plan → macro-build → 声音生成 → render。本版含 10 个镜头组，组ID、输入、cue、动作时钟、媒体时钟和音效合同与 `1.3.1-candidate` 对应；改变的是人物布局，不整体慢放演出或口播。具体容量、素材角色、原速动作窗和阅读区以 manifest.json 为准。

字体为优先推荐项，默认 preferred；原字体不可用时沿用随包 Noto 回退并检查实际排版。不要因 macOS 字体或 Vision 缺失阻断 Windows/Linux 使用。配乐须显式绑定合法曲目，或明确选择 none；不自动沿用旧片音乐。详见[兼容说明](../../../references/compatibility.md)和[完整制作流程](../../../references/full-project-templating.md)。

候选技术检查覆盖全库11风格/54组的匿名素材几何、重复与倒退寻帧、8处可见跨场接缝及音效记录一致性。没有完成这些新版的真人裁切、遮挡和连续声画验收，不把原基线验收追认为本版稳定。

旧版保持只读可重放：[前版与输入说明](../1.3.1-candidate/README.md)。当前运行规则与验证范围见[人物窗口舒适度](../../../references/presenter-comfort.md)；旧说明的版本、字体和配乐要求按当前合同核对。
