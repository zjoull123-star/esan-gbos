# GBOS 观察身份解析离线收口证据

捕获时间：`2026-08-09T19:18:23Z`。验证实现基线：
`c98f6a51c9f4efa6d05c40e640d19bd23edbaefc`（`c98f6a5`）。这是新的独立证据包，
没有修改 Gate 0–6 或既有 local-pilot 历史证据。

## 结论

本次只判定 **离线身份解析切片 technical Go**。正式 local pilot 仍为 No-Go：
`production_go=false`、`local_pilot_go=false`、
`composition.status=not_composed`、`external_send=false`。

四类用户关系保持独立，禁止相互推导：

| 关系 | 权威字段 |
| --- | --- |
| 团队数据访问 | `Observation.team_ref ↔ GBOS Team Member.user` |
| 渠道账号负责人 | `Connector Instance.account_user_ref` |
| 沟通参与人 | `Participant.identity_ref` |
| CRM 业务负责人 | `Deal owner / owner_user / assigned_to` |

参与人的 provider subject 经过 site、purpose 和 provider 隔离的 HMAC 标记化；AI
只能提出候选，不能确认身份。正式生效必须经过 `GBOS External Identity` AI Draft、
固定 revision/evidence 的 Review Case 和人工决定。撤回、过期、冲突或跨团队投影不会
授予本人访问；Party 解析只丰富显示，不改写不可变观察事件。

## 当前验证快照

- 后端：`2486 passed, 39 skipped, 1 warning`，0 failed。
- 一次性 PostgreSQL 17：Observer 001–010 与 Context 001 按正式台账名称应用两遍，
  Gate 3 完整矩阵 `14 passed, 1 warning`；无数据卷，容器和网络已删除。
- Python：Ruff check 通过，format check 覆盖 480 个文件，mypy 覆盖 121 个 service
  文件，compileall 与仓库 secret scan 通过。
- 前端：lint、typecheck、Vitest `187 passed`、production build 通过；受控
  frontend-harness Playwright `22 passed`。
- 离线 E2E 覆盖 unresolved → review → confirmed → replay/restart → revoked，验证无
  重复 work/projection/draft/review、跨团队和原始身份访问失败关闭。该测试是进程内
  public-seam component E2E，不是物理全链路。

唯一警告是既有 FastAPI TestClient 使用 httpx 的 Starlette deprecation warning。

## 未验证与 No-Go

- Task 13 真实 Email + DeepSeek canary 未执行，真实 IMAP 连接和模型 API 调用均为 0；
  observed model identity 为 unknown。
- 本轮没有执行真实 Frappe bench/site 原生身份测试。
- 固定 Prometheus 镜像本机不存在，因此 `promtool` 和 live scrape 未执行；这里只验证
  配置契约、认证头和静态告警。
- 72 小时常驻、UIDVALIDITY/429/超时等真实故障演练尚未执行。
- Kingdee、云部署、生产、外发和正式业务命令继续 No-Go。
- 没有载入真实凭据、token、原始消息、模型响应或业务数据。

JSON 是机器可读真相源；使用 `shasum -a 256 -c SHA256SUMS` 校验两个紧凑文件。
