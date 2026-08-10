# GBOS 观察身份解析本地运行收口证据

捕获时间：`2026-08-10T17:09:57Z`。代码、最终镜像和运行验证绑定
`098d728cc52e27b6f58b051dfeb925efdfc680c4`（`098d728`）。本文件属于新的
`identity-resolution-runtime` 证据包；Gate 0–6、local-pilot 和既有
identity-resolution 历史证据均未改写。

## 结论

身份解析离线实现、真实 Frappe v16 本地站点、最终 source-bound 镜像、隔离
PostgreSQL 矩阵、禁用态 synthetic core、前端与 Prometheus live scrape 已收口。
这只支持以下结论：

```text
offline_identity_resolution=go
source_bound_local_runtime=go_for_disabled_synthetic_only
production_go=false
local_pilot_go=false
composition.status=not_composed
external_send=false
```

Task 13 真实 Email + DeepSeek shadow canary 和 72 小时试点尚未执行，因此正式
local pilot、生产、Kingdee、云部署、外发和正式业务命令继续 No-Go。

## 身份关系边界

四类用户关系保持独立，禁止相互推导：

| 关系 | 权威字段 | 授权含义 |
| --- | --- | --- |
| 团队数据访问 | `Observation.team_ref ↔ GBOS Team Member.user` | 决定团队范围读取 |
| 渠道账号负责人 | `Connector Instance.account_user_ref` | 连接器内部负责人，不等于沟通参与人 |
| 沟通参与人 | `Participant.identity_ref` | 匿名外部身份，默认 unresolved |
| CRM 业务负责人 | `Deal owner / owner_user / assigned_to` | 业务归属，不反推观察身份 |

AI 只能提出 User、Party 或 Contact 候选；正式映射必须经过 AI Draft、固定
revision/evidence 的 Review Case 和人工决定。confirmed User 投影仅在同 site、
同 team 且映射新鲜时提供本人访问；revoked、过期、冲突和跨团队状态均失败关闭。

## 最终镜像与运行

| Service | Image digest | Source SHA-256 |
| --- | --- | --- |
| Frappe/PWA | `sha256:d9220d580ea36fdc04efbe9e11863f2bfb89d879255f52d6af838ee7c0b3cea5` | `1ce0cce76faa93176a0a0bff6cdcf7f6ece3226f16fb1e6ebeec24782a43b7bd` |
| Local runtime | `sha256:ceaf2daa0a578698c5f0a2df2d94030b84439b78c9e4a1e73110c4e1a3cf2aae` | `9183102aeacb990dc8b22f4a1f9e3027a70b86c3f453a380fedd9ac20105ba58` |

- 全量后端：`2492 passed, 39 skipped, 1 warning`，0 failed。
- 新建隔离 Frappe site：Frappe `16.30.0`、ERPNext `16.31.0`、CRM `1.81.0`、
  `esan_gbos 0.1.0`；原生 app tests `58 passed`。
- PostgreSQL 隔离矩阵：Gate 3 `20 passed`，Gate 4 `13 passed`，Gate 5 `2 passed`，
  Media `2 passed`，Context `2 passed`；迁移重复执行、forced RLS 与 owner-role
  台账边界均通过。
- 前端：lint、typecheck、production build 通过，Vitest `187 passed`，
  frontend-harness Playwright `22 passed`；真实 synthetic Frappe site 子集
  `4 passed, 18 skipped`。
- Python：Ruff check 通过，format check 覆盖 482 个文件，mypy 覆盖 121 个
  service 文件；compileall、`uv lock --check` 和仓库秘钥扫描通过。

唯一 pytest warning 是既有 Starlette TestClient/httpx deprecation；不影响通过
结果。隔离 Frappe/PostgreSQL 环境及其测试卷、临时 Trivy cache 和临时浏览器状态
均已删除；主 synthetic core 保留运行，PWA 仍位于 `127.0.0.1:58080`。

## 监控与安全

固定 Prometheus 3.7.3 镜像通过 `promtool` 配置和规则校验。live scrape 中
`identity-resolution` target 为 `up=1`，5 条规则 health 均为 `ok`。
`gbos_identity_resolver_ready=0` 与 `IdentityResolverNotReady` firing 是预期的
禁用态信号：真实 channel 和 identity worker 没有启动。

首次扫描最终 runtime candidate 时，Trivy 发现 42 个 High/Critical，门禁按设计
失败关闭。根因是冻结 Python 3.14.2 基础层中的旧 Debian 包；修复保留 Python
3.14.2，并在构建时应用 Debian 12.15 当前安全更新。重建后：

- final runtime：0 个未豁免 High/Critical，0 secrets，0 misconfigurations；
- final Frappe：0 个未豁免 High/Critical，0 secrets，0 misconfigurations；
- 仓库 lockfile 与两个 Containerfile：0 vulnerabilities、0 secrets、0
  misconfigurations。

历史 Gate 0/1 的 57 条例外 / 103 个 exact PURL 仍保留为治理记录，没有被本轮
静默删除，也不自动构成生产批准。

## 剩余输入与下一步

真实 canary 仍需要用户通过安全渠道提供：

1. IMAP host、port、mailbox、folder、应用专用密码与 activation time；
2. 目标 GBOS team 与 connector account user；
3. 有余额的 DeepSeek API Key；
4. 人工批准的 names/organizations trusted phrase lexicon。

这些值只进入 macOS Keychain，不进入仓库。取得后先执行一封启用时间之后的新测试
邮件，验证去重、unresolved → review → confirmed/revoked、标记化、费用台账、kill
switch、429/超时/断网恢复；再开始 72 小时常驻。完成新证据包前不得修改正式
`local_pilot_go=false`。

机器可读真相见 `identity-resolution-runtime-evidence.json`；在本目录执行
`shasum -a 256 -c SHA256SUMS` 校验证据文件。
