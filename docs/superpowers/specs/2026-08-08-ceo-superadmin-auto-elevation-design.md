# CEO 自动超级管理员设计

## 目标

任何现有或未来获得 `CEO` 角色的 Frappe 用户，都自动获得完整的 GBOS 与
Frappe 管理权限。授权保持个人用户身份，不共用 `Administrator`。

## 权限组合

CEO 用户必须同时拥有：

- `CEO`
- `GBOS Admin`
- `Integration Admin`
- `Reviewer`
- `System Manager`

`GBOS Admin` 覆盖销售、采购、产品、CRM 与 GBOS DocType 的读写创建删除；
`Reviewer` 覆盖审核命令；`Integration Admin` 明确表达连接器管理责任；
`System Manager` 开放 Frappe Desk、用户、角色、DocType 与系统设置管理。

## 自动同步

- User 在 `before_validate` 阶段只要包含 `CEO`，就补齐缺失角色并设为
  `System User`。
- `after_install` 与 `after_migrate` 扫描已有 CEO 用户并幂等回填。
- 无 CEO 的用户不发生变化。
- 本阶段不自动撤销已经授予的角色；CEO 离任时必须由管理员执行显式
  去授权流程，避免误删同时由其他职责人工授予的 System Manager 权限。

## 审核管理覆盖

现有审核 BFF 对普通 Reviewer 保持“只能查看和决定分配给自己的案件”。
`GBOS Admin` 可查看全部待审案件并对任意案件作出管理员决定，使自动升权后的
CEO 能在 PWA 完成完整审核流程。决定仍产生原有审计记录、版本校验与幂等记录。

## 保持关闭的边界

超级管理员授权不移除以下产品安全闸门：

- ERPNext 销售订单、采购订单、库存和财务交易创建仍被 V1 guard 拒绝。
- 金蝶、真实渠道、DeepSeek、自动外发和云部署仍由独立 kill switch 与
  local-pilot manifest 控制。
- API 密钥和服务账号秘密不进入 PWA，也不会复制给 CEO 用户。

## 验证

- 纯单元测试证明 User 保存与迁移回填幂等、非 CEO 不变。
- 权限测试证明复合 CEO 角色拥有 GBOS/CRM 全权，普通 CEO 单角色基线不被
  偷偷改写。
- 审核 API 测试证明 Reviewer 仍受分配约束而 GBOS Admin 可全局处理。
- 全量 Python、前端测试、类型、格式与构建通过。
- 重建本地 Frappe 镜像后，在 `gbos.localhost` 读取真实
  `synthetic.ceo@example.invalid` 角色并验证 PWA 与 Desk 权限。
