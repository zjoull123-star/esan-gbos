# GBOS 当前交接真相

更新时间：2026-08-27。本文是 current main/feature handoff 的唯一当前状态入口，
用于接手开发与决定是否进入生产测试；不授予外部权限，也不把离线、合成或历史验证
解释为生产发布。

## 交接结论

当前分支 `feat/user-identity-resolution-20260810` 在审计时为 clean，HEAD 与本地跟踪的
`origin/feat/user-identity-resolution-20260810` 均为
`7d97e3dc9f9626e9d4570d6b3509152f6ba0d5b7`（`wip(email-gateway): checkpoint
WeCom inbound integration`）。本轮未修改远端、供应商、凭据、容器或生产状态。

**结论：代码仍是 WIP，不能部署，也未达到生产测试准入。** 通用 Email Gateway/CRM
后端已形成较完整的 provider-free 基础；企业微信入站协议已经冻结，但 webhook、pull、
reconciliation、`45009` 持久暂停桥接和运行时编排仍不完整；PWA 只完成合成测试覆盖的
部分管理与人工收件箱流程。真实邮箱、真实模型、外发与生产证据仍为零。

| 工作面 | 当前状态 | 交接判断 |
| --- | --- | --- |
| Gate 0–6、身份治理、local-pilot 基础 | 历史 credential-free closure 已完成 | 历史证据有效，但不是当前 HEAD 的 live runtime 或生产证明 |
| Email Gateway Tasks 1–5 | 后端/API/数据库合同在离线层已实现 | 当前 Email Gateway 单元测试通过；真实 PostgreSQL 与 provider 未复验 |
| Task 6 企业微信入站合同 | **完成** | 五个关闭 schema、fixture 与官方兼容说明已冻结 |
| Task 7 callback / durable signal | **部分完成** | verifier、signal queue、migration/API 已有；webhook runtime 与编排缺失，且有 2 个 callback 失败 |
| Task 8 pull / reconcile | **部分完成** | provider parser/token/list/read/typed pause 已有；poller、reconciler、真实 transport 和 `45009` 持久暂停桥接缺失 |
| Task 9 shadow ingress proof | **未开始** | 指定的 offline E2E、可执行 runtime chain 与证据包均不存在 |
| Tasks 10–17 CRM/人工流程 | 后端能力较完整，PWA **部分完成** | 不得再表述为完整产品闭环；详见下方前端差距 |
| Task 18 企业微信真实外发 | **阻断** | 官方合同没有稳定 idempotency、receipt/status lookup 或超时对账能力 |
| Task 19 完整发布闭环 | **禁止启动** | Task 18 未解除，且入站 runtime 仍为 red |
| 腾讯云 TKE 密钥适配器 | **设计完成、实现未开始** | 平台托管密钥 → 只读文件挂载 → 统一 Secret Provider 的方向不变 |

## 来源与证据边界

- 规划来源基线是 `8c40731`（观察身份解析 roadmap）。当前审计代码 HEAD 是
  `7d97e3dc9f9626e9d4570d6b3509152f6ba0d5b7`。image lock 仍记录 Frappe source
  reference `35beb2586f12043ce4b89b6875527ec4a75150b9`、runtime source reference
  `1fd20d4df930fc9a70168453d29be1c9dc192522` 和 image-lock recording commit
  `54d9aa7866189d5fe2028aeea177f6cff8102b41`。这些是 2026-08-14 的历史锁定镜像
  边界，不是当前 runtime HEAD 的镜像证明。
- 当前 machine-derived source attestation：Frappe/PWA source SHA256 仍为
  `f6fe3ab3938890e6d041df03bfd5857528c8e1269a631b38d6bbb527978c959d`；local-runtime
  source SHA256 已变为
  `ae14e6c56ab2cd5c2fa5324bb7bc7364ba84bea2b6cf110f74ca0ac3705aa0fc`。因此 Frappe
  source group 仍匹配记录，而 local-runtime 已晚于锁定镜像，必须 clean rebuild、
  inspect、record 与 attestation 后才能启动 canary。
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
- 该身份切片曾从 clean source 完成 governed rebuild/record；当前新增 WeCom runtime
  源码尚未重建镜像。历史绑定不证明真实邮箱、DeepSeek、供应商发送或生产部署。

## 当前验证快照（2026-08-27）

### 源码与测试

