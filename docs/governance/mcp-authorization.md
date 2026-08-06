# MCP 鉴权与最小权限基线

本基线面向未来的远程 MCP 服务，目标协议版本为 `2026-07-28`。Gate 0–1
不启动远程 MCP、不连接真实金蝶，也不把旧的本地 stdio 进程原地升级。
Gate 2 只冻结契约、工具 schema、字段白名单和 mock，并证明金蝶网络调用
为零。协议版本目标不代表服务已经上线或通过安全验收；每个实施 Gate
开始时都要重新核对当前官方规范。

## 冻结的 scope

| Scope | 最早运行 Gate | 用途 | 明确限制 |
|---|---|---|---|
| `kingdee-read` | Gate 5 | 读取受治理的金蝶 metadata 与白名单业务投影 | Gate 2–4 仅 schema/mock；不能保存、提交、审核、反审核、删除或执行任意操作 |
| `gbos-read` | Gate 4 | 读取调用者在当前 site、团队与记录级权限内的 GBOS DTO | 不能把任意 DocType、字段或过滤表达式代理给模型 |
| `gbos-propose` | Gate 4 | 提交内部 AI Draft 或 Review Case 建议 | 只能产生待审内部记录；不能改变正式状态、外发、报价、承诺或订单 |
| `metrics-read` | Gate 5 | 读取定义已冻结且通过治理门禁的 KPI | 不允许任意 SQL/聚合；缺少血缘、新鲜度、覆盖率或对账即返回不可用 |

系统不定义任何金蝶写 scope，也不向模型暴露正式业务 writer。新增 scope
必须重新进行威胁建模、权限矩阵更新、负向测试和人工变更审批。

## 工具 allow-list

Gate 4 可实现 `sales.customer.get`、`sales.opportunity.search`、
`sales.follow_up.propose`、`procurement.requirement.get`、
`procurement.supplier.compare`、`procurement.risk.analyze`、
`context.entity.resolve`、`context.evidence.get` 和
`context.decision.trace`。

Gate 5 才可加入 `metrics.kpi.get` 以及
`kingdee.sales_order.get`、`kingdee.inventory.get`、
`kingdee.receivable.get` 等经过字段与查询预算约束的只读工具。

永久禁止 `arbitrary_sql`、`arbitrary_doctype`、`arbitrary_form_id`、
`direct_database_write` 和任何 Kingdee create/update/save/submit/audit/
unaudit/delete/payment 工具。

## 每请求验证

每个远程请求都必须独立验证：

1. TLS、签名、issuer、audience、到期时间、not-before、client 与 resource。
2. token 的 site、用户、角色、purpose 和最小 scope；不能仅依赖连接建立时
   的一次性校验。
3. 工具、参数 schema、字段 allow-list、记录级权限、数据分类和速率限制。
4. 用户对本次数据共享和工具调用的明确授权；UI 显示服务、工具、数据范围
   和影响，不得以默认勾选替代授权。
5. `request_id`、调用者、scope、工具、目标、结果分类与拒绝原因写入脱敏
   审计；不得记录 access token、密码或 Restricted 原文。

服务拒绝 token passthrough：收到的上游 token 不能未经 audience/resource
校验转交下游服务，也不能把模型或客户端 token 当作金蝶凭据。连接器使用
服务端保管、可轮换、最小权限的独立凭据引用，业务用户看不到明文。

## OAuth、SSRF 与出站控制

- 授权服务器与资源服务器职责分离；采用短时 access token、精确 redirect
  URI、PKCE（适用时）、state/nonce、资源指示和最小 audience。
- 禁止动态地按模型输入获取 OAuth metadata、JWKS、回调或业务 URL。
- 防止 SSRF：scheme/host/port/DNS/IP 均使用 allow-list；解析后再次检查，
  拒绝 loopback、link-local、私网和云 metadata 地址（除非明确属于经过
  审批的内部部署边界）；限制重定向、响应大小、超时和下载类型。
- 金蝶、对象存储和模型供应商分别使用独立出站策略；模型无法通过参数扩大
  host、form、字段或数据范围。
- 错误默认关闭。鉴权、metadata、权限、新鲜度或来源证据失败时不返回正式
  指标，也不退化为匿名或更宽 scope。

## Gate 证据

Gate 证据按能力分开：

- Gate 2：工具 schema、allow-list、scope 设计、mock、写形状拒绝和金蝶
  零网络证据。
- Gate 4：`gbos-read`/`gbos-propose` 的授权流程、正反向权限、工具 sandbox、
  用户审核、审计脱敏和撤销证据。
- Gate 5：`metrics-read`/`kingdee-read` 的真实每请求鉴权、token 重放/混淆、
  audience 错配、SSRF、重定向、出站 allow-list、最小凭据、工具越权和
  预生产审计。
- Gate 6：生产身份、轮换、监控、事件响应和最终批准。

任一对应证据缺失时，该 scope 保持 `No-Go`。

参考：

- [MCP 2026-07-28 specification](https://modelcontextprotocol.io/specification/2026-07-28)
- [MCP security best practices](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices)
