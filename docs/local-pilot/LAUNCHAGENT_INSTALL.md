# LaunchAgent 人工安装说明（未执行）

本文件描述未来获得独立授权后的人工步骤，不是安装脚本。本次变更没有向
`~/Library/LaunchAgents` 写入文件，也没有运行 `launchctl`。

1. 复制模板到仓库外的临时位置。
2. 将 `__REPO_ROOT__` 替换为当前 worktree 的绝对路径，将 `__LOG_DIR__`
   替换为一个权限受控的本地日志目录。
3. 确认渲染结果不含 Keychain 值、API key、token 或密码。
4. 运行 `plutil -lint <rendered-file>`。
5. 再次确认 `RunAtLoad=false`、`KeepAlive=false`。
6. 只有在获得单独安装授权后，操作者才能将渲染文件复制到
   `~/Library/LaunchAgents/com.esan.gbos.local-pilot.plist` 并人工加载。

任何自动安装、自动登录启动、凭证写入 plist 或把 `RunAtLoad` 改为 true
都超出本地影子试点边界。
