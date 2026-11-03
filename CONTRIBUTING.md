# 参与 Framo 开源项目

欢迎提交 Issue 和 PR。核心代码在 `app/agent_core`，媒体工具在 `app/toolchain/scripts`，云端网关在 `gateway/src`。

1. 按 [源码运行说明](docs/OPEN_SOURCE_SETUP.md) 配置自己的依赖。
2. 保持功能范围清晰，更新相关测试和文档。
3. 运行 `python scripts/validate_docs.py`、`python scripts/run_core_tests.py` 和网关离线测试。
4. 不提交真实密钥、访问码、用户素材、数据库、生成成片或依赖目录。

第一方贡献按仓库 MIT 许可提供。引用第三方实现时保留许可与修改说明。环境测试、付费模型调用和完整成片验收请分别说明；不要把离线通过当作完整质量保证。
