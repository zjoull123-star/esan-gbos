# ESAN GBOS 本地影子试点运行手册

## 当前结论

这套清单与 synthetic/dev 完全分离：项目名、PostgreSQL、对象存储与
Prometheus 命名卷均独立，不引用 `infra/dev`。数据库、对象存储控制台和
监控端口只绑定 `127.0.0.1`。本机默认状态是停用态：真实连接器默认关闭，
DeepSeek 默认关闭，模型 kill switch 打开，`external_send=false`，且
`local_pilot_go=false`。

当前结论是：**未组合，不可启动**。真实 PWA 是 Frappe PWA，不是独立
Python `pilot-ui`；本地 Frappe composition 尚未设计进该 Compose。
`infra/local/runtime-entrypoints.json` 中声明的 runtime entrypoint 尚未
实现，本地 runtime 也没有 Containerfile 或已检查镜像。
`images.lock.json` 如实记录 MinIO、Prometheus、Cloudflared 与 local
runtime 在本机均未安装；相应本地 inspect ID/RepoDigest 为 null。
preflight 必须因此 **fail closed**，`start` 必须在读取 Keychain 和调用
Compose 之前退出。这不是可运行交付。

`docker compose ... config --quiet` 即使通过，也只表示 YAML 和 Compose
模型可以解析。**Compose config 仅证明语法**，不证明镜像已安装、runtime
已组合、健康检查可达或试点可以启动。

## 权威配置与安全边界

- 运行声明：
  `infra/local/local-pilot-manifest.json`
- 权威 schema：
  `contracts/local_pilot/local-pilot-manifest-v1.0.schema.json`
- 编排：
  `infra/local/compose.yml`
- Cloudflared：默认 profile 关闭；启用时仅 WhatsApp webhook ingress
  `^/webhooks/whatsapp(/.*)?$` 可达，其他路径固定返回 404。
- WhatsApp Cloud API 不存在 poller；入口只有 webhook。后续媒体下载必须
  由 ingress 完成持久化交接后交给单独 durable worker，本清单不虚构该 worker。
- Kingdee：本试点明确禁止；manifest 中 `kingdee=false`，编排无 Kingdee
  服务或凭证。
- 媒体：模型目录只读挂载，`HF_HUB_OFFLINE=1`、
  `TRANSFORMERS_OFFLINE=1`、`PIP_NO_INDEX=1`；运行期禁止下载。
  ffmpeg 与 Whisper 模型必须提供真实 SHA-256，placeholder SHA 会被拒绝。
- 远程镜像：Compose 引用必须包含 `@sha256`；本机实际 inspect ID 与
  RepoDigest 必须同时匹配 lock。本机缺少必需/已启用镜像或 lock 字段为
  null 都会阻断，脚本不会拉取。
- UI：Frappe PWA composition 是显式 blocker；在它完成前没有本地 UI
  服务或 UI 端口可供验证。

## Keychain 准备

脚本只使用 macOS `/usr/bin/security find-generic-password ... -w` 读取
Keychain。它不接受命令行明文密码、不打印取回值，并以 `umask 077` 在系统
临时目录生成 Compose secrets；每个文件强制为 `0600`。常驻仓库和日志均
不保存明文。

本地状态服务使用下列 Keychain 通用密码项目：

| Service | Account | 用途 |
| --- | --- | --- |
| `com.esan.gbos.local-pilot` | `postgres-password` | 独立 PostgreSQL |
| `com.esan.gbos.local-pilot` | `object-store-password` | 独立对象存储 |
| `com.esan.gbos.local-pilot` | `cloudflared-tunnel-json` | 仅在 WhatsApp tunnel 启用时 |

渠道与 DeepSeek 的 `keychain://<service>/<account>` 引用来自 manifest。
不要把凭证内容写入 manifest、shell history、环境样例或 LaunchAgent。

## 安全启动

1. 先编辑独立 manifest；保持 `production_go=false`、全部 capability
   为 false。只有完成运行入口、镜像、凭证、媒体哈希和现场审批后，才把
   `local_pilot_go` 改为 true。
2. 执行只读预检：

   ```sh
   scripts/local-pilot/preflight --manifest infra/local/local-pilot-manifest.json
   ```

3. 预检无错误后才执行：

   ```sh
   scripts/local-pilot/start --manifest infra/local/local-pilot-manifest.json
   ```

`start` 始终先执行 `--require-go` preflight。manifest 只启用哪个渠道，
脚本才加入哪个独立 profile；未启用的渠道不会随其他渠道启动。

## 状态、停止与紧急停止

查看本地容器及紧急闩锁：

```sh
scripts/local-pilot/status
```

普通停止：

```sh
scripts/local-pilot/stop
```

普通停止会关闭容器并清理临时 secrets，但**不删除任何命名卷**，紧急闩锁
也保持原状态。

紧急停止：

```sh
scripts/local-pilot/emergency-stop
```

紧急停止先写入 `EMERGENCY_STOP` 闩锁，再关闭 tunnel、webhook、email/
WeCom poller、DeepSeek/model、agent worker 与 media worker；它会
**保留 PostgreSQL 与对象存储**、Prometheus 和临时 secrets，方便取证且不会
破坏数据。闩锁存在时 `start` 一律拒绝继续。完成隔离与人工确认后才可：

```sh
scripts/local-pilot/clear-emergency-stop --acknowledge-contained
```

## LaunchAgent 模板

`infra/local/launchagents/com.esan.gbos.local-pilot.plist.template` 仅为人工
审阅模板，`RunAtLoad=false`、`KeepAlive=false`，不含凭证。本次交付
**不会安装 LaunchAgent**，也不会执行 `launchctl`。

如果未来获得单独安装授权，应先把 `__REPO_ROOT__` 和 `__LOG_DIR__` 渲染到
一个新的本地文件，使用 `plutil -lint` 检查，复核 ProgramArguments 与
权限，再由操作者自行复制到 `~/Library/LaunchAgents` 并执行 bootstrap。
不要直接修改或加载仓库模板。
