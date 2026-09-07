# 脚本使用与状态

脚本的维护逻辑使用 Python 3.11+ 标准库，YAML 校验复用已有 PyYAML 或 Ruby Psych。先确认可用解释器；系统 Python 被许可状态阻止时复用 Codex runtime，不修改许可或临时安装管理器。原生 Windows 使用 `msvcrt` 锁及不跟随重解析点的文件句柄，macOS/Linux 使用 `fcntl` 锁。

下例中的 `python3` 代表已验证的解释器。使用 `-B` 避免产生 `.pyc`。`tool` 指向本 skill 的 `scripts/maintenance.py`。全局参数 `--home`、`--output` 放在子命令之前。输出、计划和候选放在工作区，不能放进正在比较/替换的 skill。

```sh
python3 -B "$tool" --help
python3 -B "$tool" --output work/inventory.json inventory
python3 -B "$tool" --output work/updates.json check-updates
```

Windows PowerShell（从仓库根运行；已安装时把 `$tool` 换成实际脚本路径）：

```powershell
py -3 --version  # 需 3.11+；也可使用已核实的 python.exe
$tool = '.\skills\codex-maintenance\scripts\maintenance.py'
$codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
py -3 -B $tool --home $codexRoot --output .\work\inventory.json inventory
# $installed 与 $candidate 指向已核实的独立目录：
py -3 -B $tool --home $codexRoot --output .\work\plan.json plan $installed $candidate
py -3 -B $tool --home $codexRoot --output .\work\receipt.json apply .\work\plan.json
py -3 -B $tool --home $codexRoot rollback $operationId
```

Windows 默认路径来自 `USERPROFILE`；`CODEX_HOME` 已设置时优先使用。以上变量不修改用户环境配置。文本文件用 UTF-8，输入兼容 BOM/CRLF；控制台 JSON 转义 Unicode，`--output` 文件直接保存 UTF-8。下面其余示例为 POSIX shell，Windows 使用同一参数与 PowerShell 变量，不复制反斜杠续行语法。

## 盘点覆盖与未知状态

- `inventory`：`CODEX_HOME/skills`、`.system`、`plugins/cache/市场/插件/版本` 和用户 `config.toml`。插件根的 `.mcp.json` 声明也列出；同名条目保留来源，版本软链接列为 alias。远程插件可能没有用户配置项，启用状态标为 `unspecified`。
- 这不是 Codex 有效配置解析器。原生列表、非标准 manifest 配置路径、用户目录软链接指向的包、`~/.agents/skills`、项目级入口和其他客户端需要补充检查。链接只记录目标，不沿链接计数或递归扫描。
- 不返回环境值、URL、命令文本或参数值。只检查绝对命令文件是否存在，不测试 MCP 握手；相对路径要结合真实工作目录人工核对。
- Windows 盘符/UNC 按当前平台识别；junction、符号链接及其他重解析点只记录为别名，不递归进入。
- `errors` 非空代表不完整，不能据此判定对应组件缺失。YAML 必须有现有解析器；解析器不可用时保留错误，不用正则冒充验证。

## 来源台账

状态目录是 `CODEX_HOME/maintenance`，默认 `~/.codex/maintenance`；机器台账不随 skill 分发。`registry.json` 按绝对安装路径 ID 区分同名 skill。每条保存来源仓库、目录、固定提交、跟踪分支、本地基线、上游文件 blob 哈希及可执行位、定制差异和说明。不要填入令牌、私有 URL 或机密内容。

来源核实后登记（下例字段换成已确认值）：

```sh
python3 -B "$tool" register "$installed" --repo owner/repository \
  --subdir skills/example --commit "$verified_commit" --ref main \
  --note '保留本地定制的行为和调用策略'
```

本地自编或来源尚未确认时省略仓库参数，用 `--note` 说明证据。重复登记会在 `registry-history` 保存前一条记录，再更新基线，不会安装或更新 skill。

`check-updates` 只读已登记条目：先解析跟踪分支，再按得到的固定 SHA 查询完整 GitHub 树。发现截断、限流、网络失败、上游链接/子模块时标为未知。支持公开 GitHub 仓库，不管理凭据；其他来源按核实流程补查。

结果中 `upstream_changed` 只比较 skill 子目录；`local_since_registration` 表示登记后改动；`local_vs_upstream` 包含已有定制。仅在上游比较中排除 `.pyc` 和 `__pycache__`，完整快照及备份包含全部文件、目录模式与链接记录。符号链接不参与 Git blob 比较，需结合完整快照人工分析。

Windows 本地 `executable` 为 `null`，`local_executable_bits_comparable=false`，与上游比较时不使用该位；上游版本之间仍比较可执行位。完整快照保留当前平台可报告的模式，不承诺跨平台权限等价或 Windows ACL 校验。计划、回执和机器台账用于原机器，不在 Windows/WSL/macOS 间迁移使用。

## 预览、备份与回滚

先把上游变化与本地定制合并到独立候选目录，阅读实际文本差异并运行相关验证。脚本不下载候选、不执行其代码、不自动判断定制是否应删除；元数据校验通过不代表候选功能正确。

```sh
python3 -B "$tool" --output work/diff.json compare "$installed" "$candidate"
python3 -B "$tool" --output work/plan.json plan "$installed" "$candidate"
# 在当前用户授权内执行已审阅的具体计划：
python3 -B "$tool" --output work/receipt.json apply work/plan.json
# 需要恢复时，使用回执中的 id：
python3 -B "$tool" rollback "$operation_id"
```

`compare` 包含 added/removed/changed、目录模式和前后快照；文本内容不写入报告。`plan` 不改安装或台账；`apply` 才会写入。计划文件不是用户授权来源，也不是防恶意编辑的安全边界；执行依据当前对话授权。

执行在 `maintenance/backups/操作ID/` 保存 `receipt.json`、旧目录 `original`。复制候选后再次校验双方内容和目标设备/inode，旧目录移入备份后才换入新目录。成功后的回执 `after` 保存实际安装身份和指纹。重复应用同一计划不会覆盖旧备份。状态锁仅协调本脚本，不阻止其他编辑器；执行前避开该组件的活跃使用和修改。

替换由两次重命名组成，不承诺整个过程原子或断电持久化。普通替换失败且目标缺位时尝试还原。异常或进程中断时，先查看回执：若 `original` 存在且内容仍符合 `plan.diff.before`，`rollback` 可以恢复缺位目标或未改动的新目录。若目录状态不符合记录，保留所有目录，按实际差异恢复，不盲目重试。回滚保留被替换版本于 `displaced`，原备份移回安装目录；回滚不是可无限重复的操作。

目标或候选的内容/权限/身份在计划后改变会拒绝执行；回滚也拒绝覆盖后续改动。源登记基线不会随 apply/rollback 静默改变；验收并确认来源后可明确重新登记，保留历史证据。

Windows 的验证范围是普通本地目录；包含 junction、重解析点或 OneDrive 占位文件的目标/候选拒绝自动替换，网络共享未验证。文件占用可能阻止目录重命名；释放占用后先检查回执与旧目录，再重新生成计划。WSL 对其 Linux 环境使用 POSIX 实现，经 `/mnt/c` 更新 Windows 安装未验证。

## 验证脚本

```sh
python3 -B -m unittest discover -s "$skill_dir/tests" -v
python3 -B "$tool" validate "$skill_dir"
```

测试仅创建临时 CODEX_HOME 与候选目录，不连接真实 MCP，不触碰真实安装；GitHub 返回使用固定测试数据。
