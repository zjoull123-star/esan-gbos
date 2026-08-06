# Observer（Gate 3 占位边界）

本目录在 Gate 0/1 只记录边界，**不实现服务、连接器、生产摄取或存储**。
生产采集默认关闭。

## Gate 3 允许的职责

- 接收已批准的 email、WeCom、WhatsApp、phone、meeting、file 或
  `manual_import` 输入，并执行认证、限流、大小限制、重放/顺序和隔离。
- 将输入规范化为不可变、带 `site_id`、`consent_basis`、分类、保留类和
  证据引用的 Canonical Observation Event。
- 管理 connector checkpoint、原始对象引用、哈希和保留/删除/legal hold
  回执；向 Frappe 发布引用或待审事实。

## 明确不负责

- 不批准或执行正式 Frappe/ERP 命令，不写 Kingdee，不发送外部消息。
- 不绕过权限把原始通信暴露给 Frappe、模型、导出或日志。
- 不把接入成功、队列健康或模型输出当作业务事实；所有派生内容都须
  经证据与人工审核边界（见 ADR-0004）。

## Contracts

- [`canonical-observation-event`](../../contracts/canonical-observation-event.schema.json)
- [`connector-checkpoint`](../../contracts/connector-checkpoint.schema.json)
- [`evidence-ref`](../../contracts/evidence-ref.schema.json)
- [`extracted-fact`](../../contracts/extracted-fact.schema.json)
- [`draft-mutation`](../../contracts/draft-mutation.schema.json)
- [`approved-command`](../../contracts/approved-command.schema.json)

Gate 3 实施前还需完成连接器威胁测试、同意/撤回、租户隔离、保留/删除/
导出/legal hold、上传隔离和运行时观测的可复核证据。此 README 不表示
任何服务已经存在或可用。