- 全仓 `pytest` 在 collection 阶段失败：
  `services/local_pilot_runtime/wecom_app_mail_webhook.py` 不存在，测试无法导入。
- 仅忽略该缺失模块的测试文件后，结果为
  **`4062 passed, 59 skipped, 6 failed, 1 warning`**。六个失败均已定位：
  两个 callback verifier 语义错误（pretty-printed XML 空白、query/event timestamp 被错误
  要求完全相等），以及四个 runtime composition/config/tunnel/secret 接线缺口。
- provider-neutral Email Gateway 聚焦回归为 **`237 passed, 2 skipped`**；两个 skip 是可选
  PostgreSQL 测试，不能当作当前 live RLS 证明。Task 6 合同测试 **`32 passed`**，企业微信
  provider core **`38 passed`**。
- 前端邮件聚焦验证为 **`35 passed`**（Vitest）和 **`4 passed`**（Playwright
  frontend-harness）；v5/BFF/文档聚焦回归 **`122 passed`**。这些是 synthetic/mock
  evidence，不是 authenticated live-site、真实邮箱或生产证据。
- WIP Python 文件 Ruff 通过；Compose 静态渲染通过。migration
  `022_wecom_app_mail_signals.sql` 本轮没有在真实 PostgreSQL 上执行两遍，RLS/grant 只有
  静态与既有测试证据。

### 当前运行环境

- Docker/OrbStack socket 不存在；锁定镜像无法 inspect，GBOS Compose 服务没有启动，
  因而不能声称 synthetic core 当前健康。
- formal preflight 和 synthetic image preflight 均返回 `rc78`。当前 blocker 不只
  `local_pilot_go=false`，还包括 Docker 不可用、required images 不可检查、runtime
  source/image 不匹配；审计时 `127.0.0.1:55432` 也被另一 PostgreSQL 进程占用。
- 主启动器没有选择 Email Gateway profiles；文档中的 Email + DeepSeek canary 仍会走向
  明确拒绝 `email_gateway` manifest 的 legacy poller。因此现有标准启动流程不能执行
  新邮件网关 canary。
- 当前 runtime inventory 中没有企业微信应用邮箱 webhook、puller 或 reconciler；也没有
  `/webhooks/wecom-app-mail` tunnel route、三份 callback secret mount、config renderer、
  status/containment/attestation 接线。

### 历史锁定证据（不可解释为当前 HEAD）

2026-08-14 的 governed rebuild/record 仍可作为历史基线：

| Service | Historical local image digest | Historical revision label |
| --- | --- | --- |
| `frappe-pwa` | `sha256:0b0e24d7e25c2e384e977c1aa00ef8d032e54aadbb84af813fb077c58fd28460` | `35beb2586f12043ce4b89b6875527ec4a75150b9` |
| `local-runtime` | `sha256:489ad22e95300ec27156904d583f67979cf8142f8b31479d8b938ad3d3a6c0b1` | `1fd20d4df930fc9a70168453d29be1c9dc192522` |

锁定 Frappe/PWA source SHA256 label 为
`f6fe3ab3938890e6d041df03bfd5857528c8e1269a631b38d6bbb527978c959d`；历史
local-runtime source SHA256 label 为
`e946cdf903d87b9d387107b82801556ad85994cb7e5702c21854eebda804fd3e`，它与当前
`ae14e6c56ab2cd5c2fa5324bb7bc7364ba84bea2b6cf110f74ca0ac3705aa0fc` 不同。

同一历史快照记录 full backend `3989 passed, 59 skipped, 1 warning`、Ruff format
`720 files`、mypy `151 sources`、frontend unit `232 passed`、Playwright harness
`29 passed`，以及 Observer/Context `3 passed, 16 deselected, 1 warning` 与 Gateway
`2 passed`。这些数值不得再称为当前 HEAD 全绿。

`test:e2e:site` 的 `4 passed, 21 skipped, 0 failed` / `6.5s` 是 2026-08-12
historical-only evidence，**not rerun on the current source-bound images**，也不是全部 25 个
场景的 live 证明。

Full-history Gitleaks exact synthetic allowlist commit 为
`6ee371e164480e967a2b4ffeb48b482e5eab3c97`；historical closure 绑定
`c27687ec6b39e669014b9ae8980cf6565556aaba`。Trivy 和 Gitleaks 必须在最终代码、最终镜像上
重新执行，不能沿用历史 zero/waiver 结论。

