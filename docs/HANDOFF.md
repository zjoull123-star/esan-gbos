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
