# ESAN GBOS 前端重构设计

- 日期：2026-08-09
- 设计基线：`efc55217c58e83054c1a84fb081d2fdde375420c`
- 状态：用户已确认设计方向，等待书面规格复核

## 1. 目标

将现有 Gate 原型式 Vue PWA 重构为适合经营管理、销售、采购、产品、AI
观察和人工审核的业务操作界面。重构基于最新 ESAN GBOS 前端蓝图，但遵循
“真实能力优先”原则：有安全数据契约的页面提供完整交互，缺少后端契约的能力
只显示明确的待接入状态，不用演示数据伪装成可运行功能。

本轮需要同时满足桌面和移动端：

- 桌面用于经营分析、多记录比较和复杂详情处理。
- 移动端用于今日行动、沟通查看、样品反馈和人工审核。
- 业务权限、证据、数据来源、幂等和在线优先边界不得因视觉重构而放宽。

## 2. 已确认的设计决策

### 2.1 总体布局

采用“深色侧栏经营工作区”：

- 桌面端使用 240px 深色侧栏和 64px 顶栏。
- 平板端使用 72px 导航轨，可展开完整抽屉。
- 移动端使用 56px 顶栏、五项底部导航和“更多”抽屉。
- 页面内容采用流式宽度，不继续使用营销页式居中大画布。
- CEO 和 GBOS Admin 可看到所有授权领域；其他角色只看到所属领域。

### 2.2 设备优先级

桌面与移动端并重。移动端不是桌面布局的简单压缩版：

- 首页先展示待处理事项和今日行动，不缩放桌面大图表。
- 列表在桌面使用固定列，在移动端转换为字段完整的标签行。
- 详情在桌面使用事实区和受控命令区双栏，在移动端按“先事实、后操作”排列。

### 2.3 排版

采用精简整齐版：

- 主界面只保留业务名称、关键数值、状态、负责人、时间和动作。
- 来源、定义版本、策略、EvidenceRef 和完整 lineage 收进紧凑状态条、抽屉或
  disclosure，不重复占用主界面。
- 数值、状态、时间、负责人和动作使用固定网格或列宽。
- 同一数据来源或状态只解释一次。

### 2.4 数据真实性

- 正式 KPI 只能来自 Metrics API，并保留新鲜度、覆盖率、对账和来源链路。
- synthetic/fixture 数据必须持续显示“演示/合成”状态。
- AI Draft、事实提案和置信度不得显示成已确认事实或已执行动作。
- Restricted 原文默认隐藏；摘要不能代替原始证据。
- 候选供应商报价不得包装成客户报价、正式采购价或最终选供结果。
- 暂不提供后端契约的 Opportunity、Quotation、Supplier 360、Context Graph 和完整
  System Admin PWA 不得使用前端模拟业务数据。

## 3. 信息架构

导航按稳定业务领域组织，角色只控制可见性。

| 领域 | 第一轮页面 | 状态 |
|---|---|---|
| 经营管理 | 产品总览、CEO Command Center | 新建角色入口；CEO 指标已有真实 BFF |
| 销售与客户 | Sales Workbench、Customer 360 | 重构已有页面；Customer List 依赖后续最小读契约 |
| 全渠道沟通 | Communication Hub、Observation Detail | 已有 v4 BFF，重构列表和详情 |
| 采购与供应商 | Procurement Workbench、Supplier Comparison | 使用现有询源和候选字段 |
| 产品与样品 | Product & Sample Workspace、Sample Detail | 重构已有页面；实体索引依赖后续读契约 |
| AI 与审批 | AI Draft Workbench、Approval Center、Decision & Evidence | 使用现有 draft/review 能力，不宣称完整执行链 |
| 数据与集成 | Data & Integration | 使用现有连接器状态、控制和模型用量接口 |
| 系统管理 | Frappe Desk 受控入口 | 不复制一套空的 PWA 管理后台 |

待后端契约成熟后再加入：

- Customer List
- Opportunity Detail
- Quotation Builder
- Supplier 360
- Context Graph
- 完整 Decision Trace
- 独立 System Administration PWA

## 4. 路由设计

第一轮保留现有 11 条业务路由并新增角色化产品入口：

```text
/gbos                         产品总览 / 角色入口
/gbos/ceo                     CEO Command Center
/gbos/sales                   Sales Workbench
/gbos/purchase                Procurement Workbench
/gbos/product                 Product & Sample Workspace
/gbos/communications          Communication Hub
/gbos/communications/:id      Observation Detail
/gbos/review                  AI Draft / Approval Center
/gbos/review/:id              Approval Detail
/gbos/party/:id               Customer 360
/gbos/sample/:id              Sample Detail
/gbos/integrations            Data & Integration
```

