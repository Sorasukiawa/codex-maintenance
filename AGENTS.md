# Codex 维护技能开发约定

源码入口为 `skills/codex-maintenance/SKILL.md`，实现为 `skills/codex-maintenance/scripts/maintenance.py`；安装在其他目录的实例可能有个人定制，不随源码修改自动覆盖。

- Python 3.11+；从仓库根目录运行 `python3 -B -m unittest discover -s skills/codex-maintenance/tests -q`。
- 保留只读盘点、来源登记、计划和执行的区别。元数据检查不证明 MCP 握手；更新报告不可把未登记、未知、跳过或失败当作最新。
- 配置值、启动参数、URL 和凭据不进入诊断输出。链接目标检查只补覆盖，不扩大 register / plan / apply 的写入范围。
- 新行为用隔离临时目录验证；测试不连接真实 MCP、不修改真实安装。机器台账、个人配置和维护备份不提交到本仓库。
- 合入前检查：`$(command -v python3.12 || command -v python3.11 || command -v python3) -B -m unittest discover -s skills/codex-maintenance/tests -q`（需 Python 3.11+ 跑隔离夹具单测）。
