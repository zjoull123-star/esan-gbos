# GBOS 当前交接真相

更新时间：2026-08-14。本文是 current main/feature handoff 的可复核状态面，
不授予外部权限，也不把本地验证解释为生产发布。

## 来源与证据边界

- 规划来源基线是 `8c40731`（观察身份解析 roadmap）；当前分支为
  `feat/user-identity-resolution-20260810`。当前 Frappe source reference 是
  `35beb2586f12043ce4b89b6875527ec4a75150b9`，runtime source reference 是
  `1fd20d4df930fc9a70168453d29be1c9dc192522`，image-lock recording commit 是
  `54d9aa7866189d5fe2028aeea177f6cff8102b41`。镜像 labels、源码哈希和 inspect
  digest 已逐项复核；它们包含本轮 Email Gateway、身份投影、人工路由、SLA、草稿
  证据与 terminal-material retention 的 credential-free 实现。后续 handoff 文档不在
  镜像 source group 内。真实 canary 仍受正式 go、供应商合同与外部授权阻断。
- 身份解析离线实现基线 `c98f6a5` 保留为历史里程碑；本轮在其上补齐了真实
  Frappe v16 站点、最终镜像、Prometheus live scrape 与安全扫描证据；这些历史
  真实 Frappe 观察不自动代表当前 HEAD live runtime。
- `docs/evidence/` 中既有 Gate、local-pilot、identity-resolution 和
  `task13-readiness` 文件均是 historical snapshots。**do not modify** historical
  evidence；本次 credential-free closure 写入独立的
  `task13-credential-free-closure` 证据包。`user-identity-governance-closure` 也是
  2026-08-12 historical snapshot，**do not modify**；当前事实由本文、image lock 与
  可重复验证命令共同约束。

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

- `esan_gbos` 当前有 **18 parent + 3 child DocTypes**；新增的 3 个 parent 是
  邮件发送审批、Approved Command 与 Command Publication 的治理记录。
- CEO `before_validate` 自动补齐封闭角色 bundle：`CEO`、`GBOS Admin`、
  `Integration Admin`、`Reviewer`、`System Manager`，并设为 `System User`；
  `after_install`/`after_migrate` 执行同一幂等 backfill。该升权不等于 Restricted
  原文授权。
- DeepSeek gateway 已固定 `https://api.deepseek.com` 与配置模型
  `deepseek-v4-flash`，具备标记化、schema、预算、kill switch 和 no-tools 边界；
  仍没有 real call，也没有 observed response model identity。

### 邮箱主入口身份闭环（2026-08-14）

- Email Gateway 管理页现单独录入一次 `canonical_mailbox_address`。Frappe 在角色、
  团队与负责人校验后把该值瞬时交给 Observer；Observer 使用只读挂载的 32-byte
  `identity_hmac_key` 生成 site/purpose 隔离的 `extid:v1:email:*`。
- 只有 opaque ref 进入 Frappe 幂等 payload、Gateway mailbox、config outbox、v2
  connector projection 与 Observer immutable config revision。原始地址不进入这些
  持久对象，也不进入 mailbox 响应、URL、浏览器存储、审计文本或普通 UI。
- 冻结的 connector projection v1 未改变。legacy NULL 行仍可读，但不能 enable 或
  relay；管理员重新录入地址后才会产生带 opaque ref 的新 revision。缺失身份的旧
  publication 使用固定安全错误码直接 dead-letter，不猜测 backfill。
- Observer participant authority 使用同一 HMAC resolver 重新解析原始 EML，只有唯一
  匹配时才赋予 `mailbox_owner`；缺 key、缺 token、跨 site、token/digest/revision 漂移
  均失败关闭。
- 本轮源码已从 clean source 重建并记录到下列 runtime/Frappe 镜像。此事实只证明
  credential-free 代码与镜像绑定，不证明真实邮箱、DeepSeek、供应商发送或生产部署。

## 当前验证快照

