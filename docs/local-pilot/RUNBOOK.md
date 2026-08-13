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

当前 Frappe source reference 是
`4b2512ba5bf8bbc3bc12cc6beb62055c735dc629`，image lock 为
`sha256:7b9979267b45c0ad8b635581112f245ef635c956a28d4055cfacb59703020d7c`；
runtime source reference 是 `341b2df9c45b22c0579f960dcb5ecbe694cdd215`，local
runtime image lock 为
`sha256:8a0ac2014c09765453e611e2bdf20ead82813b80ff9729cb52151382e11d00e3`；
image-lock recording commit 为 `d8bdc18b468f0e0b2507b4db3a5d0e55ef9ab2f2`。
两套镜像各自完成 governed rebuild/record，revision label 与 source hash 已复核；
final docs/evidence successors 不在镜像内，也尚未用真实渠道/模型凭据运行。
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
closure 的 source-bound 计数为 full pytest `2850 passed, 44 skipped, 1 warning`，
domain/contracts `799 passed`、infra `179 passed`，frontend unit `196 passed`、frontend-harness
Playwright `25 passed`，lint/typecheck/build、Ruff check/format、mypy、compileall、
secret scan 均 green。较早同一 feature lineage 的完整隔离 PostgreSQL matrix 为
`43 passed, 1 warning`；当前 runtime source 另在一次性 PostgreSQL 中完成三套迁移
双跑与 Observer/Context/Agent 三角色 canary 查询。全新 Frappe v16 site 使用当前
Frappe 镜像连续 migrate 两次后，身份原生测试 `13 passed`、全 app 原生测试
`59 passed`，所有临时容器、网络和卷均已删除。首次部分 site 的
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
- Prometheus 是可选 profile；固定 3.7.3 镜像已对当前共 13 条低基数规则通过静态校验
  （`promtool check rules`）：7 条 identity-resolution 规则、
  `RetentionSchedulerStale`、`RetentionSchedulerFailure`，以及 Task 13 冻结的 4 条
  Email Gateway 人工收件箱规则。
  先前 live scrape 只证明 `identity-resolution` target 为 `up=1`；默认关闭的
  retention scheduler 没有本次 live 运行证据，不能把静态告警校验解释为周期删除已运行。
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
`sha256:7b9979267b45c0ad8b635581112f245ef635c956a28d4055cfacb59703020d7c`，
local runtime 已记录
`sha256:8a0ac2014c09765453e611e2bdf20ead82813b80ff9729cb52151382e11d00e3`。
Frappe 与 runtime 分别标记 source reference `4b2512b` 和 `341b2df`，完成 inspect
与安全扫描。synthetic site setup 与 `/gbos/ceo`
浏览器验证来自较早的 `098d728` 快照，不自动证明新镜像 live runtime；这也不等于
正式 composition 已 go。若 final code 变化，后续重建仍必须显式运行
`scripts/local-pilot/build-frappe-image --confirm-network-build`，只在成功后记录
新的本机 image ID。

本次 closure snapshot 使用了 governed dependency/image/scanner network，并启动了
isolated PostgreSQL validation/build/scanner containers；没有 provider/channel network，
也没有 pilot application services。真实 IMAP/model/external calls 仍为零。

## Email Gateway human operations

Task 13 的人工收件箱边界默认保持 provider、model、network、external send 和
Send Outbox 关闭。离线测试只允许 fake publication 进入两个独立 primary mailbox，
随后验证人工身份投影、精确路由、claim/reassign、SLA、建议拒绝后人工合并、业务关联和
草稿编辑；它不能被解释为真实邮箱登录、真实 provider 回调、模型调用或外发证据。

保留执行遵循以下固定规则：

- 原始 EML、正文、附件和最终 MIME 始终由 Observer CAS 管理；Gateway 不直接执行
  CAS DELETE，也不创建重复的 raw-delivery、checkpoint、cursor 或 legal-hold 权威表。
- editable 草稿及仍活跃 Inbox/Conversation 的草稿内容引用继续保留。sent/terminal 或
  explicitly discarded 草稿的可编辑内容引用从终态时间起精确保留 30 天。