品牌链接、登录跳转和 PWA `start_url` 不再固定指向 CEO，而是进入 `/gbos`，由当前
Frappe session 的角色跳转到第一个授权工作区。

Standalone Integration Admin 的 Frappe Shell、导航和 API 权限需要统一。Finance
Readonly 是否具有 PWA 页面必须形成显式决定，不能继续出现 API 允许但 Shell/导航
缺失的状态。

## 5. 视觉系统

### 5.1 核心 token

| Token | 值 |
|---|---|
| Canvas | `#F6F8FB` |
| Surface | `#FFFFFF` |
| Sidebar | `#0B1220` |
| Sidebar Hover | `#172033` |
| Primary | `#6C5CE7` |
| Primary Hover | `#5A4AD1` |
| Primary Subtle | `#EFEDFF` |
| Accent | `#0F9F8F` |
| Accent Text | `#0A6F65` |
| Text | `#172033` |
| Muted Text | `#526078`（实浏览器对比度修正） |
| Border | `#E2E8F0` |
| Danger | `#C33D4B` |
| Warning Text | `#8A5A00` |
| Font | `Noto Sans SC`, `PingFang SC`, `Microsoft YaHei`, `system-ui` |
| Control Radius | 14px |
| Card/Dialog Radius | 16px |

桌面控件高度为 36–40px，移动端触控目标至少 44px。标题不继续使用超大宋体或
Georgia，经营数据和标题统一使用中文无衬线字体。

### 5.2 组件边界

建议目录：

```text
src/design/
  tokens.css
  base.css
  utilities.css

src/components/shell/
  AppShell.vue
  WorkspaceSidebar.vue
  AppTopbar.vue
  MobileNavDrawer.vue
  MobileBottomNav.vue

src/components/layout/
  PageHeader.vue
  DashboardTemplate.vue
  OperationalListTemplate.vue
  DetailCommandTemplate.vue

src/components/ui/
  GbosButton.vue
  GbosField.vue
  StatusBadge.vue
  ConfirmDialog.vue

src/components/data/
  MetricTile.vue
  OperationalList.vue
  ObjectSummary.vue
  Timeline.vue
  EvidencePanel.vue

src/components/feedback/
  ResourceBoundary.vue
```

GBOS UI 组件通过本地包装层使用 Frappe UI 公共导出，不继续依赖
`node_modules` 内部源码路径。包装层负责主题、尺寸、错误状态和未来上游版本变化。

## 6. 三类页面模板

### 6.1 Dashboard Template

适用于产品总览和 CEO Command Center。

顺序：

1. 紧凑页头与页面动作。
2. 单行数据模式/新鲜度状态。
3. 对齐 KPI 行。
4. 经营趋势与今日事项。
5. 完整治理详情和 lineage 按需展开。

不可用指标不显示正式数值。AI 观察简报必须标记为非正式观察。

### 6.2 Operational List Template

适用于销售、沟通、采购、产品、AI Draft、审核和集成。

顺序：

1. 页头与主要动作。
2. 搜索、状态、负责人等紧凑筛选栏。
3. 桌面固定列列表。
4. 游标分页。
5. 移动端转换为字段完整的标签行。

业务字段至少包括标题、状态、负责人、截止时间和下一步动作。列表不能继续固定只读
前 20 或 25 条而丢弃 cursor。

### 6.3 Detail Command Template

适用于 Customer 360、Sample、Communication 和 Approval Detail。

桌面：

- 左侧显示对象摘要、业务事实、时间线和证据。
- 右侧 sticky 区只显示当前状态和角色允许的命令。

移动：

- 事实和证据在操作区之前。
- 审批或提交按钮不得悬浮在尚未阅读的证据之前。

每次命令继续验证角色、记录级权限、状态机、revision、幂等键和 CSRF。

## 7. 页面级设计

### 7.1 产品总览

无业务数值的角色化模块入口，显示当前用户可访问领域、待处理入口和系统状态。待接入
能力显示依赖和状态，不展示虚构数量。

### 7.2 CEO Command Center

使用现有 Metrics API。首屏保留紧凑 KPI、数据状态、治理趋势和今日事项；完整定义、
来源链路和对账信息渐进展开。当前 synthetic 模式持续显示固定标识。

### 7.3 Sales Workbench

将现有工作项按负责人、状态、截止时间和关联对象展示。第一轮不伪造销售漏斗、成交
概率或报价。Party/Sample 链接保持可发现。

### 7.4 Procurement Workbench

按询源事件分组需求与候选供应商，显示供应商名称、候选状态、报价快照、币种、交期
和备注。Supplier Comparison 是询源详情内的真实字段对比，不生成综合评分。

### 7.5 Product & Sample Workspace

第一轮展示产品/样品相关工作项与已有样品入口。待 Product Brief/Sample Project 索引
BFF 可用后再升级为实体工作区。

