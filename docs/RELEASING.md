# GitHub发布流程

## 公开目录

仅上传本交付的 `github` 文件夹内容，作为仓库根目录。与其并列的private-review是本地核验材料，不上传。不要直接上传整个开发目录、旧公开目录或用户工作目录。

本仓库包含README、产品／技术文档、脱敏证据、真实界面截图和文档校验脚本。应用核心源码保持私有；不创建一个标为MIT的开源仓库来暗示已经授权软件源码。

## 上传前

```powershell
python scripts/validate_docs.py
```

再检查截图、Git历史和待上传文件列表。常见凭据扫描不能代替人工查看。首次本地初始化可以先使用 `git init`、`git add .`、`git diff --cached --stat` 检查，再提交；不要在确认文件范围之前推送开发目录。

## 仓库信息

建议名称：Framo。About文案：

> 面向电商视频生产的Windows桌面剪辑Agent，支持模型规划、本地渲染、原声长视频混剪、批量任务与检查点恢复。公开文档与演示，核心源码私有。

建议主题：video-editing、ai-agent、ecommerce、desktop-app、python、tkinter、ffmpeg、workflow-automation。

使用GitHub网页新建仓库后，上传这个公开目录中的内容，保留assets、docs、evidence、examples、scripts与.github结构。没有本地Git环境也可以按网页上传文档；单文件限制见官方资料。提交完成后检查README图片、相对链接与文档校验结果。

## 安装文件发布

安装文件与文档版本分离。正式软件发布前先处理KI-01和KI-02，完成干净环境验证，再在Releases上传实际安装程序、校验值、发行说明及许可证相关资料。

不能把本轮文档快照标记成已经重新构建的Framo安装版本，也不能让“下载”链接指向不存在的安装文件。当前的安装入口文档明确说明了这一状态。

建议每个软件Release提供：安装程序、SHA256文本、版本和模块快照摘要、支持系统、升级注意事项、实测范围、已知问题及第三方声明。安装包不含访问码和开发者的任务记录。

## 发布后核对

用未登录访问者视角打开仓库，确认能看说明、导览和Release；真正下载并核对安装文件。仓库发布、Release上传、新电脑安装及完整视频生成分别记录，不混为同一验收结果。

## 官方参考

- [GitHub README与相对链接](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)
- [GitHub大文件与Release分发](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)

本轮没有创建仓库、推送代码、上传附件或发布Release。

仓库附带只读权限的文档检查workflow，使用[官方checkout动作](https://github.com/actions/checkout)并执行标准库校验脚本。本轮只在本机执行相同脚本，尚未运行远端GitHub Actions。
