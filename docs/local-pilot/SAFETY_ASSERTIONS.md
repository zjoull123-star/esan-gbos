# Local Pilot Safety Assertions

任何一层不满足都不得解释为“可以启动”。

## NO-CLOUD

- `production_go=false`
- `cloud_server=false`
- `cloud_business_storage=false`
- PostgreSQL、MariaDB 与 filesystem CAS 使用本地独立命名卷。
- MinIO 不属于 required runtime；没有未接线的对象存储服务或控制台。
- `local-internal` 为 `internal: true`。
- `controlled-egress` 只供显式 channel/model profile 使用。
- 远程镜像使用 `@sha256`；Python base、uv builder 和 local runtime 额外固定
  `linux/arm64` 平台并要求实际本机记录。

## NO-KINGDEE

- `kingdee=false`
- Compose 不声明 Kingdee 服务、端点、账号或 secret。
- preflight 对 capability 再次 fail closed。

## NO-OUTBOUND-BY-DEFAULT

- `external_send=false`
- Email、WeCom、WhatsApp、model 与 tunnel 都没有默认 profile。
- kill switch 默认保持 true。
- WeCom standalone CLI 因没有官方 SDK factory 返回 78。
- model projection 因没有有效的人工 attested `trusted_phrase_lexicon` 与真实
  credentials 返回 78。
- Cloudflared 只连接 WhatsApp webhook ingress；未匹配路径返回 404。
- WhatsApp Cloud API 只有 webhook，没有虚构 poller。

## EXACT-INTERNAL-CONNECTIONS

- Frappe BFF 只允许 `observer-api:8003` 和 `agent-api:8002`。
- Frappe site config 只保存 URL、auth-ref 和 token-file path；禁止 token 明文。
- Frappe backend 只读挂载 mode `0600` bearer 文件。
- Agent worker 只允许 `context-api:8001`。
- Materialization worker 只允许 `frappe-backend:8000`。
- `frappe-materializer-bootstrap` 只从 mode `0600` 文件导出一次性 bench env；
  site config 不保存 key/secret。它只允许 exact auth-ref/site/purposes，并创建
  `Website User`、`desk_access=0` 的最小服务身份。
- Projection config 只声明 Observer、Context、Agent 三个独立数据库角色和
  三个不同 password file，不提供管理员连接。

## STORAGE-AND-MONITORING-TRUTH

- evidence truth 是 content-addressed filesystem CAS。
- CAS volume 停止后保留。
- Prometheus 仅抓取自身；空 alerts 不代表业务 metrics 或 SLO 已接线。
- Compose config、静态 healthcheck 声明和空告警都不是运行可用性证据。

## NOT-COMPOSED

- `composition.status=not_composed`
- `local_pilot_go=false`
- Runtime/Frappe 本地镜像尚未实际构建和记录。
- 数据库迁移、Frappe site bootstrap、synthetic 用户和 `/gbos` 尚未实际验证。
- 正式 preflight 必须返回 78；synthetic preflight 不放宽正式门。

## Kill switch 与状态保全

- 紧急停止先写闩锁，再停 tunnel、poller、webhook、model、agent、
  materialization、media 与 Frappe worker/scheduler。
- emergency-stop 不执行 `down`，保留 PostgreSQL 与 filesystem CAS。
- 普通 stop 可以执行 `down`，但禁止 `--volumes`/`-v`。
- 临时 secret 为 `0600`，只在普通 stop 后清理。
