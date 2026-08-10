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

runtime code validation reference 是 `ad58ab3ea8c0d521cebd90c2642709d135f98fac`；final branch includes only image-lock/test/docs successors after it。当前 Frappe image lock 为
`sha256:22c3a2c129442588d0353c6a8f564aec593afc63b7354b4294d37aa9d40f7625`，
local runtime image lock 为
`sha256:6cc85a0e0f39e683af2f4fde15e93b706a76489831d18acd30f78867ec45cdee`；
两者均已从该 runtime code validation reference `ad58ab3` governed rebuild/record，
revision label 已复核，image-lock recording commit 为 `e10a780`。final docs/evidence
successors 不在这些镜像内；它们尚未用真实渠道/模型凭据运行。
若 final code 再变化，真实 canary 前仍必须
重复 governed current-image rebuild/record。以下
site、浏览器和监控观察来自先前 synthetic 快照：
site `setup_complete=1`，Frappe/ERPNext/CRM/esan_gbos 版本分别为
`16.30.0`/`16.31.0`/`1.81.0`/`0.1.0`。连续两次 migrations
checksum-consistent，materializer bootstrap 为 skipped/idempotent；fixture
第二次运行全部 skipped（13 User、5 Team、各 500 CRM Organization/Contact/
Lead/Deal/Party Profile、各 240 Product Brief/Sample Project/Iteration/Shipment/
Feedback/Demand/Sourcing、280 Work Item、280 Review Case）。

Playwright 使用 `synthetic.ceo@example.invalid` 登录后访问 `/gbos/ceo` 成功，页面
显示“经营总览”和“演示 / 合成数据”；375/768/1440 宽度均无横向溢出，console
errors/warnings 均为 0，cache 只有 21 个静态预缓存条目且 API `cached=false`。
上述是历史 snapshot，不是当前 HEAD 的 live runtime 证明。当前 credential-free
closure 的 source-bound 计数为 full pytest `2692 passed/42 skipped/1 warning`，
frontend unit `188 passed`、frontend-harness Playwright `22 passed`，lint/typecheck/
build、Ruff check/format、mypy、compileall、secret scan 均 green。首次部分 site 的
失败目录已可恢复地移动到数据卷内 `.failed-gbos.localhost-20260808T033521`，未删除。

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
  live scrape 中 `identity-resolution` target 为 `up=1`，7 条低基数规则健康。
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
`sha256:22c3a2c129442588d0353c6a8f564aec593afc63b7354b4294d37aa9d40f7625`，
local runtime 已记录
`sha256:6cc85a0e0f39e683af2f4fde15e93b706a76489831d18acd30f78867ec45cdee`，
两者均标记 runtime code validation reference `ad58ab3`，完成 inspect 和安全扫描。synthetic site setup 与 `/gbos/ceo`
浏览器验证来自较早的 `098d728` 快照，不自动证明新镜像 live runtime；这也不等于
正式 composition 已 go。若 final code 变化，后续重建仍必须显式运行
`scripts/local-pilot/build-frappe-image --confirm-network-build`，只在成功后记录
新的本机 image ID。

本次 closure snapshot 使用了 governed dependency/image/scanner network，并启动了
isolated PostgreSQL validation/build/scanner containers；没有 provider/channel network，
也没有 pilot application services。真实 IMAP/model/external calls 仍为零。

## Task 13 credential-free closure 与 real-canary operator sequence

当前 source-bound closure 绑定 runtime code validation reference
`ad58ab3ea8c0d521cebd90c2642709d135f98fac`；final branch includes only
image-lock/test/docs successors after it：

```text
production_go=false
local_pilot_go=false
checked-in Email/DeepSeek disabled
real Email + DeepSeek canary 未执行
response_reported_observed_model=unknown
```

- Full pytest 为 `2692 passed/42 skipped/1 warning`；Ruff check/format、mypy、
  compileall、secret scan、frontend lint/typecheck/build 均 green；frontend unit
  为 `188 passed`，Playwright harness 为 `22 passed`。
