# ESAN GBOS 本地影子试点运行手册

## 当前结论

本地服务拓扑和当前源码镜像现已 **组合并记录**：
`composition.status=composed`。这不是运行放行；正式 manifest 仍为
`local_pilot_go=false`，`scripts/local-pilot/preflight --require-go` 必须返回 78。
生产、真实渠道、真实 DeepSeek、Kingdee 与云部署均保持 No-Go。不能把镜像构建或
synthetic 运行快照解释为正式可用性证据。

与正式门分开，当前 Mac 已完成一次本地、禁用态 synthetic core 运行快照。它使用
已构建的本地 runtime/Frappe 镜像，启动 Frappe PWA、Context、Agent、Observer；
channels、models、media、tunnel 均 disabled。PWA、API 和数据库只监听 loopback：
PWA `127.0.0.1:58080`，Context `58001`，Agent `58002`，Observer `58003`，
PostgreSQL `55432`，MariaDB `53306`。真实 UI 是 Frappe PWA，不是独立的 Python
pilot UI。

该快照还观察到：`local-internal` 是 bridge 且
`enable_ip_masquerade=false`，不是 `internal: true`；从 pwa/context/agent/observer
容器访问 `api.deepseek.com:443` 均 blocked，`webhook-tunnel` 仍为 internal。
这只证明本地 synthetic core 的隔离边界，不改变正式 No-Go。

当前 Frappe image lock 为
`sha256:94c1bb068a868e0c0c7bb1deda231c2fc5bd13f2928b83036f83802674c5afe6`，
local runtime image lock 为
`sha256:705012abe856dbe33298e508c79e121831585e1036dca701a93553ebe0186c8b`；
两者均绑定 source revision `00a1a0a`。它们已完成构建和本地 inspect，但尚未用真实
渠道/模型凭据运行。以下 site、浏览器和监控观察来自先前 synthetic 快照：
site `setup_complete=1`，Frappe/ERPNext/CRM/esan_gbos 版本分别为
`16.30.0`/`16.31.0`/`1.81.0`/`0.1.0`。连续两次 migrations
checksum-consistent，materializer bootstrap 为 skipped/idempotent；fixture
第二次运行全部 skipped（13 User、5 Team、各 500 CRM Organization/Contact/
Lead/Deal/Party Profile、各 240 Product Brief/Sample Project/Iteration/Shipment/
Feedback/Demand/Sourcing、280 Work Item、280 Review Case）。

Playwright 使用 `synthetic.ceo@example.invalid` 登录后访问 `/gbos/ceo` 成功，页面
显示“经营总览”和“演示 / 合成数据”；375/768/1440 宽度均无横向溢出，console
errors/warnings 均为 0，cache 只有 21 个静态预缓存条目且 API `cached=false`。
当前验证还包括最终 Frappe 镜像上的新建隔离 site 原生测试 `58 passed`、前端
Vitest `187 passed`、frontend-harness Playwright `22 passed`，以及真实 synthetic
site Playwright `4 passed/18 skipped`；后端与静态检查的最终计数写入新的
`identity-resolution-runtime` 证据包。首次部分 site 的失败目录已可恢复地移动到数据卷内
`.failed-gbos.localhost-20260808T033521`，未删除。

`infra/local/runtime-entrypoints.json` 如实区分可执行入口和仍受阻入口：
WhatsApp webhook、Email poller 与 connector worker 已有默认组合；WeCom
因缺少官方 SDK factory 继续 fail closed；model projection 代码已有 closed
lexicon resolver，但因没有当前用户的有效 attested lexicon 和真实 credentials
继续阻断；media 仍缺环境驱动的本地 runtime composition。
`local_pilot_go=false` 不得因 `composition.status=composed` 而提前修改。真实连接器默认关闭，
DeepSeek 默认关闭。runtime entrypoint、Compose config 和源绑定镜像只
证明组合条件，均不能解除运行门。