既有 Email Gateway 紧急停止边界已补齐：7 个 effect-producing Gateway workers 会在
effect 前检查 `/run/gbos/EMERGENCY_STOP`，containment 保留只读管理 API 以便取证；但未来
新增的企业微信 webhook/puller/reconciler 尚未进入该 inventory，完成运行时接线时必须同步
纳入 startup conflict、status、containment 和 image attestation。

详见 [当前身份治理闭环历史证据](evidence/user-identity-governance-closure/identity-governance-evidence.json)
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

### 企业微信应用邮箱当前状态

- Task 6 已完成。官方材料只证明 JSON `errcode=45009`，没有证明 HTTP 429 或
  `Retry-After`。操作方已明确批准 **“批准 45009 暂停邮箱并由管理员人工恢复”**：只暂停
  受影响邮箱、不推进 cursor/checkpoint、绝不定时或自动恢复，只有 `Integration Admin`
  或 `GBOS Admin` 的受治理命令可以恢复。
- 当前实现只把 `45009` 分类为 typed mailbox pause；通用 audited admin resume 也已存在，
  但两者尚未由 poller runtime 连接。现状不能保证错误发生后持久暂停正确邮箱、保护游标、
  重启后仍保持暂停或从同一管理命令恢复，因此该验收仍未完成。
- Task 7 的 verifier、signal queue、migration 与 Observer acceptance API 已存在，但
  `wecom_app_mail_webhook.py`、Compose/tunnel/config/secret 接线不存在，且 callback 有两个
  已知失败。
- Task 8 的 provider core 已支持 token/list/read、分页、activation fence 和 typed pause；
  `wecom_app_mail_poller.py`、`wecom_app_mail_reconciler.py`、真实 transport 与 claim/ack
  runtime 均不存在。
- Task 9 的 `test_wecom_app_mail_shadow_offline_e2e.py` 和可执行 offline shadow chain
  不存在。
- Task 18 仍被正式阻断：企业微信普通发信没有可证明的 provider idempotency key、stable
  receipt/status lookup 或 uncertain-send reconciliation。Task 19 因此禁止启动，
  `external_send=false` 不变。

### Email Gateway 前端/产品完成度

- 当前 v5 OpenAPI 有 21 个操作；PWA client 只接入 19 个邮箱管理/收件箱操作，专用
  `submit_for_review` 与 `approve` 没有邮件页面入口。当前通用 Review UI 不能替代邮件专用
  审批路径。
- 管理页已支持一次性录入真实邮箱地址、创建默认关闭入站/外发的 mailbox、安全投影、SLA
  版本、enable/pause/revoke、创建规则及安全健康摘要。尚缺邮箱/凭据编辑、规则编辑/停用、
  冲突预览、身份映射队列、callback/cursor/rate-limit、服务端审计、未知发送对账与
  emergency stop。
- 收件箱已支持安全摘要列表/分页、详情、认领、改派、有限状态转换、关联既有业务对象与
  当前页面会话内草稿。尚缺渠道账号负责人、Party/Contact、SLA、identity Review Case、
  thread merge/split、evidence reveal、完整 conversation timeline、已有草稿重载和邮件专用
  送审/批准。现有“首次回复将到期”只是 SLA 排序；“发送失败或不确定”只筛选
  `send_uncertain`。
- 因此 Tasks 10–17 只能标记为“后端切片较完整、PWA partial / synthetic-harness
  verified”，不能标记为完整 CRM 产品闭环。
- `apps/esan_gbos/frontend/README.md` 仍写“仅八个 v1 BFF 方法”，已与 v1–v5 现状不符；
  该文件位于 Frappe image source group，应在下一次受控前端源码切片中修正并随镜像重建，
  不能把旧 README 当作当前合同。

### 仍然有效的外部边界

- 历史 local-pilot Task 13 credential-free closure 已完成；真实 Email + DeepSeek canary
  **未执行**。Keychain metadata/presence 不证明 provider login、内容/schema、有效期或
  observed model identity；本 handoff 不复述、读取或记录任何 secret value。
- 正式 activation-time、repo-external control/manifest、Email checkpoint receipt 与独立
  go 尚未建立。real Email/DeepSeek call、real channels 与
  `response_reported_observed_model=unknown` 保持不变。