### 7.6 Communication Hub

使用高密度列表或桌面主从布局展示渠道、分类、发生时间、摘要、审核状态、团队和证据
数量。详情统一使用 Evidence Panel；Restricted 原文保持默认关闭。

### 7.7 AI Draft 与 Approval Center

Frappe Review Case 和 Agent draft 必须独立容错：Agent 服务不可用时，已有 Frappe
审核案件仍可查看。列表保留分页；详情展示冻结快照、策略、revision、hash 和证据引用。

### 7.8 Data & Integration

连接器状态、检查点、积压、错误、暂停、恢复、重放和模型用量分别显示。模型用量接口
失败不能使连接器状态整体不可用。UI 不能读取或显示密钥。

## 8. 数据流与错误处理

```text
Frappe session
  → role-aware shell
  → route-level authorization
  → typed BFF client
  → ResourceBoundary
  → page template
```

- API 使用 same-origin credentials 和 `cache: no-store`。
- Service Worker 对业务 API 保持 NetworkOnly。
- 路由变化、离线或组件卸载时清空活动业务响应并忽略迟到请求。
- loading、empty、offline、permission、error 使用统一 ResourceBoundary。
- 错误状态显示中文可操作信息和 request ID，不回显敏感 payload。
- 多服务页面各资源独立容错，不因一个服务失败而隐藏其他可用业务数据。
- 命令冲突返回后刷新当前 revision，不自动重放改变状态的命令。

## 9. 可访问性与响应式要求

- 保留 skip link、唯一 `main`、每页唯一 `h1` 和可见焦点。
- 移动抽屉支持 `aria-expanded`、Escape、焦点圈闭和焦点返回。
- Evidence 组件生成唯一 ID；不得循环复用固定 `summary-title`。
- 原文根据语言设置 `lang` 与 `dir`。
- 错误消息使用 `role="alert"`，成功状态使用 `role="status"`。
- 审核说明和字符要求使用持久帮助文本及 `aria-describedby`。
- 320、375、768、1024、1440px 和 200% 缩放无横向溢出。

## 10. 测试设计

实施采用测试优先的增量切片：

1. 角色 Shell、首页跳转和导航矩阵。
2. ResourceBoundary、skip link、焦点和全部状态。
3. 设计 token、公共组件和三种模板。
4. CEO/指标真实性边界。
5. 销售、采购、产品和沟通只读页面。
6. Sample、Review、AI Draft 和 Integration 命令页面。
7. Service Worker、离线深链和全浏览器存储检查。
8. 当前 Frappe 实站角色验证。

必须通过：

- ESLint
- Vue TypeScript typecheck
- Vitest
- Vite production build
- Playwright harness
- 当前 Frappe site Playwright smoke
- axe Critical/Serious 检查
- 320/375/768/1024/1440px 无横向溢出
- console、pageerror 和未识别 mock API 为零

现有 88 个前端单元测试和 11 条业务路由契约必须保留。Harness mock 改为端点白名单，
未识别请求立即失败。真实站点缺凭据或 base URL 时，正式验收任务应失败而不是把全部
用例静默标记为 skipped。

## 11. 实施阶段

### F1：设计系统与响应式 Shell

建立 token、AppShell、Sidebar、Topbar、移动抽屉和底部导航；统一角色路由与首页入口。

### F2：公共模板与状态边界

建立 ResourceBoundary、PageHeader、三种页面模板、StatusBadge、EvidencePanel 和列表组件。

### F3：真实页面迁移

按 CEO、销售、采购、产品、客户、样品、沟通、审核和集成逐页迁移，移除通用
WorkspaceView/RecordGrid 的错误抽象。

### F4：移动端与无障碍收口

完成移动导航、详情顺序、键盘路径、RTL、唯一 ID、200% 缩放和全部视口矩阵。

### F5：当前实站认证

重建前端镜像，在当前 Frappe site 以 CEO、销售、采购、产品、Reviewer 和 Integration
Admin 验证页面、权限、缓存和控制台状态，并形成绑定当前 HEAD 的证据。

## 12. 非目标

本轮不实施：

- 金蝶只读或写入
- 真实渠道和 DeepSeek 接线
- 云部署
- 自动外发或正式业务命令
- 后端不存在的报价、供应商画像、Context Graph 或系统管理数据
- 将 synthetic 指标包装成正式经营结果

## 13. 完成标准

- 已批准的深色侧栏、桌面/移动并重和精简整齐排版全部落地。
- 现有真实页面均迁移到三类模板，不再依赖展示型大卡片网格。
- 所有角色只能看到并访问授权领域。
- 所有业务数据继续保持在线优先、no-store 和失败关闭。
- 页面缺少后端能力时明确显示待接入，不产生虚假业务数值。
- 完整前端门禁与当前 Frappe 实站验证通过。
