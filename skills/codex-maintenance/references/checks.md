# 核对参考

按本轮需要读取和使用，不要求每次运行全部命令。路径、版本和字段以当前机器为准。

## 原生查询与文件检查

先运行 `command -v codex`、`codex --version` 和相关 `--help`。已确认本机支持时，可使用：

```text
codex plugin list --json
codex plugin marketplace list --json
codex mcp list --json
codex doctor --json
```

`doctor` 可能涉及网络、数据库和终端等额外领域；只在综合健康检查有用时运行。非交互工具中的 `TERM=dumb` 不足以证明用户终端有故障；会话索引重复警告也不是删除会话的依据。

查询产生的 JSON 可能含环境变量值、URL 或启动参数。程序捕获后选择允许展示的字段，再输出结果；不要先把原始敏感值送进对话再脱敏。

- Codex 常见位置：`${CODEX_HOME:-$HOME/.codex}` 下的 `config.toml`、`skills`、`skills/.system`、`plugins/cache`；项目还可能有 `.agents/skills`、`.codex/config.toml`。`~/.agents/skills` 可能是另一个有效入口或链接。
- macOS 常见其他客户端入口：`~/.claude`、`~/.cursor/mcp.json`、`~/.gemini/config/mcp_config.json` 和 `~/Library/Application Support` 中对应客户端的用户设置。只检查实际存在且在任务范围内的路径。
- macOS Codex 运行时通常位于 `~/.cache/codex-runtimes`，但应通过当前配置、应用与进程路径核实；独立项目运行时不自动归为残留。
- 先用 `rg --files --hidden` 找目标文件，排除 `.git`、依赖和构建目录。统计目录时用 `lstat` 区分链接，避免把 `latest` 或共享源再次计算成独立安装。
- 用真正的 YAML 解析器检查 skill frontmatter 和 `agents/openai.yaml`。正则可辅助提取名字，不能据此声称 YAML 校验通过。仅显式调用和隐藏的系统 helper 不等于失效。

优先复用机器已有的 Python/Node/Ruby 运行时。系统 Python 不可用时可以寻找应用自带运行时；缺少 PyYAML 时可用已有 Ruby Psych 等解析器进行明确说明的替代检查，不为盘点批量安装依赖或修改系统许可状态。

## 上游比较

来源证据优先级：安装记录 / 仓库 remote / 插件 manifest / 先前维护清单，再到已确认的发布者仓库。不要按 skill 名称相似就认定来源。

对当前机器逐条核实来源并登记，不随分发包携带任何人的安装台账。仓库路径和声明名称可能不同；旧报告只提供线索，本地定制可能比公开版本包含更多功能。

确定上游后读取当前默认分支的提交 SHA，再按固定 SHA 比对目录树。GitHub API 的 tree 响应必须确认未截断；列出相同、不同、本地独有、上游独有四类。文件 blob SHA 可用下面的纯标准库函数核对；它比文件时间可靠：

```python
import hashlib

def git_blob_sha(data: bytes) -> str:
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()
```

对差异文本查看实际 diff，保留本地功能、授权逻辑和调用策略；对链接、可执行位、子模块等元数据另行核对，内容哈希不能覆盖这些差异。忽略生成的 `.pyc` 等缓存时要记录比较口径。`SKILL.md` 一致不代表附带脚本和引用也一致。

## 清理证据

用 `lsof -nP -F pn` 等平台工具读取句柄，再筛选候选目录的精确路径边界；输出中只保留任务相关路径。搜索当前配置、插件启动配置、市场根和相关服务配置中的引用。对“无引用”注明检查范围，不声称全盘绝对不存在引用。

记录目录身份与大小后，在删除前复核；删除使用核实后的确切路径。保留校验与执行结果用于复查，不把本次执行脚本中的绝对路径或临时目录名写入长期 skill。

## 官方说明

产品行为有疑问时先核对本机 CLI/配置能力，再按需读取最新官方说明，不以历史说明覆盖当前能力：

- [Build skills](https://learn.chatgpt.com/docs/build-skills)
- [Plugins](https://learn.chatgpt.com/docs/plugins)
- [Model Context Protocol](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
