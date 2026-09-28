---
name: codex-maintenance
description: Use when the user asks to audit, update, deduplicate, organize, or clean up locally installed Codex skills, MCP servers, plugins, or their installation remnants. Also applies to checking equivalent AI-client configuration when included in the request. Not for general disk cleanup or ordinary use of a plugin.
---

# Codex 本地组件维护

核对实际安装与生效状态，完成当前授权范围内的维护。默认中文汇报。“检查”只读；执行已有清单时刷新证据，不重复确认。新对象不自动加入旧删除授权，仅对缺少关键信息或超出授权的动作提问。

## 按需执行

复用 [maintenance.py](scripts/maintenance.py)，需 Python 3.11+ 和已有 YAML 解析器。

| 任务 | 操作与参考 |
| --- | --- |
| 盘点 | 定位实际 `CODEX_HOME`，运行 `inventory`；原生状态查询见[核对参考](references/checks.md#原生查询与文件检查) |
| 检查更新 | `check-updates` 对账用户安装与来源台账；状态含义和命令见[脚本使用](references/automation.md#来源台账) |
| 比较、更新独立 skill | 准备候选，`compare` → `plan` → 在既有授权内 `apply`；备份与恢复见[操作说明](references/automation.md#预览备份与回滚) |
| 清理残留、重复 | 核对替代能力和精确路径清单，按[清理证据](references/checks.md#清理证据)执行 |

普通盘点不计算内容哈希；需要完整内容证据时用 `inventory --deep` 或对具体候选 `compare`。脚本保留不同实例及链接别名，按新旧插件格式发现组件；详细覆盖、错误字段和 Windows 用法按需查阅[脚本使用](references/automation.md)。

## 关键判断

- 缓存存在、配置声明或 `unspecified` 不证明启用或 MCP 握手成功，用原生列表补充有效状态。同名 MCP 保留插件与用户配置来源；环境变量和 headers 仅输出键名，不展示启动参数或 URL。
- 未登记、来源未知、失败和跳过项都进入覆盖说明；零更新不等于全部最新。系统、插件、链接目标、项目级入口和其他客户端按实际范围另查，不能宣称全电脑覆盖。
- 官方内置及官方管理组件用所属机制维护，不手动覆盖缓存或重复安装独立版。插件卸载用原生接口；禁用 MCP 的旧路径不等于垃圾，删除用户覆盖项可能启用插件同名服务。
- 独立 skill 先核实来源，按固定提交比较整个目录；保留本地定制、调用策略和元数据。仓库提交改变而 skill 目录未变不算更新，来源未知不按同名搜索结果替换。`register` 是持久写入，只读检查时不顺便登记。
- 同名或用途相近不等于重复，移除前比较完整能力并确认替代版实际可发现。`allow_implicit_invocation: false` 未自动出现不是损坏。

## 执行与验证

脚本仅替换真实的用户 skill 直接子目录，拒绝系统、插件和含链接的包；不支持的布局按现场证据人工维护。执行前避开其他任务的活跃修改和加载，保留回执及备份；替换并非整体原子操作，中断后先查回执，回滚不覆盖后续修改。

清理前复核身份、引用和占用，失败不当作“无占用”，活跃或已变化对象跳过，不沿链接递归删除。对话、记忆、数据库、项目、个人文件、维护台账与备份不作为安装残留清理。

验证配置可解析、入口可发现、策略有效及目标状态；脚本修改运行相关测试。汇报已处理、验证结果、未覆盖与原因，必要时附恢复命令。盘点不冒充业务验收，完成已授权项目后及时交付。