- 72 小时连续运行不再作为本阶段退出条件；该窗口 deferred/not required for this stage，
  但真实 UIDVALIDITY、`45009`、超时、重启和断网恢复仍必须做短时、可复核的故障演练。
- Kingdee、cloud、production、外发和正式业务命令继续 No-Go。

## 对齐生产测试前的 P0

以下项目全部完成前，不得创建真实 WeCom callback、不读取真实 mailbox、不运行真实模型，
也不得把 `local_pilot_go` 或 `production_go` 改为 true：

1. 修复 callback 的两个红测，并实现独立 `wecom_app_mail_webhook.py`；补齐单独 callback
   Token、EncodingAESKey、signal bearer 的只读文件挂载和 exact tunnel route。
2. 实现 application-mail transport、poller 与 periodic reconciler，真正消费 durable signal
   的 claim/heartbeat/ack；callback 只能作为 wake hint，不能当 message delivery。
3. 把 `45009` typed pause 与 connector/checkpoint 状态、审计和现有 admin resume 串成一个
   E2E：只暂停单邮箱、不推进 cursor、restart 不自动清除、CAS/idempotency 生效。
4. 实现 Task 9 offline shadow E2E，覆盖 callback replay、漏 callback 后 reconciliation、
   pagination、token refresh、重复 mail、崩溃恢复、activation fence、EML publish 和
   `external_send=false`。
5. 把新 webhook/poller/reconciler 加入 Compose profile、主启动器、config renderer、runtime
   inventory、preflight、status、startup conflict、emergency containment 和 attestation；
   同时修复 Email Gateway profiles 无法从标准启动器启动的问题。
6. 完成 PWA 的生产测试最小闭环：role-faithful 控件、真实 due/failed filter、detail 必要字段、
   草稿重载、identity review、thread/evidence 操作和专用 submit/approve；保留真实 send 禁用。
7. 在可用 Docker/OrbStack 上解决 `55432` 端口冲突，执行 PostgreSQL migration 两遍、RLS/
   least-grant、全仓 pytest、前端 lint/typecheck/unit/build/harness；所有已知红测必须归零。
8. 从最终 clean source 重建 local-runtime 镜像，刷新 image lock，逐项复核 revision/source
   SHA/inspect digest/running-container binding，并重新执行 Gitleaks/Trivy。旧镜像与旧测试数
   只能保留为 historical evidence。

## P0 之后的受控 shadow 测试

1. 仅允许已批准的 Eric 单邮箱主入口，显式 activation-time、no historical backfill；原始
   邮箱地址和客户端专用密码只经 Secret Provider 的只读文件进入进程，不进入仓库、env、
   argv、日志、证据或 Frappe site config。
2. 先做 callback URL verification，再做新测试邮件的 list/read/EML、duplicate/replay、
   reconciliation 与 `45009` 人工恢复演练；任何一步失败立即保持单邮箱 pause。
3. 验证员工/客户身份解析、人工 review、CRM 认领/改派/关联/草稿/审批的 authenticated
   live-site 路径；外发仍为 fake/disabled，不调用企业微信真实发送接口。
4. 生成新的 repo-external evidence package，绑定 HEAD、image lock、manifest、activation
   window、checkpoint、数据库状态、故障演练与零外发证明，再单独申请 local shadow Go。

真实 WeCom outbound 若未来缺少权威 idempotency/receipt/status 合同，继续永久 No-Go。
生产还需另行完成 TKE Secret Provider adapter、workload identity/SSM/KMS 投影、网络与数据库
拓扑、监控告警、备份/PITR/DR、隐私与安全审批、轮换和 60 分钟回滚演练；local shadow Go
不自动转成 production Go。

## 接手顺序

1. 从 `7d97e3d` 开始，先关闭 P0 第 1–5 项的入站 runtime red，不接触真实邮箱。
2. 每个切片先补 failing contract/test，再实现；保持 `external_send=false` 和所有 formal go
   flag 不变。
3. P0 回归全绿后重建/锁定镜像，再准备 Eric 单邮箱的 repo-external shadow manifest；任何
   凭据值由用户直接录入平台/Keychain，不能写入 issue、commit、测试输出或 handoff。
4. 完成受控 shadow evidence 后，提交单独的 local shadow Go/No-Go 审批；生产审批另行处理。

本 handoff 不包含凭据、token、cookie、原始消息、模型响应或生产业务数据。