- Model fatal latch 的 fail-closed 行为已验证：fatal/mismatch 会锁住模型外发并在
  后续 egress 前拒绝；当前没有真实模型调用。machine verifier 只读取低基数 latch
  状态，不替代真实 provider 证据。
- Email 只允许 source-bound `STATUS_UIDVALIDITY_UIDNEXT` 只读 checkpoint；probe
  会写 checkpoint + receipt，且 `canary-preflight` 必须看到绑定 activation-time、
  source commit、digest 和 receipt 才可继续。当前 real IMAP connections 为 0。
- `verify-canary-chain` 使用 machine DB-attested narrow observation window、独立
  projection config 和显式 window/output；`canary-evidence record` 必须使用
  `--chain-attestation`，只写入 `response_reported_observed_model`，不接受 free-form
  observed model。真实 canary 未运行，因此该字段当前仍 unknown。
- Email credential、DeepSeek API key、identity HMAC、trusted phrase lexicon、Frappe
  identity resolver credentials 仍缺失。此前 older source 的 current locked runtime
  images blocker 已关闭：当前镜像已在 `ad58ab3` 重建并记录；若 final code 再变化，
  **rebuild before the real canary** 仍是硬门。72 小时连续运行不再作为本阶段退出条件，
  按用户决定 deferred/not required for this stage。
- Root 已完成 isolated PostgreSQL integration matrix：`42 passed, 10 deselected,
  1 warning`，覆盖 Gate3/4/5、Context、Media；唯一 validation DB/network/volume
  已移除。该结果仍不等于真实 provider canary 或正式 Go。

所有 canary dir、control、checkpoint、receipt、projection config、attestation 和
credential 文件都必须在仓库外，secrets outside the repository 只进入 Keychain；以下变量是 operator 在本机
设置的路径或 Keychain reference，不把任何秘密值写入仓库。

严格按以下顺序执行（最终代码 → governed current-image rebuild/record → prepare
external canary dir/control → probe-email-checkpoint with activation-time → copy exact
checkpoint JSON value into Keychain Email credential `initial_checkpoint` →
canary-preflight requiring receipt → start narrow real canary → verify-canary-chain with
projection config/window/output → canary-evidence record with `--chain-attestation` →
finalize）：

