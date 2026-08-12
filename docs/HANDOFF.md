# GBOS 当前交接真相

更新时间：2026-08-12。本文是 current main/feature handoff 的可复核状态面，
不授予外部权限，也不把本地验证解释为生产发布。

## 来源与证据边界

- 规划来源基线是 `8c40731`（观察身份解析 roadmap）；当前分支为
  `feat/user-identity-resolution-20260810`。当前 Frappe source reference 是
  `485d3def0ea30ee49a3899d71c10b0787ba0429f`，runtime source reference 是
  `bb260632ff44c7065a88327f264612139a9070a2`，image-lock recording commit 是
  `a599a5200e2a8e1b5e42301d74fe8d9d914161c4`。镜像 labels、源码哈希和 inspect
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
| `frappe-pwa` | `sha256:2a0440df614314dec036ecc934e37aa0b3713b8cb8610e3ca2bd8ed69f9187c2` | `485d3def0ea30ee49a3899d71c10b0787ba0429f` |
| `local-runtime` | `sha256:de037ad28a020689fec8b72f743ad0224afdf5c2ca6856a2ea5568fabd45e568` | `bb260632ff44c7065a88327f264612139a9070a2` |

Frappe/PWA 的 source SHA256 label 是
`441e33dec9acd744dd1b461ae49e950d18f764f05ae74e90357091a698320405`；local-runtime 的
source SHA256 label 是
`c23d41903977fb350764ceee8a21efad70ce1079a7b6eed4503a87af3ac37db3`。

当前 source-bound images 已重新构建并记录到上述 image lock；当前 synthetic core 也已
使用这两套镜像重启。它只表示本地 Frappe/PWA、Observer、Context、Agent 的 synthetic
core 可重启，不改变正式 `local_pilot_go=false`，也不启动真实渠道或模型。

当前 credential-free P0 验证快照为：full backend `3064 passed, 44 skipped, 1 warning`、
failed `0`；唯一 warning 是既有 Starlette TestClient/httpx deprecation。Ruff check、
Ruff format `528 files`、mypy `101 sources`、compileall 和 `scripts/dev/secret-scan`
全部 green；frontend lint/typecheck/build green、unit `197 passed`、Playwright harness
`25 passed`。一次性无 volume 的 pgvector Gate 3 记录 15 条 migration ledger，迁移应用两次，
integration `17 passed, 1 warning`，容器已移除。

Full-history Gitleaks 扫描 `263 commits`、`0 leaks`，使用已审阅且已提交的 exact synthetic
allowlist（commit `c27687ec6b39e669014b9ae8980cf6565556aaba`）；不把未审阅结果称为 zero。
Trivy filesystem scan 与两套当前 locked image scan 均 exit `0`，这里只报告 `0` unwaived
High/Critical、`0` image secrets、`0` misconfigurations；历史 `57` 条 waiver 覆盖 `103`
个 exact PURL，均于 `2026-09-30` 到期，不宣称 total findings 为零。

当前 synthetic core 使用锁定镜像重启且健康；formal preflight 返回 `rc78`，唯一 blocker
是 `local_pilot_go=false`。全新隔离 Frappe v16 site 连续 migrate 两次后，身份原生测试
`13 passed`、全 app 原生测试 `59 passed`，临时容器、网络和卷均已移除。Email checkpoint
receipt 仍只允许 source-bound `STATUS_UIDVALIDITY_UIDNEXT`，但真实 Email IMAP login、
checkpoint 与 canary 未执行，原因是 missing working client authorization；DeepSeek real call
未执行，`response_reported_observed_model=unknown`。

Earlier source-bound closure snapshot (`2850 passed, 44 skipped, 1 warning`) 与 pre-doc-fix
red (`3060 passed, 44 skipped, 3 failed`, stale-current-doc mismatch only) 均保留在当前
closure evidence，最终 fresh run 已关闭该文档 mismatch。formal `production_go=false`、
`local_pilot_go=false` 与 real Email/DeepSeek No-Go 不变。

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

### 本地试点与未来腾讯云密钥边界

- 当前试点环境仍是 Mac 本机。macOS Keychain 只作为 local-only adapter，现有本地
  密钥不迁入仓库，也不被解释为云端部署凭据。
- 未来正式部署平台已在设计层选择 Tencent Cloud TKE managed cluster；密钥链为
  SSM pinned version → TKE ServiceAccount OIDC/CAM 临时角色 → External Secrets →
  KMS 加密的 Kubernetes Secret → 启动投影容器 → memory-backed `emptyDir` 中 0400
  普通文件 → 应用只读 `/run/secrets` → `MountedFileSecretProvider`。
- 当前只记录方案：`adapter_implementation=not_started`。腾讯云区域、账号、集群、
  CAM 角色、SSM secret/version、数据库、CFS、网络和工作负载均未创建或联系。
- 该选择不改变 No-Go：`production_go=false`、`local_pilot_go=false`。未来必须另行
  完成 TKE 全栈架构、最小权限、真实投影、故障、轮换、回滚、备份、监控、隐私与
  release approvals。

详见 [腾讯云 TKE 密钥投影设计](superpowers/specs/2026-08-11-gbos-tencent-tke-secret-projection-design.md)
与 [部署密钥生命周期](deployment-secrets.md)。

- Task 1–12 以及本轮身份权限即时撤销、目标动态资格、Rejected 重提、审核/撤回 PWA、
  自动 retention scheduler 和 canary 启动防护均已完成 credential-free 验证；计数见
  当前 closure evidence。Frappe 与 runtime 镜像分别绑定上述实际源码版本，但这仍不
  等于真实 Email/DeepSeek canary 或正式 Go。
- Task 13 的 credential-free closure 已完成；真实 Email + DeepSeek canary **未执行**。
  当前缺少 Email credential、DeepSeek API Key 和人工批准的 trusted phrase lexicon，
  且人工业务 scope 尚未提供。real Email/DeepSeek
  call、real channels 与 `response_reported_observed_model` 仍为 No-Go/unknown。
- 2026-08-11 的只读存在性审计没有读取任何 secret value。随后按用户授权创建了
  `identity-hmac-key` 与两项 Frappe identity-resolver 凭据；三个本地随机凭据分别使用
  独立 256-bit 值，格式和互异性已验证。目前 17 个固定基础 Keychain 项存在，仅
  `trusted-phrase-lexicon` 不存在。Email/DeepSeek 的动态 Keychain reference、activation
  time、team/account owner、reviewer 与目标 User/Party 尚未提供。使用占位 reference 的
  仓库外 formal manifest source/image preflight 已通过并清理临时声明；这不等于凭据或
  真实 provider 已验证。
- 72 小时连续运行不再作为本阶段退出条件；该稳定性窗口按用户决定 deferred/not
  required for this stage，未执行、未评估，也不再单独阻塞本阶段。真实 UIDVALIDITY、
  429、超时和断网恢复演练仍未执行。
- Kingdee、cloud、production、外发和正式业务命令继续 No-Go。

## 后续实施顺序

1. Frappe source reference `485d3def`、runtime source reference `bb260632` 与 current
   image lock `a599a520` 已完成 governed rebuild/record；后续 canary 仍必须复核
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
