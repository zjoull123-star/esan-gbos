# Gate 6 evidence summary

Status: **Technical Local Go / Production No-Go**.

Gate 0–6 的本地技术实现和机器可验证控制已完成。Gate 6 实现提交
`dd46e5393b714c09fb5b902d4d09f5ba9e05d3cb` 提供发布清单契约、离线预检、
只生成计划的 release/rollback 工具、单租户生产拓扑、site-per-tenant 模板、
SLO/告警策略、恢复与事件响应 runbook，以及隐私、删除、保留、法律保全和
跨境审批契约。

这不是生产发布。真实 Kingdee、腾讯云新加坡预生产、正式 Security Owner
审核、Privacy/Legal 跨境审批、业务 UAT 和双人生产授权均缺失，因此最终
生产结论是 `blocked_external_input`，Production No-Go。

## 已验证的本地控制

- 生产发布输入采用严格 JSON Schema；预检遇到缺项、过期、摘要不一致、
  能力开关开启或审批不足时失败关闭。
- 发布和回滚脚本只输出 dry-run 计划；仓库没有生产执行器，也没有任何
  production mutation 授权。
- 监控策略包含 10 个 SLO 和 16 个告警；12 份 runbook 覆盖备份恢复、PITR、
  区域灾难、事件响应、凭据轮换、隐私请求和模型/连接器停机等流程。
- 7 个隐私契约和 6 个合成示例验证同意撤回、保留、删除、访问导出、法律
  保全与跨境双角色审批；这些合成验证不构成法律批准。
- Frappe/MariaDB 备份恢复后源站与恢复站均有 871 个 DocType；Observer
  PostgreSQL 源库与恢复库均有 19 张业务表和 9 条迁移记录。

## 精确构建与测试结果

- 仓库测试：`942 passed, 12 skipped`；12 个默认 skip 由 Gate 3–5 的专用
  PostgreSQL 命令分别执行，结果为 7、3、2 个通过。
- Frappe `esan_gbos`：34 个测试通过；空白 site 安装四个 App 后连续迁移
  两次成功。
- 前端使用锁定的 pnpm 11.9.0：lint、typecheck、77 个单元测试和生产构建
  通过；Playwright 6 个关键场景通过。
- 精确镜像：
  `sha256:3b103472b2057ca365ff62e71efa02932fabef4151aba67768ab001ac79dd6f8`；
  revision 与实现提交一致，运行时不含 `git` 或 `curl`。
- Trivy 当前数据库下，仓库与精确镜像均为 0 个未豁免 High/Critical；
  Gitleaks 对工作树及 43 个提交均未发现泄漏。

## 独立门禁状态

| Area | State |
|---|---|
| code / local runtime | `go` |
| local backup/restore and operations controls | `go` |
| live Kingdee canary | `blocked_external_input` |
| Tencent Cloud Singapore preproduction | `blocked_external_input` |
| formal Security Owner review | `pending_external_review` |
| privacy and cross-border review | `blocked_external_input` |
| business-owner UAT | `blocked_external_input` |
| two-person production authorization | `blocked_external_input` |
| production | `no_go` |

## 生产限制

没有执行真实 Kingdee 启动、认证、metadata、业务查询或写操作；没有创建或
修改腾讯云资源；没有加载生产凭据；没有接入真实渠道、真实模型、个人信息，
也没有发送外部消息。

本次没有伪造生产 release manifest。仓库仅保留 manifest 契约与明确的
`not_issued` 状态。57 条 Gate 0/1 本地专用漏洞豁免（覆盖 103 个精确 PURL，
到期日 2026-09-30）仍明确阻塞生产，即使当前扫描的未豁免结果为零。

因此，“完成到 Gate 6”准确含义为：Gate 0–6 的本地代码、测试、恢复、发布
控制和治理资产已完成；生产发布必须等待上述外部与人工证据后重新生成并签署
Go/No-Go 证据包。
