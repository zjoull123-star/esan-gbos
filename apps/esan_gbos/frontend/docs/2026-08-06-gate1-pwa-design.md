# Gate 1 中文 PWA 设计

## 目标

交付一个移动优先、在线优先的 Vue 3 PWA。它只通过冻结的八个
`/api/method/esan_gbos.api.v1.*` BFF 方法读写 Frappe 数据，并按当前
Frappe session 的角色裁剪工作台导航。

## 架构

- Vue Router 声明五个角色工作台和两个详情路由。
- `frappe-ui` 提供基础 UI 组件；业务外观由本地设计 token 和响应式布局控制。
- typed `fetch` client 仅使用 Frappe session cookie；POST 统一加入
  `X-Frappe-CSRF-Token`、`idempotency_key` 和 `expected_revision`。
- session 与业务响应只保存在 Vue 内存状态；不使用浏览器持久存储。
- Service Worker 使用 `NetworkOnly` 处理 `/api/`，只用 `CacheFirst`
  缓存版本化静态 shell。

## 交互与状态

应用默认显示中文。数据卡先显示中文摘要，再以明确的“原文”和“原始语言”
区块保存证据语义。fixture 模式显示固定“演示数据”标识。加载、空结果、
权限不足、网络/服务错误都有可操作的中文状态和重试或返回入口。离线时只显示
“需要联网”，不展示上一次业务响应。

## 测试

Vitest 覆盖路由、角色导航、BFF 请求与错误解析、离线关闭、摘要/原文呈现和
敏感存储禁用。构建门包含 ESLint、`vue-tsc`、Vitest 和 Vite production build。