- 未确认 display/subject projection 只随对应 Observer raw-evidence expiry 清理。
  没有 durable Observer tombstone/expiration receipt，或 Observer 报告 legal hold 时，
  Gateway 必须阻断本地引用过期。
- confirmed CRM metadata、mapping/authority revision receipt、Conversation、assignment、
  SLA、business link、content digest、provider receipt 和 audit 按 CRM lifecycle 保留。
- dry-run 只生成 bounded plan，不写 content-expiration receipt；execute 每次最多 100 条，
  串行处理并写幂等 receipt。run 使用 generation/attempt/lease fence；emergency stop 时不
  claim 新任务。安全失败码进入 retry，下个周期重试；达到固定上限才进入 dead letter。

终态草稿与最终 MIME 的 30 天删除桥默认关闭。它只在 `local_pilot_go=true`、
Email Gateway kill switch 已解除，并同时给出下面三个精确 opt-in 时进入
`email-gateway-retention` profile：

```sh
scripts/local-pilot/start \
  --manifest /absolute/path/to/approved-pilot-manifest.json \
  --enable-email-gateway-retention-scheduler \
  --acknowledge-email-gateway-draft-reference-expiry \
  --acknowledge-terminal-email-material-deletion
```

最后一个确认表示允许 Observer 在 30 天到期且无 legal hold 时实际删除 CAS 内容；
缺少任一参数均在 secret、数据库或 Compose 变更前拒绝。Gateway 仅登记/验证权威和接收
tombstone callback，实际 CAS 删除只由隔离的 Observer worker 串行执行。当前正式
manifest 仍为 No-Go，因此本命令没有 live 执行证据，也不得仅凭静态测试解释为已删除。

Email Gateway 只暴露冻结的低基数指标：publication backlog/oldest age 的固定
`queued|retry|leased|dead_letter` state、closed Inbox queue enum、无标签的 SLA overdue、
identity pending、unassigned，以及固定 allowlist 的 authority failure、worker heartbeat、
dead-letter work kind。mailbox、address、message、participant、identity、Party、User、
provider payload 和 error payload 禁止作为 label。readiness 必须有数据库持久 heartbeat，
且最老 heartbeat age 不得超过 30 秒。

告警处置顺序：

1. `EmailGatewayWorkerHeartbeatStale`：heartbeat age `>30s` 持续 2 分钟。先保持 external
   send/所有 outbound switch 关闭，检查对应 worker 的持久 heartbeat 和 lease fence；
   不得用进程存在代替数据库 heartbeat。
2. `EmailGatewayDeadLetterIncrease`：5 分钟增量 `>0` 且持续 5 分钟。按固定 `work_kind`
   隔离，检查 safe receipt/audit，不查看或复制原始 payload；修复后以同一 idempotency
   identity 人工重放。
3. `EmailGatewayPublicationBacklogStale`：queued/retry 最老年龄 `>300s` 持续 10 分钟。
   先确认 emergency stop、mailbox isolation、relay heartbeat 和 Observer receipt，禁止推进
   provider cursor 或绕过 publication receipt。
4. `EmailGatewaySlaOverdue`：overdue `>0` 持续 15 分钟。由同 team manager 人工 claim 或
   reassign；不得通过发送或 Send Outbox 状态变更伪造 first response。

真实 identity/routing/provider gate、真实 mailbox credential、provider callback/poll、
外发批准和 Send Outbox 仍未由该离线流程执行；启用前必须另取当前 source/image 绑定的
授权与 live evidence。

## Task 13 credential-free closure 与 real-canary operator sequence

当前 source-bound closure 分别绑定 Frappe source reference
`4b2512ba5bf8bbc3bc12cc6beb62055c735dc629`、runtime source reference
`341b2df9c45b22c0579f960dcb5ecbe694cdd215` 与 image-lock recording commit
`d8bdc18b468f0e0b2507b4db3a5d0e55ef9ab2f2`：

```text
production_go=false
local_pilot_go=false
checked-in Email/DeepSeek disabled
real Email + DeepSeek canary 未执行
response_reported_observed_model=unknown
```

- Full pytest 为 `2850 passed, 44 skipped, 1 warning`；domain/contracts `799 passed`、
  infra `179 passed`；Ruff check/format、mypy、
  compileall、secret scan、frontend lint/typecheck/build 均 green；frontend unit
  为 `196 passed`，Playwright harness 为 `25 passed`。