当前 image lock 中的两套本地镜像已分别绑定到其记录时的实际源码：

| Service | Local image digest | Revision label |
| --- | --- | --- |
| `frappe-pwa` | `sha256:0b0e24d7e25c2e384e977c1aa00ef8d032e54aadbb84af813fb077c58fd28460` | `35beb2586f12043ce4b89b6875527ec4a75150b9` |
| `local-runtime` | `sha256:489ad22e95300ec27156904d583f67979cf8142f8b31479d8b938ad3d3a6c0b1` | `1fd20d4df930fc9a70168453d29be1c9dc192522` |

Frappe/PWA 的 source SHA256 label 是
`f6fe3ab3938890e6d041df03bfd5857528c8e1269a631b38d6bbb527978c959d`；local-runtime 的
source SHA256 label 是
`e946cdf903d87b9d387107b82801556ad85994cb7e5702c21854eebda804fd3e`。

上述 source-bound images 已重新构建并记录到 image lock；synthetic core 使用它们
完成受治理重启。正式 `local_pilot_go=false` 不变，真实渠道、模型、外发与 terminal
material deletion 均未启用。

当前 credential-free source verification 为：full backend
`3989 passed, 59 skipped, 1 warning`、failed `0`；唯一 warning 是既有 Starlette
TestClient/httpx deprecation。Ruff check 与 format（`720 files`）、CI-scope mypy
（`151 sources`）、compileall 和 `scripts/dev/secret-scan` 全部 green。Frontend
lint/typecheck/build green、unit
`232 passed`、Playwright harness `29 passed`。当前 migration chain 的一次性 PostgreSQL
`--all` gate 为 Observer/Context `3 passed, 16 deselected, 1 warning` 与 Gateway
`2 passed`；临时容器已清理。隔离 Frappe v16 原生 runner 也在当前代码上 exit `0`。

`test:e2e:site` 的 `4 passed, 21 skipped, 0 failed` / `6.5s` 是 2026-08-12
historical-only evidence；它不是本轮镜像的证明，**not rerun on the current source-bound
images**。当前源码只重新运行了 29 个 frontend-harness 场景，因此不宣称当前镜像已有
authenticated live-site Playwright 证据。

Full-history Gitleaks 使用已审阅且已提交的 exact synthetic allowlist（current commit
`6ee371e164480e967a2b4ffeb48b482e5eab3c97`；historical closure 仍绑定旧的
`c27687ec6b39e669014b9ae8980cf6565556aaba`）；结果必须以本轮最终提交后的重新扫描为准，
不把 worktree 元数据错误或未审阅结果称为 zero。Trivy filesystem scan 与两套当前
locked image scan 均必须 exit `0` 后才可称 current；这里只报告 `0` unwaived
High/Critical、`0` image secrets、`0` misconfigurations。历史 `57` 条 waiver 覆盖 `103`
个 exact PURL，均于 `2026-09-30` 到期，不宣称 total findings 为零。

当前 synthetic core 使用锁定镜像重启且健康；formal preflight 返回 `rc78`，唯一 blocker
是 `local_pilot_go=false`。全新隔离 Frappe v16 site 的邮件/身份原生 runner exit `0`，
临时容器、网络和卷均已移除。Email checkpoint
receipt 仍只允许 source-bound `STATUS_UIDVALIDITY_UIDNEXT`，但真实 Email IMAP login、
checkpoint 与 canary 未执行；本地凭据存在不等于已完成正式 go、activation-time、
checkpoint receipt 或 provider login 验证。DeepSeek real call 未执行，
`response_reported_observed_model=unknown`。

Email Gateway 的紧急停止边界已补齐：`email-send-worker` 与 command-publication relay
在启动前、领取前及 provider/Gateway effect 前动态检查 `/run/gbos/EMERGENCY_STOP`；
containment 同时停止并复核 7 个 effect-producing Gateway workers，保留只读管理 API
用于取证。broken symlink 与无法检查 latch 均失败关闭；此闭环没有改变
`external_send=false` 或任何默认 kill switch。

