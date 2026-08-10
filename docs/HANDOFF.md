# GBOS 当前交接真相

更新时间：2026-08-11。本文是 current main/feature handoff 的可复核状态面，
不授予外部权限，也不把本地验证解释为生产发布。

## 来源与证据边界

- 规划来源基线是 `8c40731`（观察身份解析 roadmap）；当前分支为
  `feat/user-identity-resolution-20260810`。当前 Frappe source reference 是
  `28444b8da334c0e3eae2635352e43da4f7d2477b`，runtime source reference 是
  `094e794971e96be4f3f1078e7c70936130f65387`，image-lock recording commit 是
  `eb8bb1ebb2c183430ac36ef74cafac09052cf96d`。镜像 labels、源码哈希和 inspect
  digest 已逐项复核；后续 handoff/evidence 文档不在镜像内。真实 canary 仍受外部
  凭据与正式 go 门阻断。
- 身份解析离线实现基线 `c98f6a5` 保留为历史里程碑；本轮在其上补齐了真实
  Frappe v16 站点、最终镜像、Prometheus live scrape 与安全扫描证据；这些历史
  真实 Frappe 观察不自动代表当前 HEAD live runtime。
- `docs/evidence/` 中既有 Gate、local-pilot、identity-resolution 和
  `task13-readiness` 文件均是 historical snapshots。**do not modify** historical
  evidence；本次 credential-free closure 写入独立的
  `task13-credential-free-closure` 证据包。本轮不改这些历史文件，另建
  `user-identity-governance-closure` 当前证据包。

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

当前 image lock 中的两套本地镜像已分别绑定到其实际源码：

| Service | Local image digest | Revision label |
| --- | --- | --- |
| `frappe-pwa` | `sha256:71d7e7fd074d519b246cc1da7bb72deb97c07bf58ffc2a1946c2abc26576fb34` | `28444b8da334c0e3eae2635352e43da4f7d2477b` |
| `local-runtime` | `sha256:d79fa3982f727b5a47b1783b3731ed153dc07f6a7f1c4a1c81c9b1a5ef407824` | `094e794971e96be4f3f1078e7c70936130f65387` |

当前 credential-free 设计闭环的 source-bound 验证快照为：full pytest
`2798 passed, 43 skipped, 1 warning`；domain/contracts `786 passed`；Ruff
check/format、mypy、compileall 与 secret scan 全部 green；frontend unit
`196 passed`、Playwright harness `25 passed`、lint/typecheck/build 全部 green。
隔离 PostgreSQL integration 为 `43 passed, 1 warning`；全新隔离 Frappe v16 site
连续 migrate 两次后，身份原生测试 `13 passed`、全 app 原生测试 `59 passed`，所有
临时容器、网络和卷均已移除。模型 fatal latch 的 fail-closed 行为已验证；Email
只允许 source-bound `STATUS_UIDVALIDITY_UIDNEXT` checkpoint/receipt，preflight
要求 receipt，未建立真实 IMAP 连接。machine DB-attested narrow-window canary-chain
verifier 只报告 `response_reported_observed_model`，不接受 free-form observed model。
Governed Trivy 0.73.0 scans of both rebuilt images reported `0` unwaived High/Critical,
`0` image secrets and `0` misconfigurations; historical 57 waiver entries/103 exact PURLs
were displayed separately and are not zero-total-findings claims. Scan services were not
started.

这些结果不等于真实 Email/DeepSeek canary 或正式 Go。当前镜像已完成
governed current-image rebuild/record；数据库隔离矩阵结果见 closure evidence，且
唯一验证 DB/network/volume 已移除。本快照曾使用 governed dependency/image/scanner
network，并启动 isolated PostgreSQL validation/build/scanner containers；没有
provider/channel network，也没有 pilot application services。真实 IMAP/model/external
calls 仍为零。

详见 [当前身份治理闭环证据](evidence/user-identity-governance-closure/identity-governance-evidence.json)
及 [Task 13 历史真实渠道前就绪证据](evidence/task13-readiness/task13-readiness-summary.md)。

## 正式状态与剩余门

正式 local pilot 仍是 **No-Go**：

```text
production_go=false
local_pilot_go=false
composition.status=composed
external_send=false
credential_free_readiness=go
real_email_deepseek_canary=no_go
observed_model_identity=unknown
response_reported_observed_model=unknown
checked_in_email_enabled=false
checked_in_deepseek_enabled=false
```

- Task 1–12 以及本轮身份权限即时撤销、目标动态资格、Rejected 重提、审核/撤回 PWA、
  自动 retention scheduler 和 canary 启动防护均已完成 credential-free 验证；计数见
  当前 closure evidence。Frappe 与 runtime 镜像分别绑定上述实际源码版本，但这仍不
  等于真实 Email/DeepSeek canary 或正式 Go。
- Task 13 的 credential-free closure 已完成；真实 Email + DeepSeek canary **未执行**。
  当前缺少 Email credential、DeepSeek API Key、identity HMAC、人工批准的 trusted
  phrase lexicon，以及 Frappe identity resolver API key/secret。real Email/DeepSeek
  call、real channels 与 `response_reported_observed_model` 仍为 No-Go/unknown。
- 72 小时连续运行不再作为本阶段退出条件；该稳定性窗口按用户决定 deferred/not
  required for this stage，未执行、未评估，也不再单独阻塞本阶段。真实 UIDVALIDITY、
  429、超时和断网恢复演练仍未执行。
- Kingdee、cloud、production、外发和正式业务命令继续 No-Go。

## 后续实施顺序

1. Frappe source reference `28444b8`、runtime source reference `094e794` 与 current
   image lock `eb8bb1e` 已完成 governed rebuild/record；后续 canary 仍必须复核
   revision label、source hash 与 manifest binding。
2. 用户以安全方式提供 Task 13 外部输入；凭据只进入 macOS Keychain，不写仓库。
3. 按 [运行手册](local-pilot/RUNBOOK.md) 创建 repo-external canary dir/control，
   用 activation-time 做 Email STATUS-only checkpoint probe，复制 exact checkpoint
   JSON value 到 Keychain credential 的 `initial_checkpoint`，然后让 canary-preflight
   验证 receipt。
4. 只启动一条 Email instance 与模型投影；用 projection config/window/output 运行
   machine DB-attested verifier，再用 `canary-evidence record --chain-attestation`
   记录响应报告模型身份，不接受自由填写的 observed model。
5. 完成证据绑定的短时健康采样、真实故障演练和新 evidence package 后，才可讨论
   Email + DeepSeek local shadow Go；证据记录实际运行时长但不要求 72 小时。
   正式 `local_pilot_go` 与 production Go 仍需独立审批。

本 handoff 不包含凭据、token、cookie、原始消息、模型响应或生产业务数据。
