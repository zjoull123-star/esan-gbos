# User identity governance credential-free closure

捕获时间：`2026-08-12T01:43:10Z`。本快照不修改任何历史 evidence，分别绑定：

- Frappe source reference：`485d3def0ea30ee49a3899d71c10b0787ba0429f`
- runtime source reference：`bb260632ff44c7065a88327f264612139a9070a2`
- image-lock recording commit：`a599a5200e2a8e1b5e42301d74fe8d9d914161c4`
- current Frappe/PWA inspect digest：`sha256:2a0440df614314dec036ecc934e37aa0b3713b8cb8610e3ca2bd8ed69f9187c2`
- current local-runtime inspect digest：`sha256:de037ad28a020689fec8b72f743ad0224afdf5c2ca6856a2ea5568fabd45e568`
- source SHA256 labels：Frappe/PWA `441e33dec9acd744dd1b461ae49e950d18f764f05ae74e90357091a698320405`；
  local-runtime `c23d41903977fb350764ceee8a21efad70ce1079a7b6eed4503a87af3ac37db3`

## 结论

代码与本地、无真实凭据的设计目标闭环已经完成。身份撤回或目标失去资格时会同步、
fail-closed 地阻断旧 Observer 权限；Rejected 映射可以在保留历史审核的前提下按新
revision 重提；Sales 仅能选择 Party/Contact，管理员和 CEO 才能选择 User；PWA 已有
安全审核、撤回、冲突刷新和 rejected 重提；30 天 retention 已有默认关闭、双开关、
有界执行、legal hold、fencing、CAS/vault 清理和告警调度。

```text
credential_free_design_closure=go
real_email_deepseek_canary=no_go
response_reported_observed_model=unknown
local_pilot_go=false
production_go=false
external_send=false
```

真实 Email/DeepSeek canary 未执行。真实 IMAP 连接和真实模型 API 调用均为 `0`，
observed model identity 与 response-reported observed model 均为 `unknown`。这仍是正式
No-Go，不得把源码完成、镜像构建或 synthetic/in-process 验证解释为真实 provider
可用性、生产放行或正式经营指标。

当前 source-bound 镜像已重建并记录，synthetic core 已使用它们重启；这不等于正式
local pilot 启动，`local_pilot_go=false` 仍保持。当前 core 仅包含 Frappe/PWA、Observer、
Context、Agent，channels/models/media/tunnel 均为 disabled。

## 验证结果

- Earlier source-bound closure snapshot (before this image refresh) recorded full pytest：
  `2850 passed, 44 skipped, 1 warning`，failed `0`。
- Domain + contracts：`799 passed`；infra：`179 passed`。
- Frontend unit：`196 passed`；Playwright harness：`25 passed`；lint、typecheck、
  production build 全部通过。
- 较早同一 feature lineage 的完整隔离 PostgreSQL integration：`43 passed, 1 warning`；
  临时环境已移除。当前 runtime source 另在全新一次性 PostgreSQL 中将 Observer
  001–013、Context 001–005、Agent 001–006 连续应用两次，并以三个
  `NOBYPASSRLS` app role 验证 read-only start guard 与 chain 查询；临时环境已移除。
- 全新隔离 Frappe v16 site 使用当前 Frappe 镜像连续 migrate 两次；身份原生测试
  `13 passed`，全 app 原生测试 `59 passed`；临时容器、网络和卷已移除。
- Ruff check/format、mypy services、compileall、secret scan 全部通过。
- Email checkpoint receipt 的 credential binding 通过 HMAC-SHA256 绑定 connector
  account、team、task、host、port、mailbox、folder 与 username，并明确排除 password。
- Machine chain verifier 已闭合校验 Email delivery、Observer observation/participant、
  confirmed identity/active authority、Agent invocation、Context intelligence/draft 和
  Frappe receipt；不接受 free-form observed model。
- Earlier closure scan of the then-current rebuilt images with Trivy 0.73.0 reported `0`
  unwaived High/Critical, `0` image secrets and `0` misconfigurations；57 条历史 waiver /
  103 个 exact PURL 单独显示，不冒充“总发现为零”。The refreshed Frappe image requires a
  new security scan before any real canary.
- 本次 current-truth 文档刷新前，full backend 最新结果为 `3060 passed, 44 skipped,
  3 failed`；三个失败全部是 stale-current-doc mismatch（旧 image lock/digest 与当前
  source-bound 记录不一致），不是应用实现失败。本次更新已修正 current refs、digests、
  source labels 与 closure checksum；focused governance/docs checks 已通过，但 full
  backend 仍需由 root 重新运行，不能提前宣称全绿。

验证过程中仅为受控依赖、镜像构建和安全扫描使用网络，并启动了隔离
PostgreSQL/Frappe 测试容器；没有 provider/channel network，没有启动正式
local-pilot application stack。所有隔离测试状态均已移除。

Formal external manifest 的 source/image/manifest preflight 已使用仓库外临时声明和
占位 Keychain reference 通过，且临时声明已清理。随后执行的 metadata-only Keychain inventory
没有读取或记录 secret value。随后按用户授权创建了三个本地随机凭据：
`identity-hmac-key`、Frappe identity-resolver API key 与 API secret；它们分别使用
独立 256-bit 随机值，长度、字符集和互异性均已验证。目前 17 个固定基础项存在，
仅 `trusted-phrase-lexicon` 仍缺失。
真实 Email 与 DeepSeek 的动态 Keychain reference、activation time、team/account owner、
reviewer 及目标 User/Party 尚未提供，不能由实现自行猜测。

## 延期与剩余外部门

72 小时连续运行按用户决定延期，未执行，且不作为本阶段验收门槛。它的延期不放宽
真实渠道、模型、预算、隐私、外发或生产控制。

后续若要把 `real_email_deepseek_canary` 改为 Go，仍需由操作人员提供获批 Email、
DeepSeek、trusted-phrase lexicon 及上述人工 scope，并通过 macOS Keychain 配置；
随后执行仓库外、单 Email instance、激活时间之后、无历史回填的真实
canary。必须记录 IMAP 只读行为、稳定 UID checkpoint、模型响应报告身份、预算、
fatal latch、人工身份审核、重启/故障/撤回/retention 证据。Kingdee、cloud、production、
external send 和正式业务命令仍分别保持 No-Go。
