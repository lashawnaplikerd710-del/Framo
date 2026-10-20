# 环境与快速开始

普通使用者按[安装说明](INSTALLATION.md)操作，无需安装Codex、Node或开发用Python环境；正式安装包应包含它所需的本地运行依赖。

本仓库是公开项目材料，不包含agent_core、云端网关源码、模型或应用运行时。克隆后能阅读文档、查看导览、运行文档检查，不能直接启动完整Framo。

```powershell
python scripts/validate_docs.py
```

该命令仅使用Python标准库，检查本地链接、JSON、SVG、文件类型和常见敏感信息模式。它不安装软件、不请求网关，也不能证明应用没有安全问题。

私有开发与打包需要Windows Python/Tk、媒体工具、语音分析／音色资源及私有源码；维护边界见[开发说明](DEVELOPMENT.md)和[部署说明](DEPLOYMENT.md)。不要照搬旧公开目录中已经过时的路径和源码启动命令。