`docker compose ... config --quiet` 仅证明 YAML 和 Compose 模型可以解析，
不证明镜像存在、secret 可读、服务健康或试点可以启动。

## 存储、监控与网络真实性

- 权威 manifest：`infra/local/local-pilot-manifest.json`
- 权威 schema：`contracts/local_pilot/local-pilot-manifest-v1.0.schema.json`
- 编排：`infra/local/compose.yml`
- 本地不可变 evidence truth 是 `local-pilot-evidence-cas` filesystem CAS。
  MinIO 不属于 required runtime，也没有对象存储控制台。
- Prometheus 是可选 profile；固定 3.7.3 镜像已完成 `promtool` 配置/规则校验，
  live scrape 中 `identity-resolution` target 为 `up=1`，5 条低基数规则健康。
  `gbos_identity_resolver_ready=0` 是预期禁用态结果，不可解释为真实 worker 已就绪或
  SLO 已达成。72 小时连续运行已按用户决定延后且不作为本阶段退出条件；证据只记录
  实际采样时长。
- PostgreSQL、MariaDB、API、PWA、webhook 与可选 Prometheus 的宿主机端口
  只绑定 `127.0.0.1`；本次 synthetic core 的已验证端口见上方快照。
- `local-internal` 使用 bridge 网络并关闭 `enable_ip_masquerade`，不使用
  `internal: true`；pwa/context/agent/observer 到 `api.deepseek.com:443` 的实测
  出站均 blocked。`webhook-tunnel` 仍为 internal。
- Cloudflare Tunnel 只连接 WhatsApp webhook ingress，且只允许
  `^/webhooks/whatsapp(/.*)?$`；它不连接 API 网络。
- 仅 WhatsApp webhook ingress 可以被 tunnel 访问。
- Kingdee、cloud server、cloud business storage 与 external send 均关闭。
- WhatsApp Cloud API 不存在 poller；接收入口只有 webhook。
- Media 模型目录只读，运行期禁止下载。

## 镜像与构建

Runtime Containerfile 固定 `linux/arm64` Python 3.14.2 与 uv 0.9.28 的
digest。`scripts/local-pilot/build-runtime-image --confirm-network-build`
才允许显式网络构建，因为 Docker 可能需要取得 digest-pinned base image，
`uv sync --frozen` 也可能需要下载 lock 中的依赖。构建成功后脚本才原子记录
Python base、uv builder 与 local runtime 的本机 image ID、RepoDigest 和平台。

Frappe 使用独立的本地 image ref。当前 lock 已记录
`sha256:94c1bb068a868e0c0c7bb1deda231c2fc5bd13f2928b83036f83802674c5afe6`，
local runtime 已记录
`sha256:705012abe856dbe33298e508c79e121831585e1036dca701a93553ebe0186c8b`，
两者仅完成当前源码构建、inspect 和安全扫描。synthetic site setup 与 `/gbos/ceo`
浏览器验证来自较早的 `098d728` 快照，不自动证明新镜像 live runtime；这也不等于
正式 composition 已 go。后续重建仍必须显式运行
`scripts/local-pilot/build-frappe-image --confirm-network-build`，只在成功后记录
新的本机 image ID。

## Keychain 与最小权限

`prepare-secrets` 只通过 macOS Keychain reference 读取凭据，不接受明文
环境变量，也不打印 secret。临时目录为 `0700`，文件为 `0600`。

PostgreSQL 使用独立凭据文件：

| Role | Keychain account |
| --- | --- |
| 管理迁移 | `postgres-password` |
| `gbos_observer_app` | `postgres-observer-password` |
| `gbos_context_app` | `postgres-context-password` |
| `gbos_agent_app` | `postgres-agent-password` |
| `gbos_media_app` | `postgres-media-password` |

