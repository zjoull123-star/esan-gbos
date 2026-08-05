# Gate 0/1 治理索引

这些文档定义工程控制、审批边界和待验证证据；它们不是法律意见，也
不替代适用法律、合同或专业顾问的判断。

- [数据治理](data-governance.md)：分类、同意/撤回、保留、删除、导出和法律保全。
- [新加坡跨境检查表](cross-border-singapore-checklist.md)：上线前由隐私/法律负责人逐项确认。
- [威胁模型](threat-model.md)：MCP、webhook、上传、模型和供应链等边界。
- [权限矩阵](../permission-matrix.md)：按角色、租户和数据分类的允许/拒绝动作。
- [外部依赖](../external-deps.md)：owner、status、blocking gate 与 evidence。

Gate 0/1 默认关闭生产采集、真实模型调用和真实金蝶帐套访问；任何开启
都需要对应 Gate 记录和可复核证据。
