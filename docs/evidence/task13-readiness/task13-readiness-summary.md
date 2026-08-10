# Task 13 真实 Email + DeepSeek 前就绪证据

捕获时间：`2026-08-10T19:01:53Z`。本证据绑定代码提交
`66a17db87528d67f08bf6692a1f67eb85e03f4e3`，运行镜像绑定
`00a1a0a395d6326688ff131192c9aa332f8d32b1`。两者之间没有
`apps/`、`services/`、`contracts/` 或依赖锁文件差异；较新的提交只涉及本地运行
编排、证据、文档和测试。

## 结论

```text
credential_free_readiness=go
disabled_synthetic_core=go
real_email_deepseek_canary=no_go
observed_model_identity=unknown
production_go=false
local_pilot_go=false
external_send=false
```

72 小时连续运行不再作为本阶段退出条件；本轮不执行也不评估 72 小时稳定性，只记录
实际健康采样。该调整不放宽真实渠道、模型身份、外发、生产或合规边界。

## 已验证

- 全量后端 `2557 passed, 41 skipped, 1 warning`；唯一 warning 是既有
  Starlette TestClient/httpx 弃用提示。
- Ruff、492 文件格式检查、123 个 service 文件 mypy、Python 3.14.2 compileall、
  uv lock 与秘钥扫描全部通过。
- 前端 lint、typecheck、188 个 unit、production build、22 个严格 harness
  Playwright 全部通过；当前 Frappe site live 子集 `4 passed, 18 skipped`。
- 使用当前 Frappe/PWA 镜像创建全新临时 site，安装 Frappe 16.30、ERPNext 16.31、
  CRM 1.81 和 `esan_gbos` 0.1.0，连续迁移两次并运行身份权限原生测试
  `12 passed`；临时 site 与数据库已删除。
- Observer/Context/Agent/Media PostgreSQL 迁移链在本地组合栈连续执行两次，
  checksum ledger 一致。
- 30 天保留策略 dry-run 成功，删除数为 0。证据 CAS 与 tokenizer vault 均由
  非特权 UID/GID `10001:10001`、mode `0700` 持有。
- 真实本地紧急停止演练已验证所有处理目标为 0 个运行服务，随后显式解除锁存并恢复
  synthetic core。
- 仓库外、无凭据故障演练通过：重复 UID、UIDVALIDITY、附件隔离、模型重试/协议失败、
  身份重启与撤销。

## 仍被外部输入阻塞

本机没有 Email credential、DeepSeek API Key、identity HMAC、人工批准的 trusted
phrase lexicon，以及 Frappe identity resolver API key/secret。因此没有生成真实
canary manifest，没有连接 IMAP，没有调用 DeepSeek，也没有观察到返回模型身份。

拿到上述输入后，只允许创建仓库外 manifest，启用一条 Email instance 与模型投影，
验证新邮件、BODY.PEEK、User/Party 人工审核、稳定二次解析、PII 标记化、预算、模型身份
和 live 故障恢复。通过前继续保持 `real_email_deepseek_canary=no_go`；Kingdee、云部署、
生产、外发和正式业务命令全部 No-Go。

机器可读事实见 `task13-readiness-evidence.json`；本目录 `SHA256SUMS` 只覆盖该 JSON
与本摘要，不改写任何历史 Gate 证据。