```sh
# The governed commands below run from the canonical repository root.
REPO_ROOT=/Users/ericesan/Documents/GBOS
cd "$REPO_ROOT"

# 1. final code: commit/checkout the exact source to validate.
git rev-parse HEAD

# 2. governed current-image rebuild/record (both commands update image lock only on success).
scripts/local-pilot/build-frappe-image --confirm-network-build
scripts/local-pilot/build-runtime-image --confirm-network-build

# 3. prepare external canary dir/control as a new, empty repo-external directory.
CANARY_DIR=/absolute/path/outside/repo/gbos-task13-canary
ACTIVATION_TIME=__RFC3339_APPROVED_ACTIVATION_TIME__
EMAIL_KEYCHAIN_REF=keychain://__SERVICE__/__EMAIL_ACCOUNT__
DEEPSEEK_KEYCHAIN_REF=keychain://__SERVICE__/__DEEPSEEK_ACCOUNT__
scripts/local-pilot/prepare-email-deepseek-canary \
  --acknowledge-shadow-pilot \
  --output-dir "$CANARY_DIR" \
  --site-id gbos.localhost \
  --activation-time "$ACTIVATION_TIME" \
  --email-credential-ref "$EMAIL_KEYCHAIN_REF" \
  --deepseek-keychain-ref "$DEEPSEEK_KEYCHAIN_REF"

# 4. probe-email-checkpoint with activation-time: only STATUS/UIDVALIDITY/UIDNEXT; no BODY fetch/backfill.
EMAIL_CREDENTIAL_FILE=/absolute/path/outside/repo/private-email-credential.json
scripts/local-pilot/probe-email-checkpoint \
  --credential-file "$EMAIL_CREDENTIAL_FILE" \
  --output-dir "$CANARY_DIR" \
  --activation-time "$ACTIVATION_TIME"

# 5. Operator action outside the repo: copy the exact JSON value from
#    $CANARY_DIR/email-checkpoint.json into the Email credential's
#    initial_checkpoint field, then update the whole credential JSON in its Keychain item.

# 6. canary-preflight validates the private manifest/control and checkpoint pair.
SECRET_DIR=/absolute/path/outside/repo/private-canary-secrets
scripts/local-pilot/canary-preflight \
  --manifest "$CANARY_DIR/pilot-manifest.json" \
  --run-control "$CANARY_DIR/canary-run.json" \
  --secret-dir "$SECRET_DIR" \
  --repo-root "$REPO_ROOT" \
  --json
#    It must require and verify the checkpoint receipt before returning ready.

# 7. Start only the narrow real Email + DeepSeek shadow canary.
scripts/local-pilot/start-email-deepseek-canary \
  --acknowledge-real-email-and-model \
  --canary-dir "$CANARY_DIR"

# 8. verify-canary-chain with projection config/window/output and one DB-attested observation window.
CONFIG_DIR=/absolute/path/outside/repo/private-canary-config
scripts/local-pilot/render-config \
  --manifest "$CANARY_DIR/pilot-manifest.json" \
  --output-dir "$CONFIG_DIR"
PROJECTION_CONFIG="$CONFIG_DIR/projection-connections.json"
WINDOW_START=__RFC3339_WINDOW_START__
WINDOW_END=__RFC3339_WINDOW_END__
CHAIN_ATTESTATION=/absolute/path/outside/repo/task13-chain-attestation.json
scripts/local-pilot/verify-canary-chain \
  --canary-dir "$CANARY_DIR" \
  --projection-config "$PROJECTION_CONFIG" \
  --window-start "$WINDOW_START" \
  --window-end "$WINDOW_END" \
  --output "$CHAIN_ATTESTATION"

# 9. Record the machine chain attestation; do not pass --observed-at or free-form observed model text.
#    The attestation is the only source for response_reported_observed_model.
scripts/local-pilot/canary-evidence record \
  --canary-dir "$CANARY_DIR" \
  --kind model_identity_exact \
  --source system_query \
  --chain-attestation "$CHAIN_ATTESTATION"

# 10. After all required checks and bounded samples, finalize the private evidence package.
scripts/local-pilot/canary-evidence finalize --canary-dir "$CANARY_DIR"
```

The sequence never enables checked-in Email/DeepSeek, Kingdee, cloud, external send or
production. It must stop on any source/image binding mismatch, missing receipt, closed model
fatal latch, projection ambiguity, or response-reported model mismatch.

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
`sales`、`purchase`、`product_sample`、`ceo`。`account_user_ref` 是独立的
渠道账号负责人，只能填写已确认的 Frappe User；它不能推导沟通参与人、客户映射或
业务负责人。

### Email credential JSON

```json
{
  "instance_id": "email-primary",
  "team_ref": "__APPROVED_TEAM__",
  "agent_task_type": "sales",
  "account_user_ref": "__APPROVED_FRAPPE_USER__",
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
  "initial_checkpoint": "{\"mailbox\":\"pilot-primary\",\"uid\":0,\"uidvalidity\":1,\"version\":1}"
}
```

上述 `initial_checkpoint` 只展示 closed JSON 形状，**不能直接用于真实 canary**。
真实值必须在 activation time 使用只读 IMAP TLS 检查取得当前 `UIDVALIDITY` 与
最高已存在 UID，并保持 mailbox 完全一致；否则 preflight 必须拒绝启动。该检查不应
设置已读、移动、删除或抓取历史正文。

### WhatsApp credential JSON

```json
{
  "instance_id": "whatsapp-primary",
  "team_ref": null,
  "agent_task_type": null,
  "account_user_ref": "__APPROVED_FRAPPE_USER__",
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
  "account_user_ref": "__APPROVED_FRAPPE_USER__",
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
