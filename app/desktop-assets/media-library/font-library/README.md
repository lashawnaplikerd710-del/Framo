# Framo 字体库 v3

共 54 个字体家族、67 个原始字体文件。本次新增 30 个字体家族详见上一级 README.md。已接入 Framo 自动字幕选择与 Pillow 渲染流程。

manifest.json 保存语言、风格、用途、字重、可变字体轴、字形范围、逐文件来源和 SHA-256。fonts/ 各家族目录保留 SIL Open Font License 原文及版权声明；原始字体未改写。随软件分发时保留这些文件。字体读取无需系统安装或网络下载。

字体适用语言由 manifest 的 languages 标签筛选，实际字幕仍需逐句查字形；一款英文字体不能因此用于渲染中文。颜色与字体独立控制。

font_library.py 是可单独复用的加载模块；Framo 使用 agent_core/font_library.py 与 auto_decorations.py 进行自动选择。需要 Python 3.10+ 和 Pillow。
