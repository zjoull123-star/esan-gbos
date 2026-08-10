# User identity governance credential-free closure

捕获时间：`2026-08-10T22:27:09Z`。本快照不修改任何历史 evidence，分别绑定：

- Frappe source reference：`28444b8da334c0e3eae2635352e43da4f7d2477b`
- runtime source reference：`094e794971e96be4f3f1078e7c70936130f65387`
- image-lock recording commit：`eb8bb1ebb2c183430ac36ef74cafac09052cf96d`

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

## 验证结果

- Full pytest：`2798 passed, 43 skipped, 1 warning`，failed `0`。
- Domain + contracts：`786 passed`；infra：`173 passed`。
- Frontend unit：`196 passed`；Playwright harness：`25 passed`；lint、typecheck、
  production build 全部通过。
- 隔离 PostgreSQL integration：`43 passed, 1 warning`；临时环境已移除。
- 全新隔离 Frappe v16 site 连续 migrate 两次；身份原生测试 `13 passed`，全 app
  原生测试 `59 passed`；临时容器、网络和卷已移除。
- Ruff check/format、mypy services、compileall、secret scan 全部通过。
- Frappe 镜像 inspect digest：
  `sha256:71d7e7fd074d519b246cc1da7bb72deb97c07bf58ffc2a1946c2abc26576fb34`。
- Local runtime 镜像 inspect digest：
  `sha256:d79fa3982f727b5a47b1783b3731ed153dc07f6a7f1c4a1c81c9b1a5ef407824`。

验证过程中仅为受控镜像构建使用网络，并启动了隔离 PostgreSQL/Frappe 测试容器；
没有 provider/channel network，没有启动正式 local-pilot application stack。所有隔离
测试状态均已移除。

## 延期与剩余外部门

72 小时连续运行按用户决定延期，未执行，且不作为本阶段验收门槛。它的延期不放宽
真实渠道、模型、预算、隐私、外发或生产控制。

后续若要把 `real_email_deepseek_canary` 改为 Go，仍需由操作人员把获批 Email、
DeepSeek、identity HMAC、trusted-phrase lexicon 和 Frappe resolver 凭据通过 macOS
Keychain 提供，并执行仓库外、单 Email instance、激活时间之后、无历史回填的真实
canary。必须记录 IMAP 只读行为、稳定 UID checkpoint、模型响应报告身份、预算、
fatal latch、人工身份审核、重启/故障/撤回/retention 证据。Kingdee、cloud、production、
external send 和正式业务命令仍分别保持 No-Go。