根代理已在 OrbStack 验证：host uid 501、mode `0600` 的 bind secret 在 runtime
容器内映射为 uid 10001、mode `0600`，且 user `10001` 可读。仓库测试固定
runtime 用户和 secret 模式声明；不同运行时仍必须在启动前重新验证，不能从
这条本机证据推断其他 Docker 实现。

Frappe site config 只保存 agent/observer 的 exact internal URL、auth-ref 和
`/run/secrets/agent_api_bearer` 文件路径；不保存 token 明文。Frappe backend
只读挂载该 token 文件。Agent worker 只连接 `context-api:8001`；
materialization worker 只连接 `frappe-backend:8000`。

Frappe site 初始化会先写入 closed materializer identity 配置。随后
`start` 严格执行 `migrations → materializer identity bootstrap → runtime`：
profile-only `frappe-materializer-bootstrap` 从两个 mode `0600` 文件读入 API
key/secret，并调用 bench-only provisioning helper；secret 不写进 site config
也不输出。helper 只允许 exact `agent-materializer-v1`、当前 site、
`gbos-materializer@localhost.invalid`，以及 `observation_processing`、
`sales_follow_up`、`procurement_coordination`、
`product_sample_management`、`metric_reporting` 五项 exact processing
purposes，并创建 `Website User` / `desk_access=0` 的最小角色身份。当前 synthetic
snapshot 记录该 bootstrap 为 skipped/idempotent；正式 materializer 身份和正式
composition 仍受 `local_pilot_go=false` 门约束。

## 渠道 credential JSON

manifest 的 `keychain://<service>/<account>` 对应 Keychain item 保存的是
整份 credential JSON，不是单独 token。下面仅是字段完整的无秘密占位示例，
不能直接使用。启用渠道时，manifest 的 `activation_time` 必须是带时区的
审批时间，且 rendered connector config 必须与它完全一致；
`backfill_history` 必须保持 false。

`instance_id` 标识一个稳定的 connector instance。`team_ref` 与
`agent_task_type` 必须同时为空，或使用已审批 team；只要
`agent_task_type` 非空就必须提供 `team_ref`。task type 只允许
`sales`、`purchase`、`product_sample`、`ceo`。

### Email credential JSON

```json
{
  "instance_id": "email-primary",
  "team_ref": null,
  "agent_task_type": null,
  "host": "imap.example.invalid",
  "port": 993,
  "mailbox": "pilot-primary",
  "folder": "INBOX",
  "username": "__REPLACE_IN_KEYCHAIN__",
  "password": "__REPLACE_IN_KEYCHAIN__",
  "poll_limit": 25,
  "max_message_bytes": 1000000,
  "max_attachment_bytes": 100000,
  "max_attachments": 5,
  "rescan_max_window_seconds": 86400,
  "rescan_max_uids": 100,
  "initial_checkpoint": null
}
```

### WhatsApp credential JSON

```json
{
  "instance_id": "whatsapp-primary",
  "team_ref": null,
  "agent_task_type": null,
  "app_secret": "__REPLACE_IN_KEYCHAIN__",
  "verify_token": "__REPLACE_IN_KEYCHAIN__",
  "path": "/webhooks/whatsapp",
  "max_body_bytes": 1048576
}
```

### WeCom credential JSON

```json
{
  "instance_id": "wecom-primary",
  "team_ref": null,
  "agent_task_type": null,
  "corp_id": "__REPLACE_IN_KEYCHAIN__",
  "secret": "__REPLACE_IN_KEYCHAIN__",
  "private_key": "__REPLACE_IN_KEYCHAIN__",
  "initial_checkpoint": "__REPLACE_WITH_APPROVED_SEQUENCE__"
}
```

WeCom 当前状态仍是 `blocked_official_sdk`；录入 JSON 不会解除 SDK factory
门，也不得据此启用 profile。

