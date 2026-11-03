# Framo 自托管模型网关

完整 Worker、原子额度账本、请求恢复、鉴权策略和离线测试在本目录。使用自己的 Cloudflare 账号、域名和模型密钥；不依赖作者的 Cloudflare 网关或付费访问码。

```powershell
cd gateway
npm install
npx wrangler login
npm test
npx wrangler deploy
npx wrangler secret put DEEPSEEK_API_KEY
npx wrangler secret put DASHSCOPE_API_KEY
```

为首次启动准备 `AUTH_CLIENTS`：在本地生成 `jyg_` 加32字节 URL-safe 随机内容的访问码，将原码 SHA256 放在下方 `token_hash`。原码只发给自己的用户，不能提交仓库。通过 `npx wrangler secret put AUTH_CLIENTS` 输入 JSON 数组（替换占位值后使用）：

```json
[{"id":"self-hosted","token_hash":"REPLACE_WITH_64_HEX_SHA256","enabled":true,"expires_at":"2027-01-01T00:00:00Z","daily_units":1000,"max_concurrent":1,"models":["deepseek-flash","qwen3.8-omni-flash","qwen3.8-max"]}]
```

可选 `max_requests` 设置累计请求次数；每日单位在 UTC 零点重置。DeepSeek/Omni 每次1单位、Max每次15单位，失败但已发起的调用也计额度；它不是人民币账单。上游模型名及兼容地址在 `src/policy.mjs`，以提供方当前接口为准。

`scripts/manage_access.py`、`provision_gateway.py` 是 Windows 维护工具源码；首次使用前自行配置 `private/grants.json`、Windows 凭据及 `app/config.native.json`。它们不会附带作者现有授权、密钥或访问码。不要用初始化脚本覆盖已经发放的注册表。

`GET /v1/status` 查询本人额度；`POST /v1/chat/completions` 发起模型调用；恢复接口详见 `src/recovery.mjs`。部署、上游费用和网络可达性由各自部署者负责。Cloudflare 控制台中管理生成的 Durable Object 和日志，勿公开真实请求内容。
