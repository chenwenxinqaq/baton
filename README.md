# Unified Memory

用一套 Markdown 记忆库为 Claude Code、Codex、Cursor 和 Antigravity 提供上下文。框架包含本地 MCP 服务、中文全文与时间检索、Web 编辑和归档、技能管理、客户端接入与配置恢复。需要 macOS 或 Linux、Python 3.9 以上，仅使用标准库。

## 框架与数据

本仓库保存程序、空模板、通用记忆技能和合成数据测试。使用者的索引、条目和运行状态保存在单独的数据目录。可将同一份代码与不同的数据目录配合使用。

| 公开源码 | 每位使用者的私有数据目录 |
| --- | --- |
| `mcp/`、`manager/`、`scripts/` | `MEMORY.md` 和 L0–L4 五个条目目录 |
| `templates/`、内置的 `skills/unified-memory/` | `user-managed/` 和使用者自己的 `skills/` |
| `tests/` 及其虚构样本 | `.state/` 中的锁、备份、回收站和客户端恢复信息 |

## 从空库开始

在源码目录运行。默认数据位置为 `~/UnifiedMemory`，也可以预先设置 `UNIFIED_MEMORY_ROOT` 指向仓库之外的目录。命令行和管理页需要使用相同的环境变量。

```sh
export UNIFIED_MEMORY_ROOT="$HOME/my-private-memory"
python3 scripts/memoryctl.py init
python3 scripts/memoryctl.py doctor
python3 manager/manager.py
```

打开 http://127.0.0.1:8765/ ，新建一条 L1 方法并搜索标题。初始化可重复执行，已存在的索引和技能会保留。导入已有同格式库时直接指定其根目录，运行 doctor 检查索引。

## Agent 如何决定写什么

公开版的写入规约位于 [空库模板的 §0](templates/library.md)。它规定何时保存、何时跳过、如何选择 L0–L4、怎样合并已有主题，以及何时撤回或归档旧结论。`init` 把规约和三份条目模板放进新库。`connect` 将同一份公开规约写入 Claude Code、Codex 和 Antigravity 各自的接入指令。Cursor 通过全局 MCP 配置和本地技能目录接入。接入时只读取公开模板，个人索引和正文留在私有库。

从早期公开版升级时，在原数据目录配置下重跑 `init` 补齐缺少的模板，再对已接入的客户端重跑 `connect` 并重开会话。新规约随客户端指令生效，已有私有索引和自定义模板会保留。

## 接入客户端

以下以 Codex 为例，也可选择 claude、cursor 或 antigravity。接入会更新该客户端的指令与 MCP 配置，命令指向源码目录中的服务程序，参数指向当前选定的数据目录。客户端重新启动后能继续找到该库。

```sh
python3 scripts/memoryctl.py connect codex --dry-run
python3 scripts/memoryctl.py connect codex
```

需要解除接入时，在相同数据目录配置下运行 `python3 scripts/memoryctl.py disconnect codex`。更换数据目录时先解除旧接入。自己的技能放在数据目录的 `skills/<名称>/SKILL.md`，可通过管理页或 CLI 启用、停用和同步。

技能使用的共享文档也可以留在私有库。例如在 `skills/resources.json` 写入 `{"report-helper": ["shared-guides"]}`，并在 `skills/shared-guides/` 放置文档。启用 report-helper 时，客户端会同步挂载该资源目录。多个技能可以引用同一资源。

## 验证与分发

运行 `python3 -m unittest discover -s tests -v`。回归测试在临时目录生成样本，并为客户端配置使用独立的临时 HOME。

发布源码仓库或从该仓库打包。备份个人记忆时备份数据目录。模板中写入示范格式，测试中使用虚构数据。

运行 `python3 scripts/package_release.py` 生成 `unified-memory-starter.zip`。打包脚本仅从源码根目录逐个读取清单中的文件，新增公开组件时同步更新清单。
