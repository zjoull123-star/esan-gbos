# Local Pilot Safety Assertions

这些断言同时由 manifest、Compose、preflight、生命周期脚本和专属测试
约束。任何一层不满足都不得解释为“可以启动”。

## NO-CLOUD

- `production_go=false`
- `cloud_server=false`
- `cloud_business_storage=false`
- PostgreSQL、对象存储与 Prometheus 都是本机独立命名卷。
- 默认网络 `local-internal` 为 `internal: true`。
- `controlled-egress` 仅能被显式 profile 服务使用。

## NO-KINGDEE

- `kingdee=false`
- 本地清单不声明 Kingdee 服务、端点、账号或 secret。
- preflight 对 capability 再次执行 fail-closed 断言。

## NO-OUTBOUND-BY-DEFAULT

- `external_send=false`
- email、wecom、whatsapp、model 与 tunnel 均无默认 profile。
- 真实渠道和 DeepSeek 只有在 manifest 启用、Keychain 引用存在、kill
  switch 明确解除且 preflight 完整通过时，才能加入 `controlled-egress`。
- 媒体 worker 只连接 `local-internal`；模型只读挂载且禁止网络下载。
- Cloudflared 只连接受控 webhook ingress；未匹配路径返回 404。

## Kill switch 与状态保全

- 缺省 connector/model kill switch 均为 true。
- 紧急停止先落闩，再停 tunnel、poller、webhook、model 与 media。
- emergency-stop 不执行 `down`，保留 PostgreSQL 与对象存储。
- 普通 stop 可执行 `down`，但禁止 `--volumes`/`-v`。
- 临时 secret 只在普通 stop 后清理；紧急停止时保留给仍运行的状态服务。
