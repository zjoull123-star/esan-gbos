# ESAN GBOS 本地影子试点运行手册

## 当前结论

当前状态仍是 **未组合，不可启动**。Frappe/PWA、MariaDB、Redis、
PostgreSQL、API、worker、渠道入口、model projection 与本地 filesystem
CAS 的拓扑已经声明，但尚未完成本地 runtime/Frappe 镜像构建、真实迁移、
站点 bootstrap、健康检查和 `/gbos` 浏览器验证。它们是“已声明但未运行验证”，
不是可用性证据。
真实 UI 是 Frappe PWA，不是独立的 Python pilot UI。

`infra/local/runtime-entrypoints.json` 如实区分可执行入口和仍受阻入口：
WhatsApp webhook、Email poller 与 connector worker 已有默认组合；WeCom
因缺少官方 SDK factory 继续 fail closed；model projection 代码已有 closed
lexicon resolver，但因没有当前用户的有效 attested lexicon 和真实 credentials
继续阻断；media 仍缺环境驱动的本地 runtime composition。
`local_pilot_go=false` 与 `composition.status=not_composed` 不得提前修改。
真实连接器默认关闭，DeepSeek 默认关闭。runtime entrypoint 的文件存在或
Compose config 仅证明语法，均不能解除运行门。

`docker compose ... config --quiet` 仅证明 YAML 和 Compose 模型可以解析，
不证明镜像存在、secret 可读、服务健康或试点可以启动。

## 存储、监控与网络真实性

- 权威 manifest：`infra/local/local-pilot-manifest.json`
- 权威 schema：`contracts/local_pilot/local-pilot-manifest-v1.0.schema.json`
- 编排：`infra/local/compose.yml`
- 本地不可变 evidence truth 是 `local-pilot-evidence-cas` filesystem CAS。
  MinIO 不属于 required runtime，也没有对象存储控制台。
- Prometheus 是可选 profile，当前仅抓取自身，空 alerts 文件不宣称业务
  指标、SLO 或告警已经接线。
- PostgreSQL、MariaDB、API、PWA、webhook 与可选 Prometheus 的宿主机端口
  只绑定 `127.0.0.1`。
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

Frappe 使用独立的本地 image ref。只有显式运行
`scripts/local-pilot/build-frappe-image --confirm-network-build` 后才记录
本机 image ID。本手册不声称任何一个 build 已完成。

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
purposes，并创建 `Website User` / `desk_access=0` 的最小角色身份。该入口
已组合，但尚未在本机真实 Frappe image/site 上执行验证。

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
`composition.status` 或 formal `start --require-go` 门。此路径已静态组合但
尚未实际运行。

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
且仍受正式 go/composition/image preflight 约束。该路径与 `/gbos` 目前均为
已声明但未运行验证。

## LaunchAgent

`infra/local/launchagents/com.esan.gbos.local-pilot.plist.template` 保持
`RunAtLoad=false`、`KeepAlive=false`，不含凭据。本次不会安装 LaunchAgent，
也不会调用 `launchctl`。
