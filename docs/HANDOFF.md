# GBOS 当前交接真相

更新时间：2026-08-11。本文是 current main/feature handoff 的可复核状态面，
不授予外部权限，也不把本地验证解释为生产发布。

## 来源与证据边界

- 规划来源基线是 `8c40731`（观察身份解析 roadmap）；当前分支为
  `feat/user-identity-resolution-20260810`，实现与最终镜像验证基线为
  `098d728cc52e27b6f58b051dfeb925efdfc680c4`（`098d728`）。
- 身份解析离线实现基线 `c98f6a5` 保留为历史里程碑；本轮在其上补齐了真实
  Frappe v16 站点、最终镜像、Prometheus live scrape 与安全扫描证据。
- `docs/evidence/` 中既有 Gate、local-pilot 和 identity-resolution 文件均是
  historical snapshots。**do not modify** historical evidence；新的现场验证写入
  独立的 `identity-resolution-runtime` 证据包。

## 已实现的业务与治理边界

### AI 观察身份与系统用户

四类关系始终独立，**禁止相互推导**：

| 关系 | 权威字段 | 用途 |
| --- | --- | --- |
| 团队数据访问 | `Observation.team_ref ↔ GBOS Team Member.user` | 决定团队范围读取 |
| 渠道账号负责人 | `Connector Instance.account_user_ref` | 标记内部连接器负责人 |
| 沟通参与人 | `Participant.identity_ref` | 保存 site/purpose/provider 隔离的匿名身份 |
| 业务负责人 | `Deal owner / owner_user / assigned_to` | CRM 归属与跟进责任 |

参与人默认是 unresolved。AI 只能提出 User、Party 或 Contact 候选，不能确认映射。
正式关联必须经过 `GBOS External Identity` AI Draft、固定 revision/evidence 的
Review Case 和人工决定。confirmed User 投影只有在同 site、同团队、映射新鲜时
才可提供本人访问；revoked、过期、冲突或跨团队投影均不授权。Party 投影只丰富
显示，不改写不可变观察事件。

### 当前功能面

- `esan_gbos` 当前有 **15 parent + 3 child DocTypes**。
- CEO `before_validate` 自动补齐封闭角色 bundle：`CEO`、`GBOS Admin`、
  `Integration Admin`、`Reviewer`、`System Manager`，并设为 `System User`；
  `after_install`/`after_migrate` 执行同一幂等 backfill。该升权不等于 Restricted
  原文授权。
- DeepSeek gateway 已固定 `https://api.deepseek.com` 与配置模型
  `deepseek-v4-flash`，具备标记化、schema、预算、kill switch 和 no-tools 边界；
  仍没有 real call，也没有 observed response model identity。

## 当前验证快照

最终本地镜像都绑定 source revision `098d728`：

| Service | Local image digest | Source digest |
| --- | --- | --- |
| `frappe-pwa` | `sha256:d9220d580ea36fdc04efbe9e11863f2bfb89d879255f52d6af838ee7c0b3cea5` | `1ce0cce76faa93176a0a0bff6cdcf7f6ece3226f16fb1e6ebeec24782a43b7bd` |
| `local-runtime` | `sha256:ceaf2daa0a578698c5f0a2df2d94030b84439b78c9e4a1e73110c4e1a3cf2aae` | `9183102aeacb990dc8b22f4a1f9e3027a70b86c3f453a380fedd9ac20105ba58` |

已观察的当前验证：

- 新建隔离 Frappe site 安装 `frappe 16.30.0`、`erpnext 16.31.0`、
  `crm 1.81.0`、`esan_gbos 0.1.0`；最终 Frappe 镜像运行原生 app 测试
  `58 passed`。
- 本地 synthetic core 已用上述最终镜像重新创建；Frappe/PWA、Observer、Context、
  Agent、MariaDB、PostgreSQL、Redis 均健康，channels/models/media/tunnel 仍关闭。
- 真实 Frappe synthetic 站点 Playwright 为 `4 passed, 18 skipped`；完整响应式、
  身份审核与错误态由前端 harness `22 passed` 覆盖，unit 为 `187 passed`。
- PostgreSQL 隔离矩阵验证了 Gate 3、Gate 4、Gate 5、Media 和 Context；Context
  管理台账只由 owner role 查询，application role 保持最小权限。
- Prometheus 3.7.3 live target `identity-resolution` 为 `up=1`，5 条规则健康；
  `gbos_identity_resolver_ready=0` 是预期结果，因为真实身份 worker/渠道未启用。
- 固定 Trivy 0.73.0 对源码锁文件、两个 Containerfile 和最终两套镜像的结果均为
  0 个未豁免 High/Critical、0 secrets、0 misconfigurations；运行时仍为 Python
  3.14.2，但构建时安装了当前 Debian 12.15 安全更新。

详见 [当前身份解析运行证据](evidence/identity-resolution-runtime/identity-resolution-runtime-summary.md)。

## 正式状态与剩余门

正式 local pilot 仍是 **No-Go**：

```text
production_go=false
local_pilot_go=false
composition.status=not_composed
external_send=false
```

- Task 1–12 的离线、真实本地 Frappe、最终镜像和监控验证已完成。
- Task 13 真实 Email + DeepSeek shadow canary **未执行**；缺少 IMAP 凭据、
  DeepSeek API Key/余额、启用时间、目标 team/account user 和人工批准的 names/org
  lexicon。real channels、real model call/model identity 仍为 No-Go/unknown。
- 72 小时常驻试点及真实 UIDVALIDITY、429、超时、断网恢复演练未执行。
- Kingdee、cloud、production、外发和正式业务命令继续 No-Go。

## 后续实施顺序

1. 用户以安全方式提供 Task 13 外部输入；凭据只进入 macOS Keychain，不写仓库。
2. 创建 repo-external canary manifest，只启用一条 Email instance 和模型投影；正式
   checked-in manifest 不修改。
3. 先做一封新测试邮件，再验证去重、身份 unresolved → review → confirmed/revoked、
   模型标记化、费用台账和 kill switch；不得回补历史。
4. 完成 72 小时常驻、故障演练和新 evidence package 后，才可讨论
   `local_pilot_go`；production Go 仍需独立审批。

本 handoff 不包含凭据、token、cookie、原始消息、模型响应或生产业务数据。
