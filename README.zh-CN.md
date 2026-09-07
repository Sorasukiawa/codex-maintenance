# codex-maintenance

[English · 四语简介](README.md)

面向 Codex 的本地组件维护 skill：盘点 skills、MCP 和插件，区分官方管理与独立安装，保留本地定制，并为用户 skill 更新提供预览、备份和回滚。

这是一个社区项目，与 OpenAI 无隶属关系。当前版本适合在 macOS/Linux 上维护个人 Codex 环境。

## 它解决什么问题

- **不知道装了什么**：同时查看用户、系统和插件缓存中的 skill；同名安装实例各自保留，`latest` 链接单独记录。
- **不知道该由谁更新**：官方管理的插件、skill 和 MCP 交给官方机制，避免缓存覆盖或重复安装。
- **更新可能丢失定制**：用来源台账保存固定提交和目录基线，区分上游变化与本地改动。
- **需要能恢复的更新**：先比较完整候选目录，再生成计划；执行时复核身份和内容，保留原目录，回滚拒绝覆盖后续修改。

脚本负责可重复的机械检查；是否重复、如何合并定制、缓存是否可清理，由 agent 结合当前证据和用户授权判断。

## 安装与调用

在 Codex 中使用内置的 skill 安装能力：

```text
$skill-installer 从 https://github.com/Sorasukiawa/codex-maintenance
安装 skills/codex-maintenance。
若已有同名版本，先核对来源和本地修改，不重复安装。
```

也可以将 `skills/codex-maintenance` 整个目录复制到当前 Codex 版本实际使用的用户 skill 目录。安装路径以客户端为准；不要把同一 skill 同时复制到多个可发现入口。[官方技能说明](https://learn.chatgpt.com/docs/build-skills)

调用示例：

```text
用 $codex-maintenance 检查本机 skills、MCP 和插件。
已有官方管理版本不手动更新、不重复安装。
先给出有证据的候选清单，按我已经明确的授权处理。
```

skill 的说明目前以中文编写；可要求 agent 使用你的语言回复。

## 直接使用脚本

需要 Python 3.11+，以及当前解释器可用的 PyYAML 或 `PATH` 中已有的 Ruby Psych。脚本本身不安装依赖。YAML 解析器缺失时会明确记录错误。

在仓库根目录运行：

```sh
python3 -B skills/codex-maintenance/scripts/maintenance.py --help
python3 -B skills/codex-maintenance/scripts/maintenance.py inventory
python3 -B skills/codex-maintenance/scripts/maintenance.py check-updates
```

| 命令 | 行为 |
| --- | --- |
| `inventory` | 只读盘点配置与目录，不测试 MCP 握手 |
| `register` | 持久保存一个已核实来源或本地基线，不安装 skill |
| `check-updates` | 只读查询已登记的公开 GitHub 来源，不下载执行内容 |
| `compare` / `plan` | 比较完整候选目录，生成更新预览 |
| `apply` | 执行已审阅且处于用户授权范围内的计划，保留备份 |
| `rollback` | 在替换版本未发生后续改动时恢复旧目录 |
| `validate` | 检查 skill 元数据和调用策略 |

可用 `--home` 指定实际 `CODEX_HOME`，`--output` 保存 JSON；这两个全局参数放在子命令之前。完整例子和恢复流程见 [脚本参考](skills/codex-maintenance/references/automation.md)。

## 范围与限制

- 默认扫描 `CODEX_HOME/skills`、`.system`、标准插件缓存布局及用户 `config.toml`。`~/.agents/skills`、项目级入口和其他客户端需要 agent 补查。
- 缓存存在不代表有效启用；原生列表与握手状态需要另行核对。同名禁用 MCP 覆盖项不视为垃圾。
- 自动替换限于 `CODEX_HOME/skills` 的真实直接子目录；系统、插件、含软链接的包不由更新脚本处理。Windows 不支持 `fcntl` 锁。
- 不自动做语义合并、下载候选、卸载插件或通用缓存删除。来源不明或联网失败保持未知。
- 两次目录重命名之间存在短暂缺位窗口，不承诺整个更新过程原子或断电持久化。锁仅协调本脚本。

状态保存在 `CODEX_HOME/maintenance`，与分发目录分开。报告省略 MCP 参数、URL 和环境值，但仍含本地路径和组件名；发布 issue 前使用最小复现并去掉个人信息。不要上传真实台账、配置、备份或会话文件。

## 开发与验证

```sh
python3 -B -m unittest discover -s skills/codex-maintenance/tests -v
python3 -B skills/codex-maintenance/scripts/maintenance.py validate skills/codex-maintenance
```

测试使用临时 `CODEX_HOME`，GitHub 检查使用固定测试数据，不连接真实 MCP。GitHub Actions 在 macOS/Linux 上运行测试，并分别覆盖 Ruby Psych 和 PyYAML 解析路径。

欢迎提交带最小复现的 issue 或 PR，特别是不同 Codex 安装布局、配置格式和恢复情形。请保持维护范围清晰，并为行为变化增加有意义的测试。

## 相关项目与许可

设计调研参考了 [EfanWang/skills-manager](https://github.com/EfanWang/skills-manager) 的来源记录思路，以及 [GrubbyLee/skill-manager](https://github.com/GrubbyLee/skill-manager) 的生命周期与预览思路。本项目的脚本独立实现，重点是 Codex 官方管理组件与本地定制共存的维护流程。

[MIT License](LICENSE)。
