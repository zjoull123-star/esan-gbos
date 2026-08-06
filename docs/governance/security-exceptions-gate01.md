# Gate 0/1 漏洞例外与生产阻断

## 结论

Gate 0/1 只允许在本机回环地址、合成数据、无真实连接器、无真实模型、
无金蝶网络调用的环境中使用当前镜像。当前扫描发现的剩余
High/Critical 项已逐项绑定到精确的 `ID + PURL + 版本`，并设置
`2026-09-30T00:00:00Z` 到期时间；这不是“没有漏洞”的声明。

这些例外只解除 Gate 0/1 本地开发门禁。Gate 5 预生产和 Gate 6
生产发布继续保持 **No-Go**，直到重新扫描后满足以下任一条件：

1. 上游发布兼容修复并完成全量回归；
2. 迁移到经兼容验证且不存在对应问题的基础镜像；
3. 安全/平台负责人基于真实部署边界完成新的、独立的风险处置。

## 责任与审批边界

| 项目 | 记录 |
|---|---|
| 风险责任角色 | GBOS Platform/Security Owner |
| 工程处置 | Gate 0/1 限定例外；生产风险不接受 |
| 复核频率 | 每周、上游版本变更时、进入下一 Gate 前 |
| 最晚到期 | 2026-09-30T00:00:00Z；过期后 CI 自动失败 |
| 回滚条件 | 出现可利用证据、暴露真实数据/公网入口、scope 不再精确匹配时立即停止 |
| 机器策略 | `security/trivy-gate01-ignore.yaml` |
| 策略校验 | `scripts/dev/validate-security-waivers` |

## 已执行修复

- 从 Debian Bookworm 当前安全仓库升级全部 12 个可升级系统包。
- 删除运行镜像中的 Vim 运行/文档包。
- 删除 Frappe、ERPNext、CRM 和 GBOS 前端构建用 `node_modules`。
- 仅保留 Node.js 24.13.0 二进制和 27 个锁定的 Frappe realtime
  运行依赖。
- 将 `socket.io` 固定为 4.8.3，并使 `engine.io`、`socket.io-parser`
  和 `ws` 分别解析为 6.6.9、4.2.7、8.21.2；该依赖树的 npm
  High/Critical 为 0。
- 不使用 `--ignore-unfixed`，也不使用 CVE 全局通配。Trivy 只在
  最终镜像扫描时加载精确 PURL 策略，并显示被抑制项。
- 镜像唯一的 Java archive 是 Debian `gettext-base` 所属的
  `/usr/share/java/libintl-0.21.jar`（约 2.6 KB）；运行时没有 JVM，
  该文件不被执行，所属 Debian 包仍由 OS vulnerability scanner 覆盖。
  为避免安全与 SBOM 流程为这一非运行制品下载约 900 MB Java
  数据库，两条扫描命令都只跳过这一条精确路径，不使用 JAR 通配。

## 剩余例外

### Debian 12.15：85 项

- 20 Critical、65 High。
- Debian 状态全部为 `affected`、`fix_deferred` 或
  `will_not_fix`，当前仓库没有可安装的修复版本。
- 涉及 Perl、glib、curl、nginx、libxml2、zlib 等上游运行依赖。
  强行移除会同时删除 MariaDB/PostgreSQL 客户端、备份恢复能力、
  nginx 或 Frappe 运行依赖，因此本 Gate 不通过破坏兼容性的方式
  制造“零发现”。

### Frappe Python 依赖：18 项

- `cryptography`、`Pillow`、`pypdf` 的修复版本与冻结的
  Frappe v16.30.0 依赖约束冲突，`pip check` 会明确失败。
- `pdfkit` 1.0.0 暂无修复版本；Gate 1 PWA 不暴露用户可控 PDF
  渲染入口。
- `msgpack` 1.1.2 和 `setuptools` 70.3.0 是 Pillow wheel 内嵌
  auditwheel SBOM 的构建组件记录，不是 Frappe virtualenv 中可导入
  的运行分发包；仍保留精确扫描器例外，避免静默删除供应链元数据。

## 强制控制

- 当前环境只监听 `127.0.0.1`，只使用 fixtures 合成数据。
- 生产渠道采集、真实 AI、金蝶实连和外部写操作全部关闭。
- 每个例外必须包含精确 PURL、理由和 RFC3339 到期时间。
- 相同 CVE 出现在新包、新版本或新路径时不会自动被现有策略覆盖。
- 到期、字段缺失、通配 PURL、重复 ID 或缺少 Gate 5/6 阻断说明时，
  校验脚本和 CI 立即失败。

参考：[Trivy filtering and scoped suppression](https://trivy.dev/docs/latest/guide/configuration/filtering/)。
