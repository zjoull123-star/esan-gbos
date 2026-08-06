# ESAN GBOS

ESAN Global Business Operating System 是面向全球销售、产品打样、采购协同、
AI 观察和经营洞察的治理型业务前台。Gate 0/1 复用 Frappe CRM 的
Organization、Contact、Lead 和 Deal；金蝶仍是订单、库存与财务权威系统。

## 当前交付边界

本仓库当前只实现 Gate 0 与 Gate 1：

- 冻结的 Frappe v16、ERPNext v16、Frappe CRM v1 兼容基线；
- `esan_gbos` Frappe App、13 个父 DocType 与 2 个 Child DocType；
- 销售 → 产品需求 → 样品 → 反馈 → 需求 → 询源 → 工作项/审核闭环；
- 版本化 BFF、团队级权限、revision、幂等与服务端状态机；
- 中文响应式 PWA 和确定性合成 fixtures；
- 治理、共享契约、本地 Compose、CI、安全与 Gate 证据。

以下能力保持关闭或不存在：真实金蝶连接及写入、生产渠道采集、真实 AI
模型调用、自动外发、自动报价、正式订单创建和腾讯云部署。Observer
profile 只是 Gate 3 的隔离占位，不是 Gate 1 运行依赖。

Gate 1 的 CEO 页面读取确定性合成数据，只是已交付的工作台与交互基线，
不是 Gate 5 的 Metrics API、正式 CEO 驾驶舱或实时经营证据。

## 后续路线

规范路线以
[GBOS v4 设计](docs/superpowers/specs/2026-08-06-gbos-v4-agent-context-roadmap-design.md)
和
[ADR-0009](docs/adr/ADR-0009-four-truths-agent-context-and-gate-sequencing.md)
为准：

- Gate 2：冻结 Agent、Context、Metrics 和金蝶字段/接口契约与 mock；零实连。
- Gate 3：建设渠道观察、不可变证据、事实提案和最小 Context Service。
- Gate 4：建设持久 Agent Runtime、Context/Decision、Action Guard 和人工审核。
- Gate 5：建设 Metrics API、正式 CEO 驾驶舱、金蝶只读 MCP 实连及预生产。
- Gate 6：完成生产安全、隐私、恢复、运维和 Go/No-Go。

Gate 0/1 证据中的旧 downstream planning note 是当时的历史快照，不再作为
Gate 2–6 的执行顺序；证据文件和校验和保持不变。

## 本地启动

前置条件：

- macOS ARM64；
- OrbStack 已安装并完成首次 GUI 启动；
- Git 工作区处于已提交状态；最终镜像只接受可追溯的 clean commit。

首次启动只需：

```bash
scripts/dev/bootstrap
```

命令会在缺少 `infra/dev/.env` 时从合成示例创建权限为 `0600` 的本地配置，
校验生产开关、按需构建锁定版本的最终镜像，并在
[http://gbos.localhost:8080/gbos/ceo](http://gbos.localhost:8080/gbos/ceo)
启动四 App site。若本机 DNS 不解析 `gbos.localhost`，可使用
[http://127.0.0.1:8080/gbos/ceo](http://127.0.0.1:8080/gbos/ceo)；
Compose 会固定 Frappe site header。

仅验证三个上游 App 时可显式执行：

```bash
scripts/dev/bootstrap --upstream-only
```

这不是 Gate 1 四 App 结果。Observer 占位 profile 仅在需要契约连接冒烟时
显式启用：

```bash
scripts/dev/bootstrap --observer
```

查看状态与停止：

```bash
scripts/dev/status
scripts/dev/teardown
```

`teardown` 不删除 named volumes。只有在确认不再需要本地 site、数据库、
队列和 Observer 数据后，才可运行独立的永久删除命令：

```bash
scripts/dev/purge-local-data --confirm-delete-local-volumes
```

该命令拒绝生产配置，也不会操作仓库外目录。旧的带尾空格目录不在任何
脚本目标中。

## 合成演示数据

本地示例默认启用 `GBOS_LOAD_DEMO_FIXTURES=true`，并从
`fixtures/gate1/frappe_payload.json` 幂等导入。所有数据均为确定性合成
数据，界面必须显示“演示数据”。演示账号列在 fixture manifest 中，密码
只从被忽略的 `infra/dev/.env` 的 `GBOS_DEMO_PASSWORD` 注入；不得提交真实
凭据或把该值用于其他环境。

## 开发与验证

仓库级检查：

```bash
uv sync --frozen
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy \
  apps/esan_gbos/esan_gbos/domain \
  services/observer/contract_check.py \
  fixtures/gate1/generate.py \
  fixtures/kingdee/gate1/mock.py \
  fixtures/kingdee/gate2/adapter.py
```

前端检查：

```bash
pnpm --dir apps/esan_gbos/frontend install --frozen-lockfile
pnpm --dir apps/esan_gbos/frontend run lint
pnpm --dir apps/esan_gbos/frontend run typecheck
pnpm --dir apps/esan_gbos/frontend run test:unit
pnpm --dir apps/esan_gbos/frontend run build
pnpm --dir apps/esan_gbos/frontend run test:e2e
```

安全与依赖证据：

```bash
scripts/dev/secret-scan
scripts/dev/security-scan esan-gbos-final:gate1
scripts/dev/license-sbom /tmp/esan-gbos-sbom esan-gbos-final:gate1
```

Trivy 对全部未豁免 High/Critical 结果失败关闭；仓库只保留摘要、校验和和
CI 链接，原始日志与大型 SBOM 作为短期 artifact 保存。

## 重要设计资料

- `docs/superpowers/specs/`：当前规范产品/架构设计及 Gate 路线；
- `docs/adr/`：架构、权威边界、租户、AI Draft、Observer 与升级决策；
- `contracts/`：JSON Schema 2020-12 与 BFF OpenAPI；
- `docs/permission-matrix.md`：角色和记录级权限；
- `docs/governance/`：数据分类、同意/撤回、保留、删除、跨境和威胁模型；
- `docs/compat/`：精确版本、SHA、镜像 digest、CRM 字段契约和许可证；
- `docs/evidence/`：Gate 证据摘要、机器可读记录和校验和。

Gate 证据必须区分已实测事实、已知限制和待完成事项。镜像构建成功、site
可访问或 fixture 数量本身都不能替代权限、业务闭环、安全、浏览器和恢复
验收。
