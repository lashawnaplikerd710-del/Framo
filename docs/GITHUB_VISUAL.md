# GitHub展示设计规范

本次只优化公开仓库的README和配套视觉材料，不修改Framo业务代码、软件界面或公开范围。沿用真实F标志、石墨黑面板与银白主色，保持原始软件截图。

## 视觉系统

| 角色 | 取值／约束 |
|---|---|
| 主背景 | #0e0f10，与软件深色画布一致 |
| 面板 | #171718；线条#343438 |
| 主文字 | #f0f0f2 |
| 次要文字 | #a1a1a7 |
| 流程信号 | #a8dadd，仅作工作流汇合和连接强调 |
| 标志 | 原有F图形，保持比例和识别特征 |
| 图形语言 | 媒体片段、语音信号、连接和输出位置；是工作流示意，非产品多轨编辑器截图 |
| 文案 | 中文为主，英文提供简短补充；不增加用户量、认证、下载量或未经验证的效果承诺 |

## 首页层级

品牌横幅 → 一句话说明 → 四个入口 → 真实工作台 → 主要创作方式 → Agent流程 → 功能边界 → 界面导览 → 实测及已知问题 → 文档导航。

安装与使用为主要入口，其余链接进入已经存在的文档。文件生成与创意合格分开，关键元素缺失直接显示，历史细节可折叠展开。正文使用GitHub原生标题、表格、链接和折叠区，遵循访问者的GitHub主题。

## 视觉资产

- `assets/github/hero.png`：1600×680，桌面横幅。
- `assets/github/hero-mobile.png`：800×960，小屏构图。
- `assets/github/pipeline.png`与`pipeline-mobile.png`：横向／纵向Agent流程。
- `assets/github/nav-*.svg`：安装、PRD、Agent和文档入口。
- `assets/github/social-preview.png`：1280×640，仓库链接分享封面，小于1MB。
- 对应SVG保留可编辑几何结构；文字已转换为轮廓，避免中文字体在不同环境缺失。实际标题和功能说明仍保留为README文字与图片替代文本。

## GitHub设置

1. 将本公开目录内容上传为仓库根目录，保留assets及docs结构，README引用会使用相对路径。
2. 在仓库Settings的Social preview中选择Edit、Upload an image，上传`assets/github/social-preview.png`。仅把图片提交到assets不会自动设为分享封面。
3. 仓库About可使用：“面向电商内容的Windows桌面剪辑Agent，支持模型规划、本地渲染与检查点恢复。公开文档和演示，核心源码私有。”
4. Topics建议：video-editing、ai-agent、ecommerce、desktop-app、python、tkinter、ffmpeg。
5. 在GitHub预览中核对图片、四个入口、折叠内容和移动端显示，再单独维护软件Releases附件。

本轮只提供本机渲染预览，没有上传仓库或修改GitHub设置。本机预览采用近似GitHub Markdown排版，最终显示仍以GitHub实际渲染为准；它不是另一个公开网站。

## 兼容性

README使用GitHub支持的picture、图片、相对链接、表格和details。图像提供移动端构图及替代文字，PNG避免字体环境差异。页面没有依赖自定义脚本、iframe、网页CSS或在线徽章服务；因此不会把本机预览的外壳样式误当作可以控制GitHub平台的CSS。

官方参考：[README与响应式图片](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/quickstart-for-writing-on-github)、[分享封面设置](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview)。