- Model fatal latch 的 fail-closed 行为已验证：fatal/mismatch 会锁住模型外发并在
  后续 egress 前拒绝；当前没有真实模型调用。machine verifier 只读取低基数 latch
  状态，不替代真实 provider 证据。
- Email 只允许 source-bound `STATUS_UIDVALIDITY_UIDNEXT` 只读 checkpoint；probe
  会写 checkpoint + receipt；receipt 以私有 HMAC 绑定同一账号、team、task、host、
  port、mailbox、folder 与 username（不绑定 password），且 `canary-preflight` 必须
  复核 activation-time、source commit、digest 和 credential binding 才可继续。
  当前 real IMAP connections 为 0。
- `verify-canary-chain` 使用 machine DB-attested narrow observation window、独立
  projection config 和显式 window/output，交叉验证 Email delivery、observation、
  participant、confirmed identity、active authority、Agent invocation、Context
  intelligence/draft 与 Frappe receipt；`canary-evidence record` 必须使用
  `--chain-attestation`，只写入 `response_reported_observed_model`，不接受 free-form
  observed model。真实 canary 未运行，因此该字段当前仍 unknown。
- 本地 identity HMAC 与 Frappe identity resolver API key/secret 已按用户授权生成并存入
  Keychain，未进入仓库或证据。Email credential、DeepSeek API key、人工批准的 trusted
  phrase lexicon 与 operator scope 仍缺失。此前 older source 的 current locked runtime
  images blocker 已关闭：当前镜像已按上述实际源码重建并记录；若对应源码再变化，
  **rebuild before the real canary** 仍是硬门。72 小时连续运行不再作为本阶段退出条件，
  按用户决定 deferred/not required for this stage。
- 较早完整 isolated PostgreSQL integration matrix 为 `43 passed, 1 warning`；当前
  runtime source 另完成一次性三角色迁移/只读 canary SQL。全新 Frappe v16 site
  以当前镜像完成两次 migration，identity `13 passed`、whole app `59 passed`。
  所有 validation DB/site/network/volume 已移除。该结果仍不等于真实 provider
  canary 或正式 Go。

所有 canary dir、control、checkpoint、receipt、projection config、attestation 和
credential 文件都必须在仓库外，secrets outside the repository 只进入 Keychain；以下变量是 operator 在本机
设置的路径或 Keychain reference，不把任何秘密值写入仓库。

严格按以下顺序执行（最终代码 → governed current-image rebuild/record → prepare
external canary dir/control → probe-email-checkpoint with activation-time → copy exact
checkpoint JSON value into Keychain Email credential `initial_checkpoint` →
guarded start（内部生成临时 secrets 并执行 canary-preflight）→ verify-canary-chain with
projection config/window/output → canary-evidence record with `--chain-attestation` →
finalize）：

