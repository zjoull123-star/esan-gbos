# 威胁模型（Gate 0/1 基线与 Gate 2–6 演进）

> 当前记录的是控制设计和待验证证据，不是“已安全”声明。生产采集、
> 真实模型和真实金蝶访问默认关闭。运行证据按能力进入对应 Gate：
> Gate 3 为观察/证据，Gate 4 为 Agent/Context/Decision，Gate 5 为
> Metrics API、金蝶只读 MCP 和预生产。

## 资产与信任边界

- 资产：站点/租户数据、原始通信和证据、事实版本与冲突、决策链和 Agent
  时间线、工作流与审批命令、Metrics 定义与结果、Kingdee 只读快照、
  凭据/密钥、模型提示词/输出、审计日志、依赖与构建产物。
- 边界：浏览器/PWA → Frappe；Frappe ↔ Observer 合同；Observer ↔
  外部 channel/webhook；Observer ↔ Context/Decision；Agent Runtime ↔
  Context/Frappe/Metrics；GBOS ↔ Kingdee MCP/查询；GBOS ↔ 模型供应商；
  Metrics read model ↔ CEO；CI/依赖/镜像 → 运行环境；备份/支持/导出是
  额外边界。
- 主要对手：被盗会话或恶意租户用户、伪造/重放发送方、恶意文件、
  提示词注入、被攻陷供应商/依赖、误配置管理员和越权内部人员。

## 风险与控制

| ID | 路径/威胁 | 影响 | Gate 控制与证据要求 |
|---|---|---|---|
| TM-01 | MCP 被提示词注入或工具描述诱导，触发越权查询/写操作 | ERP 或跨租户数据泄露、正式交易 | Gate 2 只做 schema/mock 且金蝶零网络；Gate 4/5 每请求鉴权、只读 allow-list、拒绝写形状、最小凭据、参数 schema、出站 allow-list 和审计；证据：ADR-0003/0009、负向测试 |
| TM-02 | webhook 伪造、重放、乱序或请求体放大 | 伪造事件、重复处理、资源耗尽 | 提供商签名/HMAC 或 mTLS、时间窗与 nonce、`connector-checkpoint` 重放窗口、限流、大小限制、schema 校验、隔离队列；证据：Gate 3 连接器测试 |
| TM-03 | 上传恶意文件、压缩炸弹、内容类型伪装或嵌入个人数据 | 代码执行、存储耗尽、意外披露 | 隔离区、大小/解压比限制、类型和魔数校验、病毒/沙箱扫描、不可执行对象存储、哈希与证据引用；证据：Gate 3 上传安全测试 |
| TM-04 | 模型读取过量原文、提示词注入、幻觉或输出泄露 | 个人/商业数据外泄、错误状态变化 | 最小上下文、供应商/地域白名单、策略/版本、证据与置信度；Gate 3 仅工具-free 转换/提案，Gate 4 Action Guard + AI Draft + 人工批准；证据：ADR-0004、模型评估 |
| TM-05 | 依赖混淆、镜像漂移、恶意供应链更新 | 构建/运行被植入、不可复现 | immutable commit/digest、锁文件、SBOM/许可证、签名/来源验证、漏洞门禁、隔离 CI；证据：docs/compat、升级记录 |
| TM-06 | Frappe、Observer、Context 或 Agent 的租户边界/引用校验失败 | 跨租户原文、证据、事实或决策泄露 | 每个对象 `site_id`、独立站点/存储前缀/队列、默认拒绝 raw access、哈希校验和交叉站点负向测试；证据：ADR-0002/0005/0009 |
| TM-07 | 被盗会话、break-glass 滥用或导出范围过大 | Restricted 数据泄露 | 短时令牌、MFA/设备策略（由部署确认）、最小权限、审批导出、字段清单、过期、全量审计和告警；证据：权限矩阵与演练 |
| TM-08 | PWA/service worker 或日志缓存敏感内容 | 共享/丢失设备泄露 | 仅缓存静态 shell；API no-store；不写浏览器持久存储；日志脱敏；登出/超时清内存；证据：ADR-0008、浏览器存储检查 |
| TM-09 | 错误实体合并、伪造来源或旧事实污染 Context Graph | Agent 基于错误客户/供应商/时间做建议 | 证据强制、双时间、冲突不覆盖、不确定实体进入 Review Case、关系级 provenance、撤回/更正可传播；证据：Gate 3/4 图与冲突测试 |
| TM-10 | 未审核 Fact/AI Draft 被 Metrics 使用，或指标定义/数据过期 | CEO 看到错误“官方数字” | Metrics Registry、定义版本、只消费已批准 workflow 与 Gate 5 受治理投影、freshness/coverage/reconciliation 失败关闭；证据：Gate 5 指标测试 |
| TM-11 | Agent 重复领取、lease 丢失、无限重试或预算失控 | 重复动作、成本失控、队列阻塞 | PostgreSQL lease + `FOR UPDATE SKIP LOCKED`、幂等键、重试上限、dead-letter、预算/速率限制、人工停止；证据：Gate 4 并发与恢复测试 |
| TM-12 | 外部动作超时后结果未知却被盲目重试 | 重复外发或重复业务副作用 | correlation/idempotency key、`verification_required` 状态、先查询后重试、人工介入；Kingdee V1 无写动作；证据：Gate 4/5 故障注入 |

## 风险处理规则

- “已设计控制”与“已通过运行时测试”分开记录；后者必须链接可重放
  的构建、测试或审计证据。
- 发现控制失效时先暂停相关连接器、模型或导出开关，保留证据并升级
  owner；不要通过隐藏错误或降级版本来报告绿色状态。
- 风险接受、例外和残余风险必须有负责人、范围、到期日和回滚条件。

### Gate 2 机器可读处置记录

Gate 2 的 [security-review.json](../evidence/gate2/security-review.json)
对每个处置强制保存 `risk_id`、`severity`、`owner`、`status`、
`test_refs`、`evidence_refs` 和 `human_review`。`human_review` 至少包含
reviewer role、状态与时间；未完成人审时必须明确为 pending。

状态只描述当前 Gate 的处置：

- `controlled_by_disabled_capability`：真实连接/模型/渠道/运行时未启动，
  对应风险被 Gate 边界阻断，但不表示运行时已经安全。
- `design_verified`：结构化负向测试验证了 schema/mock 设计控制。
- `deferred_to_later_gate`：后续 Gate owner 和证据已指定，当前不能放行能力。
- `open`：没有可接受处置，阻断相应 Gate。

不得把风险 ID、控制名称或 “pass/closed” **字符串**存在当成关闭证据。
Critical/High 至少需要实际 `test_refs`、`evidence_refs` 和人审字段；如果
真实能力未启动，只能记录由 disabled boundary 控制，不能声称通过了
runtime security review。
