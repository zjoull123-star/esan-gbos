# ESAN GBOS Gate 1 PWA

Vue 3 + TypeScript 的中文、在线优先 PWA。生产应用只调用
`contracts/bff-v1.openapi.json` 冻结的八个 BFF 方法；Playwright
`frontend-harness` 中的 route mock 只用于前端 CI，不进入生产 bundle。

## 本地门禁

```bash
pnpm install --frozen-lockfile --ignore-scripts
pnpm run lint
pnpm run typecheck
pnpm run test:unit
pnpm run build
pnpm run test:e2e
```

`test:e2e` 启动本地 Vite preview，使用内存 synthetic Frappe session 和
内存 BFF 响应，检查五个工作台 axe、375/768/1440 横向溢出、键盘顺序、
离线关闭及浏览器持久存储。

真实 Frappe site 是可选项目，不在无凭据 CI 中自动运行：

```bash
GBOS_E2E_BASE_URL=https://synthetic-site.example.invalid \
GBOS_E2E_STORAGE_STATE=/secure/path/synthetic-user-state.json \
pnpm run test:e2e:site
```

`GBOS_E2E_STORAGE_STATE` 必须是测试人员在仓库外维护的 synthetic 用户已登录
Playwright state；不得把账号、cookie 或 state 文件提交到本目录。

## Frappe shell 集成点

前端唯一产物目录是 `frontend/dist/`。Frappe app/容器集成需要在前端范围外完成：

1. 运行 `pnpm --dir frontend build`，再将 `frontend/dist/` 原样复制到
   `esan_gbos/public/frontend/`。
2. `/gbos/*` 全部返回同一个正式 shell。shell 读取
   `dist/.vite/manifest.json` 的 `index.html` entry，按 manifest 加载 CSS、
   modulepreload 和入口 JS。
3. 在入口 JS 前提供当前请求的内存 bootstrap；JSON 必须用安全的 script-data
   转义，不能把 session 写入浏览器持久存储：

   ```html
   <script id="gbos-bootstrap" type="application/json">
     {"user":"synthetic@example.invalid","roles":["Sales User"],"csrf_token":"..."}
   </script>
   ```

4. shell 加载
   `/assets/esan_gbos/frontend/registerSW.js`。该文件注册
   `/assets/esan_gbos/frontend/service-worker.js`，scope 为 `/gbos/`。
   Service Worker 响应必须带 `Service-Worker-Allowed: /gbos/`；Vite preview
   已声明同一 header，Frappe/反向代理仍需单独配置。
5. `bench build --app esan_gbos` 默认不会自动发现这个嵌套 Vite 项目。需要在
   app 根构建脚本或容器构建阶段调用上述 pnpm build + copy；当前前端范围没有
   修改 Frappe hooks。

Service Worker 对 `/api/` 使用 `NetworkOnly`，只对版本化静态 shell 使用
`CacheFirst`。业务响应仅保存在活动 Vue 视图内存中；离线、卸载或会话清除时
立即清空。
