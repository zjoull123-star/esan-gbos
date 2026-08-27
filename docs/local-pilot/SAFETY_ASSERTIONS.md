# Local Pilot Safety Assertions

> **2026-08-27 current-audit notice：** 本文中的镜像 digest、运行状态与测试计数是
> historical snapshots。当前 source/image/runtime/P0 真相见 [HANDOFF](../HANDOFF.md)；
> 未完成新的 clean rebuild、attestation 与全绿回归前，不得把下列历史 observation 当作
> 当前可用性或 production Go。

任何一层不满足都不得解释为“可以启动”。

## NO-CLOUD

- `production_go=false`
- `cloud_server=false`
- `cloud_business_storage=false`
- PostgreSQL、MariaDB 与 filesystem CAS 使用本地独立命名卷。
- MinIO 不属于 required runtime；没有未接线的对象存储服务或控制台。
- `local-internal` 使用 bridge 且 `enable_ip_masquerade=false`，不是
  `internal: true`；pwa/context/agent/observer 到 `api.deepseek.com:443` 的实测
  出站均 blocked。`webhook-tunnel` 保持 `internal: true`。
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
- Prometheus 3.7.3 已实时抓取 authenticated `identity-resolution` target，`up=1`，
  五条低基数规则健康；resolver readiness 为 `0`，因为真实 worker/channel 未启用。
- Compose config、静态 healthcheck、一次 live scrape 和健康规则都不是持续运行
  可用性、真实渠道、SLO 达成或 production Go 证据。72 小时窗口已明确延后且不再
  作为本阶段退出条件；只记录实际采样时长。

## COMPOSED-BUT-NO-GO

- `composition.status=composed`
- `local_pilot_go=false`
- 正式 preflight 必须返回 78；synthetic preflight 不放宽正式门。
- 本段 historical snapshot 的 runtime/Frappe 镜像曾构建并记录；这不改变 formal No-Go，也不构成
  生产、真实渠道、DeepSeek、Kingdee 或云 Go。

## SYNTHETIC-CORE-SNAPSHOT

- 仅启动 Frappe PWA、Context、Agent、Observer；channels、models、media、tunnel
  均 disabled。PWA `127.0.0.1:58080`，Context `58001`，Agent `58002`，Observer
  `58003`，PostgreSQL `55432`，MariaDB `53306`，均为 loopback。
- Frappe image lock digest 为
  `sha256:d9220d580ea36fdc04efbe9e11863f2bfb89d879255f52d6af838ee7c0b3cea5`，
  local runtime digest 为
  `sha256:ceaf2daa0a578698c5f0a2df2d94030b84439b78c9e4a1e73110c4e1a3cf2aae`；
  `setup_complete=1`，Frappe/ERPNext/CRM/esan_gbos 为
  `16.30.0`/`16.31.0`/`1.81.0`/`0.1.0`。
- migrations 连续两次 checksum-consistent；materializer bootstrap
  skipped/idempotent；fixture 第二次运行全部 skipped。
- Playwright 以 `synthetic.ceo@example.invalid` 访问 `/gbos/ceo` 成功，显示
  “经营总览”和“演示 / 合成数据”；375/768/1440 无横向溢出，console errors/
  warnings 为 0，21 个静态预缓存条目且 API `cached=false`。
- 首次部分 site 失败目录已恢复性移动到数据卷内
  `.failed-gbos.localhost-20260808T033521`，未删除。

## EVIDENCE-SNAPSHOT-LIMITS

- 证据是本地 synthetic 快照，不是连续稳定性结论或最终签字；72 小时项已延后、
  未评估且不作为本阶段门禁，全量测试结果仍需主代理复跑确认。
- 不得将 synthetic 数据、loopback 健康、浏览器成功或静态检查解释为真实渠道、
  DeepSeek、Kingdee、云部署、生产可用性或外发授权。

## Kill switch 与状态保全

- 紧急停止先写闩锁，再停 tunnel、poller、webhook、model、agent、
  materialization、media 与 Frappe worker/scheduler。
- emergency-stop 不执行 `down`，保留 PostgreSQL 与 filesystem CAS。
- 普通 stop 可以执行 `down`，但禁止 `--volumes`/`-v`。
- 临时 secret 为 `0600`，只在普通 stop 后清理。
