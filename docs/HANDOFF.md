# GBOS 当前交接真相

更新时间：2026-08-10。本文是当前 main/feature handoff 的最小、可复核状态面，
不授予外部权限，不替代新的 runtime 或生产证据。

## 来源与证据边界

- source baseline 是 `8c40731`（`docs(plan): add observation identity resolution
  roadmap`）；当前 feature branch 是
  `feat/user-identity-resolution-20260810`，为该基线的 descendant。
- **Current main evidence** 指当前源码、测试、配置和锁定清单所能证明的边界。它可以
  证明实现/配置存在和 deterministic/mock 检查结果，不能把文件存在、Compose
  解析、镜像 digest 或本地 synthetic run 解释为 live runtime。
- `docs/evidence/` 中的 Gate 与 local-pilot 文件是按各自 source SHA 捕获的
  historical snapshots。它们的摘要、JSON、截图和 `SHA256SUMS` 保持不变；
  **do not modify** historical evidence，也不要把历史 commit 的结果复制成
  当前 HEAD 的验证结论。需要新的验证时，另建绑定当前 source SHA 的 evidence
  package。

## 当前代码事实

### 观察身份与系统用户关系

身份解析实现基线是 `c98f6a5`。这条链路只建立经过治理、可撤回的关联，四种关系
始终独立，**禁止相互推导**：

| 关系 | 权威字段 | 用途 |
| --- | --- | --- |
| 团队数据访问 | `Observation.team_ref ↔ GBOS Team Member.user` | 决定用户能否按团队读取观察记录 |
| 渠道账号负责人 | `Connector Instance.account_user_ref` | 标记内部谁负责该邮箱/企微/WhatsApp 连接器 |
| 沟通参与人 | `Participant.identity_ref` | 保存经 site、purpose、provider 隔离的匿名外部身份 |
| 业务负责人 | `Deal owner / owner_user / assigned_to` | 表达 CRM 业务归属和跟进责任 |

`Participant.identity_ref` 首先进入未解析状态。AI 只能建议同团队 User、Party 或
Contact 候选；正式关联必须经过 `GBOS External Identity` AI Draft、Review Case 和
人工审核。已确认 User 投影可在团队规则之外提供严格的本人访问，但只在解析仍为
confirmed、后台解析任务新鲜且事件团队匹配时有效；撤回、过期、冲突或跨团队投影
均不授权。Party 投影只丰富显示，不改写不可变观察事件。

当前离线身份解析切片已实现并验证：稳定 HMAC 身份、Frappe 权威映射、人工审核、
Observer 投影、持久任务队列、BFF/PWA、内部 worker、指标契约，以及一次性
PostgreSQL 17 中迁移两遍和 14 项 Gate 3 集成测试。离线 E2E 是基于公开组件 seam
的进程内链路，不是已启动真实 IMAP、Frappe 站点和浏览器的物理全链路。

### 身份解析实施状态

- Task 1–3：已完成基线修复、真相交接和闭合契约。
- Task 4–5：实现了 Frappe 权威与最小权限 resolver；本轮未执行真实 Frappe
  bench/site 原生测试，因此该运行边界仍未验证。
- Task 6–11：已完成 Observer 稳定身份、投影、审核、BFF 与 PWA 流程。
- Task 12：离线 fake-transport/component E2E、内部 worker、静态 Prometheus
  抓取契约和告警已实现；Prometheus 镜像、`promtool` 和 live scrape 本轮未运行。
- Task 13：真实 Email + DeepSeek 影子 canary **未执行**；没有载入真实凭据、没有
  真实模型调用，也没有观测模型返回身份。
- 72 小时常驻试点未执行；正式 `local_pilot_go=false`，Kingdee、云、生产和外发
  继续 No-Go。

### Frappe DocType inventory

当前 `esan_gbos` app 有 **15 parent + 3 child DocTypes**。15 个 parent 包含
Gate 4 audit parent `GBOS Review Decision`；3 个 child 是：

- `GBOS Team Member`
- `GBOS Sourcing Candidate`
- `GBOS Informal Evidence Ref`

因此 README 的旧 `13 parent + 2 child` 计数已废弃。库存来自
`apps/esan_gbos/esan_gbos/gbos/doctype/*/*.json`，不是历史 evidence 中的 fixture
数量。

### CEO auto-elevation

`esan_gbos.ceo_access.ensure_ceo_full_access` 在 User `before_validate` 中识别
`CEO`，并自动补齐准确且封闭的 bundle：

```text
CEO_FULL_ACCESS_ROLES = (
    CEO,
    GBOS Admin,
    Integration Admin,
    Reviewer,
    System Manager,
)
```

该 User 同时被设为 `System User`。`after_install` 与 `after_migrate` 会运行同一
幂等 backfill；非 CEO 不会获得该 bundle。auto-elevation 是身份/角色同步，
不是 Restricted 原文授权，不是 production 或 local pilot Go。

### DeepSeek gateway

DeepSeek gateway 已实现并完成配置边界：endpoint
`https://api.deepseek.com`，configured model `deepseek-v4-flash`，本地 tokenization、
schema、budget、kill-switch 和 provider adapter 均有 deterministic/mock 测试。
当前没有 observed real call，也没有 observed response model identity；因此不能
声称 real model 已验证或模型返回了配置身份。

## Runtime 与外部边界

正式 local pilot 仍为 **No-Go**：

```text
production_go=false
local_pilot_go=false
composition.status=not_composed
```

`infra/local/runtime-entrypoints.json` 是声明性入口账本；entrypoint 文件存在、
Compose config 通过或本地 synthetic-core 快照都不改变这三个门。当前 **real channels**、
**real model call/model identity**、Kingdee、cloud runtime 和 production
均未验证，保持关闭或 No-Go；没有 live channel/model/Kingdee/cloud/production
证据。

`infra/local/images.lock.json` 当前记录的本地 inspect digest 是：

| Service | Reference | Recorded local inspect digest |
|---|---|---|
| `frappe-pwa` | `esan-gbos-local-pilot-frappe:2026-08-08` | `sha256:fdbaf8af7da81958de22798e33d9bade3c7c09d57c59faa69d39b56ab4e99542` |
| `local-runtime` | `esan-gbos-local-pilot-runtime:2026-08-08` | `sha256:7f91afbe932cf1a0e55bcb3936b809754084d4aecbe6b7506b90f7a81b58cb93` |

这些是锁定文件中的记录，不是当前机器上服务健康、真实启动或生产发布的
证明。若重建镜像或改变 source SHA，必须生成新的绑定证据，不能覆盖历史快照。

## Handoff checklist

接手者在声明任何新能力前应：

1. 重新确认当前 HEAD、feature descendant、工作区和 `infra/local/images.lock.json`；
2. 运行本文件对应的 governance tests，并检查 `git diff --check`；
3. 对 real channel、real model call/model identity、Kingdee、cloud 或 production
   分别取得新的、source-bound、可审计证据；
4. 保持 `production_go=false`、`local_pilot_go=false`，直到正式门禁和新证据
   明确批准。

本 handoff 不包含凭据、token、原始业务数据或 live service 操作。