Model projection 还要求 Keychain account
`trusted-phrase-lexicon` 保存整份 `trusted_phrase_lexicon` closed JSON。
它必须绑定当前 site、包含人工 attestation，并且有效期不超过 30 天。
DeepSeek 关闭时 prepare-secrets 只生成空 sentinel，且只有
model-projection-worker 挂载该文件；DeepSeek 启用时缺失、过期或 site
不匹配都会在数据库与 HTTP 前返回 78。当前状态是
`blocked_user_lexicon_and_credentials`，不是代码入口缺失。

无秘密、不可直接使用的 closed JSON 字段示例：

```json
{
  "schema_version": "1.0",
  "site_id": "gbos.localhost",
  "resolver_version": "__APPROVED_VERSION__",
  "approved_by": "__HUMAN_APPROVER__",
  "approved_at": "__RFC3339_APPROVED_AT__",
  "expires_at": "__RFC3339_WITHIN_30_DAYS__",
  "names_complete": true,
  "organizations_complete": true,
  "names": ["__APPROVED_NAME_PLACEHOLDER__"],
  "organizations": []
}
```

`names` 与 `organizations` 必须是去重 string arrays，至少一个非空；短语
不得含换行、NUL 或 tokenizer token 形状。`approved_at` 与 `expires_at`
必须带时区，并满足 `approved_at <= now < expires_at <= approved_at + 30 天`。

## 启动、停止与紧急停止

正式启动入口：

```sh
scripts/local-pilot/start --manifest infra/local/local-pilot-manifest.json
```

`start` 先运行 `preflight --require-go`，随后才允许准备 secret、渲染 config、
执行 PostgreSQL migrations、materializer identity bootstrap 和启动 runtime。
当前正式 preflight 必须返回 78。仅用于禁用态配置测试的命令是：

```sh
scripts/local-pilot/preflight --synthetic
```

在已构建并记录的本地镜像以 `linux/arm64` 本机检查通过后，
可用下面的显式路径启动隔离 synthetic core 与 Frappe/PWA：

```sh
scripts/local-pilot/start-synthetic --acknowledge-synthetic
```

它先执行 `preflight --synthetic --require-runtime-images`，再从 checked-in
disabled manifest 生成临时、仓库外的 core-only runtime manifest。该 manifest
保持 production、Kingdee、cloud、external send、所有 channel 与 DeepSeek
关闭；只启动 Context/Agent/Observer API 和 Frappe worker/scheduler/PWA，
不启动 connector、model、media 或 tunnel。它不会更改 checked-in manifest、
`composition.status` 或 formal `start --require-go` 门。历史 snapshot 曾按该路径
运行并完成上述本地验证；它不是当前镜像的真实渠道/模型证据，也不是正式 Go。

状态与停止：

```sh
scripts/local-pilot/status
scripts/local-pilot/stop
scripts/local-pilot/emergency-stop
```

普通 stop 不删除任何命名卷。紧急停止保留 PostgreSQL 与 filesystem CAS，
并停止 tunnel、channel、model、agent、materialization、webhook、media 和
Frappe worker/scheduler。完成隔离与人工确认后才可执行：

```sh
scripts/local-pilot/clear-emergency-stop --acknowledge-contained
```

## Frappe synthetic bootstrap

Site 创建会检查并安装 `erpnext`、`crm`、`esan_gbos`，然后连续执行两次
`bench migrate`。显式 synthetic 测试用户路径是：

```sh
scripts/local-pilot/bootstrap-synthetic-user --acknowledge-synthetic
```

密码只能来自 `/run/secrets/frappe_demo_password`；脚本不会隐式启动 stack，
且仍受正式 go/composition/image preflight 约束。本次 synthetic snapshot 已完成
该用户路径并由 Playwright 验证 `/gbos/ceo`；这不放宽正式门。

## LaunchAgent

`infra/local/launchagents/com.esan.gbos.local-pilot.plist.template` 保持
`RunAtLoad=false`、`KeepAlive=false`，不含凭据。本次不会安装 LaunchAgent，
也不会调用 `launchctl`。
