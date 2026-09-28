# codex-maintenance

[English](#english) · [简体中文](#简体中文) · [繁體中文](#繁體中文) · [日本語](#日本語)

### English

A community skill for auditing and maintaining local Codex skills, MCP configurations, and plugins. It distinguishes official managed components from independent installations, preserves local customizations, and provides update previews, backups, and rollback.

Supports native Windows, macOS, and Linux with Python 3.11+.

### 简体中文

用于检查和维护 Codex 的本地 skills、MCP 配置和插件。区分官方管理组件与独立安装，保留本地定制，并为经过审阅的更新提供预览、备份和回滚。[简中详细说明](README.zh-CN.md)

支持原生 Windows、macOS 和 Linux，需要 Python 3.11+。

### 繁體中文

用於檢查與維護 Codex 的本機 skills、MCP 設定與外掛。區分官方管理元件與獨立安裝，保留本機自訂內容，並為審閱後的更新提供預覽、備份與復原。

支援原生 Windows、macOS 與 Linux，需要 Python 3.11+。

### 日本語

Codex のローカルスキル、MCP 設定、プラグインを点検・保守するためのスキルです。公式管理のコンポーネントと個別に導入したスキルを区別し、ローカルのカスタマイズを維持しながら、更新内容のプレビュー、バックアップ、ロールバックを行えます。

ネイティブ Windows、macOS、Linux に対応しています。Python 3.11 以降が必要です。

This project is not affiliated with OpenAI. The technical guide below is maintained in English; a [detailed Simplified Chinese guide](README.zh-CN.md) is also available.

## Install

Ask Codex:

```text
$skill-installer Install skills/codex-maintenance from
https://github.com/Sorasukiawa/codex-maintenance.
If an equivalent skill is already installed, inspect its source and local changes first;
do not install a duplicate.
```

Alternatively, copy the complete `skills/codex-maintenance` directory into the user skill directory supported by your Codex installation. Use one discovery location, following the [official skill documentation](https://learn.chatgpt.com/docs/build-skills).

Then ask:

```text
Use $codex-maintenance to audit my local skills, MCP servers, and plugins.
Keep official managed versions under their existing manager and avoid duplicate installs.
Show evidence for maintenance candidates and carry out actions within my stated authorization.
```

The skill instructions are currently in Chinese. Ask Codex to respond in your preferred language.

## Requirements and commands

Python 3.11+ on native Windows, macOS, or Linux, with either PyYAML available to that interpreter or Ruby Psych on `PATH`. The tool does not install dependencies. Windows uses a native file handle and `msvcrt` locking; macOS/Linux use `fcntl`.

From the repository root:

```sh
python3 -B skills/codex-maintenance/scripts/maintenance.py --help
python3 -B skills/codex-maintenance/scripts/maintenance.py inventory
# Include full content hashes when needed:
python3 -B skills/codex-maintenance/scripts/maintenance.py inventory --deep
python3 -B skills/codex-maintenance/scripts/maintenance.py check-updates
```

On Windows, use PowerShell and your installed Python 3.11+ interpreter (`py -3` below; `python` also works when it selects the correct version):

```powershell
py -3 --version
$tool = '.\skills\codex-maintenance\scripts\maintenance.py'
$codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
py -3 -B $tool --home $codexRoot --output .\work\inventory.json inventory
py -3 -B $tool --home $codexRoot check-updates
```

If neither YAML parser is available, choose an existing interpreter with one installed. To set up PyYAML explicitly in the selected Windows interpreter, run `py -3 -m pip install 'PyYAML>=6,<7'`. This is an optional manual setup step, not performed by the skill. UTF-8 files (including BOM/CRLF) and Unicode paths are supported. JSON files use UTF-8; console JSON escapes Unicode for legacy code pages.

`inventory` is read-only and defaults to metadata inspection. Add `--deep` for full content hashes. YAML documents are parsed in bounded batches, reducing Ruby process startup overhead while isolating invalid documents; file contents and invocation policies are reread on each run.

`register` persists a verified source or local baseline. `check-updates` reconciles installed user skills with the registry and reads public GitHub metadata for confirmed sources without installing anything. Unregistered skills, unknown sources, failed checks, and an unreadable registry remain explicit in its coverage summary; zero updates does not mean all skills are current.

`compare` and `plan` describe a complete staged candidate. `apply` replaces a reviewed user skill while retaining a backup; `rollback` refuses to overwrite subsequent edits. Plans are not a substitute for user authorization.

Inventory and update reports use `schema=2`; registries, plans, and receipts remain at schema 1. Consumers that need inventory fingerprints must request `inventory --deep`. Read failures retain partial results and sanitized reason codes.

Pass global options `--home` and `--output` before the subcommand. State and backups live in `CODEX_HOME/maintenance`, outside the distributed skill. See the [command and recovery reference](skills/codex-maintenance/references/automation.md).

## Boundaries

The filesystem inventory covers `CODEX_HOME/skills`, `.system`, standard plugin caches, and user `config.toml`. Other discovery roots such as `~/.agents/skills`, project configuration, and nonstandard layouts need supplementary inspection. Portable plugins use root `plugin.json`, `skills/`, and `mcp.json`; legacy plugins use `.codex-plugin/plugin.json` and its declared component paths. Coexisting manifests do not duplicate components, and missing explicitly declared paths are reported. Cache entries do not establish effective enablement or a successful MCP handshake.

Automatic replacement supports only real direct children of `CODEX_HOME/skills`. Managed directories and packages containing symlinks, junctions, or other reparse points are excluded. Windows support targets ordinary local directories; OneDrive placeholders and network shares are not validated update targets. Windows cannot reliably compare POSIX executable bits or ACLs: local executable bits are reported as unknown and excluded from local/upstream comparisons. Upstream-to-upstream mode changes remain visible. There is no automatic semantic merge, candidate download, plugin uninstall, or general cache deletion. Unknown sources and network failures remain unknown.

Updates use two directory renames and have a brief gap. They are not a single atomic transaction and do not guarantee power-loss durability. The lock coordinates this tool, not other editors or running agents.

On Windows, an open file may prevent a directory rename. Close applications using the affected skill, inspect the retained directories/receipt, and create a fresh plan before retrying. WSL uses the Linux implementation for its own Codex environment; cross-maintenance of Windows installations through `/mnt/c` is not covered by the native Windows tests.

MCP environment values, URLs, and argument contents are omitted from reports. Reports still contain local paths and component names: use sanitized minimal reproductions when filing issues. Do not upload real configuration, source registries, backups, or conversations.

## Test and contribute

```sh
python3 -B -m unittest discover -s skills/codex-maintenance/tests -v
python3 -B skills/codex-maintenance/scripts/maintenance.py validate skills/codex-maintenance
```

Tests use temporary installations and mocked GitHub responses. CI covers native Windows (Python 3.11 and 3.13), macOS, Linux, and both YAML parser paths. Windows tests exercise real junctions, file sharing, and process locks as well as updates and rollback. Contributions that improve installation-layout handling, configuration validation, and recovery behavior are welcome; include a focused test for behavioral changes.

Design research included [EfanWang/skills-manager](https://github.com/EfanWang/skills-manager) and [GrubbyLee/skill-manager](https://github.com/GrubbyLee/skill-manager). This tool is independently implemented, with a focus on Codex managed components and local customizations.

Released under the [MIT License](LICENSE).
