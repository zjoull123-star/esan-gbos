# Gate 0–2 治理索引

这些文档定义工程控制、审批边界和待验证证据；它们不是法律意见，也
不替代适用法律、合同或专业顾问的判断。

- [数据治理](data-governance.md)：分类、同意/撤回、保留、删除、导出和法律保全。
- [新加坡跨境检查表](cross-border-singapore-checklist.md)：上线前由隐私/法律负责人逐项确认。
- [威胁模型](threat-model.md)：MCP、webhook、上传、模型和供应链等边界。
- [Gate 2 数据流与容量](gate2-data-flow-and-capacity.md)：四个真相层、
  服务边界、容量假设、失败关闭和 Gate 2 零外部能力。
- [Gate 2–5 测试策略](gate2-test-strategy.md)：测试/证据 owner、负向测试、
  退出条件和 evidence 工作流。
- [Gate 2 证据摘要](../evidence/gate2/gate2-summary.md)：仅设计/schema/mock
  的当前状态、限制和 Go/No-Go 边界。
- [GBOS v4 设计](../superpowers/specs/2026-08-06-gbos-v4-agent-context-roadmap-design.md)：四个真相层、Agent/Context/Metrics 和 Gate 2–6 规范路线。
- [ADR-0009](../adr/ADR-0009-four-truths-agent-context-and-gate-sequencing.md)：金蝶实连延后 Gate 5 及后续门禁顺序。
- [Gate 0/1 漏洞例外](security-exceptions-gate01.md)：精确 PURL、到期和生产阻断。
- [权限矩阵](../permission-matrix.md)：按角色、租户和数据分类的允许/拒绝动作。
- [外部依赖](../external-deps.md)：owner、status、blocking gate 与 evidence。

Gate 0/1 默认关闭生产采集、真实模型调用和真实金蝶帐套访问。Gate 2
仍为设计/mock 且金蝶零网络；Gate 3、4、5 分别验证观察证据、Agent/Context
和 Metrics/金蝶只读预生产。任何开启都需要规范路线指定 Gate 的可复核证据。