Earlier source-bound closure snapshot (`2850 passed, 44 skipped, 1 warning`) 与 pre-doc-fix
red (`3060 passed, 44 skipped, 3 failed`, stale-current-doc mismatch only) 均保留在当前
closure evidence，最终 fresh run 已关闭该文档 mismatch。formal `production_go=false`、
`local_pilot_go=false` 与 real Email/DeepSeek No-Go 不变。

本轮 mailbox identity closure 的 fresh source verification 为：全仓 backend
`3630 passed, 48 skipped, 1 warning`、failed `0`；前端 unit `217 passed`、
frontend-harness Playwright `27 passed`，lint/typecheck/build 均 green；Ruff check、
Ruff format（`668 files`）、mypy（`130 sources`）与 compileall 均 green。一次性
PostgreSQL 环境将 Observer 与 Email Gateway migrations（Gateway 001–009）应用两遍，
Observer 3 个聚焦 gate 与 Gateway 2 个真实 repository/RLS 测试通过；隔离 Frappe v16
site 完成安装、连续两次 migrate 与 5 个邮件/身份原生模块测试。两类临时容器、网络、
volume 与运行目录均已清理。该验证没有真实邮箱、模型或 external-send 网络活动。

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

- Email Gateway Tasks 1–5 与 10–17 的 provider-free 合同、数据库、BFF/PWA、worker、
  证据、SLA、人工路由、Send Outbox 与 terminal-material retention 切片已实现并纳入
  当前回归；所有 provider worker 与真实删除仍保持关闭。Tasks 6–9 未实现：企业微信
  官方材料证明的是 JSON `errcode=45009`，不是计划冻结的 HTTP 429/`Retry-After`。
  当前也没有收到精确批准“批准 45009 暂停邮箱并由管理员人工恢复”，不得自行改写
  供应商合同或继续 Tasks 7–9。
- Task 18 仍被正式阻断：现有企业微信普通发信资料没有可证明的 provider idempotency
  key、send receipt/status lookup 或 uncertain-send reconciliation。Task 19 因此禁止启动；
  `external_send=false` 不变。
- 历史 local-pilot Task 13 credential-free closure 已完成；真实 Email + DeepSeek canary
  **未执行**。Keychain 中 Email、DeepSeek、trusted lexicon 与本地运行所需条目已存在，
  但本轮没有读取或记录 secret value；存在性不证明 provider login、内容/schema、有效期
  或 observed model identity。用户已提供试点团队/人员/Party 业务范围，但正式
  activation-time、repo-external control/manifest、Email checkpoint receipt 与独立 go
  尚未建立。real Email/DeepSeek call、real channels 与
  `response_reported_observed_model` 仍为 No-Go/unknown。
- 72 小时连续运行不再作为本阶段退出条件；该稳定性窗口按用户决定 deferred/not
  required for this stage，未执行、未评估，也不再单独阻塞本阶段。真实 UIDVALIDITY、
  429、超时和断网恢复演练仍未执行。
- Kingdee、cloud、production、外发和正式业务命令继续 No-Go。

## 后续实施顺序

1. Frappe source reference `35beb25`、runtime source reference `1fd20d4` 与 current
   image lock `54d9aa7` 已完成 governed rebuild/record；后续 canary 仍必须复核
   revision label、source hash 与 manifest binding。
2. 在不读取/输出 secret value 的前提下，为当前选定的 Eric 主入口生成正式
   activation-time 与 repo-external canary manifest/control；凭据仍只进入 macOS
   Keychain，不写仓库。
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
6. WeCom application-mail ingestion 必须先取得 45009 的精确治理决定；真实 outbound
   必须先取得可审计的 idempotency/receipt/status reconciliation 合同。两者都不得用
   一般“批准”替代。

本 handoff 不包含凭据、token、cookie、原始消息、模型响应或生产业务数据。
