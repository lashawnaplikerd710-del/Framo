# 内部接口与网关契约

本页描述私有实现的集成边界，不提供公开可调用的Framo REST服务。桌面UI通过Python服务方法调用后端；公开仓库不能直接运行这些方法。

## TaskService

| 方法 | 用途与约束 |
|---|---|
| register_asset(input) | 探测媒体并登记来源指纹；执行时长／格式约束 |
| create_job(spec) | 校验任务规格，保存版本绑定并创建任务 |
| start_job(job_id, resume=False) | 在任务锁和阶段检查下执行；不表示立即完成 |
| get_job(job_id) | 查询规格、状态、错误和时间 |
| read_events(job_id, after_sequence=0) | 按序号增量读取阶段事件 |
| list_artifacts(job_id)／get_artifact(artifact_id) | 获取产物索引／校验后的产物信息 |
| cancel_job(job_id) | 请求取消，在安全检查点停止 |
| resume_job(job_id) | 恢复前核验素材与运行时；完成任务不能直接恢复 |
| create_batch(specs, ...) | 创建最多15个独立子任务 |
| batch_status(batch_id) | 汇总子任务和直接输出交付情况 |
| retry_batch_child(batch_id, output_id) | 只对失败／需要处理／待补资源子任务重新排队 |

模型选段的核心字段是既有asset_id、first_unit、last_unit、frame_times_us及选择依据。任意起止秒数不直接获得信任。策略字段包含creation_mode、original_montage_policy、opening_policy；旧任务缺少新字段时不自动改变语义。

## 网关路由

| 路由 | 用途 |
|---|---|
| GET /health | 服务存活信息；不代表模型授权或额度有效 |
| GET /v1/status | 查询当前访问权限与额度状态 |
| POST /v1/chat/completions | 经过鉴权与模型／请求校验的模型请求 |
| GET /v1/requests/{requestId} | 使用恢复协议查询已提交请求状态 |
| GET /v1/requests/{requestId}/result | 取回仍在保留期内的完成结果 |

客户端使用Authorization Bearer访问凭证；请求编号绑定调用身份。新版恢复协议使用X-Recovery-Protocol: 1及持久化请求编号，重复查询不等于再次调用上游。请求ID和摘要冲突不能视为可重试成功。

## 权限与错误

401表示访问被拒绝，可能由码无效、停用或过期导致，需读取实际状态。402可对应每日加权额度或累计次数耗尽。409可能表示请求在处理中、已记录或身份冲突；不能统一盲目重发。

每日单位按模型权重累计，UTC日期切换重置；累计请求次数不每日重置；续期／换码沿用用户ID时不清零累计次数。单次模型请求不是一条视频，视频任务可能产生多次请求。

错误对用户应给中文原因与动作，对维护者保存脱敏类别、阶段和请求编号。真实错误和已知缺陷以[故障排查](TROUBLESHOOTING.md)为准。