```sh
# Run from the exact checkout being validated; never jump to another clone/worktree.
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
test "$(git rev-parse --show-toplevel)" = "$REPO_ROOT"
test -z "$(git status --porcelain --untracked-files=all)"

# 1. final code: commit/checkout the exact source to validate.
SOURCE_COMMIT="$(git rev-parse HEAD)"
test -n "$SOURCE_COMMIT"

# 2. governed current-image rebuild/record (both commands update image lock only on success).
scripts/local-pilot/build-frappe-image --confirm-network-build
scripts/local-pilot/build-runtime-image --confirm-network-build
# Image recording changes only the lock. Review and commit that governed fact before
# formal preflight, which intentionally rejects a dirty checkout.
git diff --check
test -z "$(git status --porcelain --untracked-files=all -- . ':!infra/local/images.lock.json')"
git commit --only -m "build(local-pilot): record canary images" -- infra/local/images.lock.json
EXPECTED_SOURCE_COMMIT="$(git rev-parse HEAD)"
test -z "$(git status --porcelain --untracked-files=all)"

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
test "$(jq -r .source_commit "$CANARY_DIR/canary-run.json")" = "$EXPECTED_SOURCE_COMMIT"

# 4. probe-email-checkpoint with activation-time: only STATUS/UIDVALIDITY/UIDNEXT; no BODY fetch/backfill.
# IDENTITY_BINDING_KEY_FILE is a temporary repo-external 0600 export of the exact
# 32-byte Keychain identity-hmac-key that `start` will materialize for preflight.
# It binds the probe result to account/team/task/server/mailbox/folder/username; the
# Email password is deliberately excluded so password rotation does not invalidate it.
EMAIL_CREDENTIAL_FILE=/absolute/path/outside/repo/private-email-credential.json
IDENTITY_BINDING_KEY_FILE=/absolute/path/outside/repo/private-identity-hmac-key
test "$(stat -f '%Lp' "$IDENTITY_BINDING_KEY_FILE")" = 600
test "$(wc -c < "$IDENTITY_BINDING_KEY_FILE" | tr -d ' ')" = 32
scripts/local-pilot/probe-email-checkpoint \
  --credential-file "$EMAIL_CREDENTIAL_FILE" \
  --binding-key-file "$IDENTITY_BINDING_KEY_FILE" \
  --output-dir "$CANARY_DIR" \
  --activation-time "$ACTIVATION_TIME"

# 5. Operator action outside the repo: copy the exact JSON value from
#    $CANARY_DIR/email-checkpoint.json into the Email credential's
#    initial_checkpoint field, then update the whole credential JSON in its Keychain item.

# 6. Start only the narrow real Email + DeepSeek shadow canary. The start command
#    materializes one private temporary secret directory, runs the internal
#    canary-preflight against the bound checkpoint receipt before any Compose
#    mutation, and cleans it on failure/stop.
CANARY_STARTED=false
cleanup() {
  original_status=$?
  trap - EXIT HUP INT TERM
  if [[ "$CANARY_STARTED" == true ]]; then
    scripts/local-pilot/stop || original_status=1
  fi
  exit "$original_status"
}
trap cleanup EXIT HUP INT TERM
scripts/local-pilot/start-email-deepseek-canary \
  --acknowledge-real-email-and-model \
  --enable-retention-scheduler \
  --acknowledge-periodic-expired-local-data-deletion \
  --canary-dir "$CANARY_DIR"
CANARY_STARTED=true

# 7. Capture the first source/image/container-bound status sample.
umask 077
STATUS_BEFORE="$CANARY_DIR/status-before.json"
scripts/local-pilot/status \
  --manifest "$CANARY_DIR/pilot-manifest.json" \
  --json > "$STATUS_BEFORE"
chmod 600 "$STATUS_BEFORE"
scripts/local-pilot/canary-evidence sample \
  --canary-dir "$CANARY_DIR" \
  --status-json "$STATUS_BEFORE"

# 8. Run the machine chain verifier inside Compose local-internal. It reads the
#    already-rendered private config and 0600 DB secrets recorded by start;
#    never run verify-canary-chain directly on the host with postgres:5432 or
#    /run/secrets paths.
WINDOW_START=__RFC3339_WINDOW_START__
WINDOW_END=__RFC3339_WINDOW_END__
CHAIN_ATTESTATION=/absolute/path/outside/repo/task13-chain-attestation.json
scripts/local-pilot/canary_verifier_runtime \
  --canary-dir "$CANARY_DIR" \
  --window-start "$WINDOW_START" \
  --window-end "$WINDOW_END" \
  --output "$CHAIN_ATTESTATION"
#    The output binds exactly one observation window to the current run.

# 9. Record the machine chain attestation. It is the only source for
#     response_reported_observed_model; do not pass --observed-at or free-form observed model text.
scripts/local-pilot/canary-evidence record \
  --canary-dir "$CANARY_DIR" \
  --kind model_identity_exact \
  --source system_query \
  --chain-attestation "$CHAIN_ATTESTATION"

# 10. Produce the remaining 0600, repo-external artifacts and their closed
#     attestations from authenticated local system queries, browser captures
#     and controlled drills. Each attestation binds the run/site/source commit,
#     exact evidence SHA-256, observed time and kind-specific bounded facts;
#     hand-entered timestamps and a bare artifact hash are rejected.
CHECK_DIR="$CANARY_DIR/checks"
scripts/local-pilot/canary-evidence record --canary-dir "$CANARY_DIR" --kind email_body_peek_no_backfill --source system_query --evidence-file "$CHECK_DIR/email-body-peek.json" --check-attestation "$CHECK_DIR/email-body-peek-attestation.json"
scripts/local-pilot/canary-evidence record --canary-dir "$CANARY_DIR" --kind user_mapping_reviewed --source browser_capture --evidence-file "$CHECK_DIR/user-mapping-review.png" --check-attestation "$CHECK_DIR/user-mapping-review-attestation.json"
scripts/local-pilot/canary-evidence record --canary-dir "$CANARY_DIR" --kind party_mapping_reviewed --source browser_capture --evidence-file "$CHECK_DIR/party-mapping-review.png" --check-attestation "$CHECK_DIR/party-mapping-review-attestation.json"
scripts/local-pilot/canary-evidence record --canary-dir "$CANARY_DIR" --kind user_second_message_auto_resolved --source system_query --evidence-file "$CHECK_DIR/user-second-message.json" --check-attestation "$CHECK_DIR/user-second-message-attestation.json"
scripts/local-pilot/canary-evidence record --canary-dir "$CANARY_DIR" --kind party_second_message_auto_resolved --source system_query --evidence-file "$CHECK_DIR/party-second-message.json" --check-attestation "$CHECK_DIR/party-second-message-attestation.json"
scripts/local-pilot/canary-evidence record --canary-dir "$CANARY_DIR" --kind model_input_tokenized --source system_query --evidence-file "$CHECK_DIR/model-input-tokenized.json" --check-attestation "$CHECK_DIR/model-input-tokenized-attestation.json"
scripts/local-pilot/canary-evidence record --canary-dir "$CANARY_DIR" --kind ai_draft_review_visible --source browser_capture --evidence-file "$CHECK_DIR/ai-draft-review.png" --check-attestation "$CHECK_DIR/ai-draft-review-attestation.json"
scripts/local-pilot/canary-evidence record --canary-dir "$CANARY_DIR" --kind budget_limits_verified --source system_query --evidence-file "$CHECK_DIR/budget-limits.json" --check-attestation "$CHECK_DIR/budget-limits-attestation.json"
scripts/local-pilot/canary-evidence record --canary-dir "$CANARY_DIR" --kind retention_verified --source controlled_drill --evidence-file "$CHECK_DIR/retention-drill.json" --check-attestation "$CHECK_DIR/retention-drill-attestation.json"
scripts/local-pilot/canary-evidence record --canary-dir "$CANARY_DIR" --kind emergency_stop_verified --source controlled_drill --evidence-file "$CHECK_DIR/emergency-stop-drill.json" --check-attestation "$CHECK_DIR/emergency-stop-drill-attestation.json"
scripts/local-pilot/canary-evidence record --canary-dir "$CANARY_DIR" --kind fault_drills_verified --source controlled_drill --evidence-file "$CHECK_DIR/fault-drills.json" --check-attestation "$CHECK_DIR/fault-drills-attestation.json"
scripts/local-pilot/canary-evidence record --canary-dir "$CANARY_DIR" --kind runtime_health_verified --source system_query --evidence-file "$CHECK_DIR/runtime-health.json" --check-attestation "$CHECK_DIR/runtime-health-attestation.json"
scripts/local-pilot/canary-evidence record --canary-dir "$CANARY_DIR" --kind zero_prohibited_actions --source system_query --evidence-file "$CHECK_DIR/zero-prohibited-actions.json" --check-attestation "$CHECK_DIR/zero-prohibited-actions-attestation.json"

# 11. Capture a later healthy status sample, then close the ledger. 72 hours is not
#     required, but sample order must be increasing and every live check must
#     fall between the two samples.
STATUS_AFTER="$CANARY_DIR/status-after.json"
scripts/local-pilot/status \
  --manifest "$CANARY_DIR/pilot-manifest.json" \
  --json > "$STATUS_AFTER"
chmod 600 "$STATUS_AFTER"
scripts/local-pilot/canary-evidence sample \
  --canary-dir "$CANARY_DIR" \
  --status-json "$STATUS_AFTER"
scripts/local-pilot/canary-evidence finalize --canary-dir "$CANARY_DIR"
scripts/local-pilot/stop
CANARY_STARTED=false
trap - EXIT HUP INT TERM
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
