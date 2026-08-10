# GBOS 当前交接真相

更新时间：2026-08-11。本文是 current main/feature handoff 的可复核状态面，
不授予外部权限，也不把本地验证解释为生产发布。

## 来源与证据边界

- 规划来源基线是 `8c40731`（观察身份解析 roadmap）；当前分支为
  `feat/user-identity-resolution-20260810`，实现与最终镜像验证基线为
  `00a1a0a395d6326688ff131192c9aa332f8d32b1`（`00a1a0a`）。
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

当前本地镜像都绑定 source revision `00a1a0a`：

| Service | Local image digest | Source digest |
| --- | --- | --- |
| `frappe-pwa` | `sha256:94c1bb068a868e0c0c7bb1deda231c2fc5bd13f2928b83036f83802674c5afe6` | `6de42172a68ce9a2f0f7fe9b158a4471d4b5b3a646e33563dedc48e364092e7c` |
| `local-runtime` | `sha256:705012abe856dbe33298e508c79e121831585e1036dca701a93553ebe0186c8b` | `a1e4f3a068ab88c54d3cf7753cfa75147b31843f2799144ab1e3a7e23f497894` |

两套镜像已构建并写入本地 image lock；当前提交尚未用真实凭据启动正式
渠道/模型 profile。下列 synthetic/Frappe/监控观察来自历史证据快照，不能被新镜像
构建动作自动继承为新的运行证据。

已观察的历史运行验证（不自动升级为 `00a1a0a` 的 live proof）：

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
- 固定 Trivy 0.73.0 已在本轮对源码锁文件、两个 Containerfile 和 `00a1a0a`
  两套镜像重新执行；结果均为 0 个未豁免 High/Critical、0 secrets、0
  misconfigurations。历史 57 条豁免/103 个 exact PURL 仍单独显示且不等于消失；
  运行时仍为 Python 3.14.2，构建时安装了当前 Debian 12.15 安全更新。

详见 [当前身份解析运行证据](evidence/identity-resolution-runtime/identity-resolution-runtime-summary.md)。

## 正式状态与剩余门

正式 local pilot 仍是 **No-Go**：

```text
production_go=false
local_pilot_go=false
composition.status=composed
external_send=false
```

- Task 1–12 的离线、真实本地 Frappe、最终镜像和监控验证已完成。
- Task 13 真实 Email + DeepSeek shadow canary **未执行**；缺少 IMAP 凭据、
  DeepSeek API Key/余额、启用时间、目标 team/account user 和人工批准的 names/org
  lexicon。real channels、real model call/model identity 仍为 No-Go/unknown。
- 72 小时连续运行不再作为本阶段退出条件；该稳定性窗口未执行、未评估，也不再
  单独阻塞 local pilot。真实 UIDVALIDITY、429、超时和断网恢复演练仍未执行。
- Kingdee、cloud、production、外发和正式业务命令继续 No-Go。

## 后续实施顺序

1. 用户以安全方式提供 Task 13 外部输入；凭据只进入 macOS Keychain，不写仓库。
2. 创建 repo-external canary manifest，只启用一条 Email instance 和模型投影；正式
   checked-in manifest 不修改。
3. 先做一封新测试邮件，再验证去重、身份 unresolved → review → confirmed/revoked、
   模型标记化、费用台账和 kill switch；不得回补历史。
4. 完成短时健康采样、故障演练和新 evidence package 后，才可讨论
   `local_pilot_go`；证据记录实际运行时长但不要求 72 小时，production Go 仍需
   独立审批。

本 handoff 不包含凭据、token、cookie、原始消息、模型响应或生产业务数据。
