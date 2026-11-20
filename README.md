# Unified Memory

用一套 Markdown 记忆库为 Claude Code、Codex、Cursor 和 Antigravity 提供上下文。框架包含本地 MCP 服务、中文全文与时间检索、Web 编辑和归档、技能管理、客户端接入与配置恢复。需要 macOS 或 Linux、Python 3.9 以上，仅使用标准库。

## 桌面版和命令行版

macOS 桌面版使用 Electron。Apple 芯片发行包为 `Unified-Memory-macOS-arm64.zip`。先将解压出的 **Unified Memory.app** 移到用户的 Applications 目录，再打开。应用包含 Python，首次打开会初始化个人库。侧栏提供记忆检索、技能、读写动态、客户端接入和共享同步。在「客户端接入」里可以选择要配置的客户端。关闭窗口会结束该窗口启动的本地服务。

独立命令行包为 `unified-memory-cli.tar.gz`，适用于有 Python 3.9+ 和 Git 的 macOS / Linux。解压到固定目录后运行：

```sh
cd unified-memory-cli
./bin/umem init
./bin/umem list
./bin/umem search "部署"
./bin/umem doctor
```

`umem read` 接受上一条结果的 `ref`。`umem write --help` 给出写入所需字段，可用 `--body-file -` 从标准输入读取 Markdown。把 `bin/umem` 链接到 PATH 中的个人 bin 目录即可从任何目录调用。同步 Git 仓库需要本机 Git 以及相应仓库权限。

桌面版会常驻 macOS 菜单栏：关闭窗口不会退出，菜单栏图标可打开读写动态、共享与同步，并提供「开机自动启动」开关（首次打开会询问一次）。

### 稳定入口

客户端（Cursor、Codex、Claude Code、Antigravity）的 MCP 与 Hook 配置不会直接指向源码目录或 `.app` 包内部，而是指向固定启动器 `~/Library/Application Support/Unified Memory/bin/`（Linux 为 `~/.local/share/unified-memory/bin/`）下的 `umem-mcp`、`umem-hook`、`umem`。启动器由 `memoryctl connect`、桌面版每次启动或 `memoryctl install-shims` 自动刷新；应用被移动或升级后，若记录的运行时已消失，启动器会回退到 `~/Applications`、`/Applications` 里的应用包，仍找不到则明确报错而不是静默失效。旧版直接写入 python 与 `server.py` 路径的配置会被识别为「待同步」，点「同步」即可迁移。

两个入口默认读取 `~/UnifiedMemory`。桌面窗口单独分配回环端口，原有的 8765 网页可以继续使用。已有 CLI 源码安装的用户，可在 macOS 的 `~/Library/Application Support/Unified Memory/backend.json` 配置 `framework` 和 `python` 两个绝对路径，让应用沿用同一份已升级的源码和解释器；新安装默认使用包内运行时。

### 本地构建

在 Apple 芯片 Mac 上用 Node.js 22.12+、npm、Python 3 和 macOS 命令行工具构建。源码开发使用 `cd desktop && npm ci && npm start`。在同一目录运行 `npm run package` 可生成桌面 ZIP。构建脚本验证内置 Python 发行包的 SHA-256，首次构建需要联网。另可提供同架构的可迁移运行时目录 `UMEM_PYTHON_DIST`。桌面产物为本地 ad-hoc 签名；正式面向公众分发还需 Developer ID 签名和 Apple 公证。

在仓库根目录运行 `python3 scripts/package_release.py --cli` 单独生成 CLI 包。两个发行包都位于 `dist/`。分发内容来自公开文件清单。

## 框架与数据

本仓库保存程序、空模板、通用记忆技能和合成数据测试。使用者的索引、条目和运行状态保存在单独的数据目录。可将同一份代码与不同的数据目录配合使用。

| 公开源码 | 每位使用者的私有数据目录 |
| --- | --- |
| `mcp/`、`manager/`、`scripts/` | `MEMORY.md` 和 L0–L4 五个条目目录 |
| `templates/`、内置的 `skills/unified-memory/` | `user-managed/` 和使用者自己的 `skills/` |
| `tests/` 及其虚构样本 | 本机运行状态位于 `~/.local/state/unified-memory/`（锁、回收站、恢复信息），不污染记忆仓库 |

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

## 多设备与团队共享（Git 同步与多库挂载）

支持将个人库绑定到私有 Git 仓库（GitHub / Gitee / 自建 Gitea 等）实现多设备同步，同时按库挂载只读（`ro`）或共写（`rw`）的团队共享库。共享库默认仅开放 `L1 / L3 / L4`，不会改写本机 `L0` 红线与 `L2` 个人偏好。

```sh
# 1. 为个人主库绑定私有 Git 远端并同步
python3 scripts/memoryctl.py sync setup personal --remote-url git@github.com:you/my-memory.git
python3 scripts/memoryctl.py sync run personal

# 2. 挂载团队共享库（支持自动克隆，按需选 ro 或 rw）
python3 scripts/memoryctl.py mount add team-kb --path ~/UnifiedMemory-Team --label "研发共享库" --mode rw --git git@gitee.com:team/memory-kb.git
python3 scripts/memoryctl.py sync run all
```

也可以直接在 Web 管理页顶部点击「共享与同步」图形化配置远端、增删共享库，或在条目页一键将个人记忆分享到共享库；多端同时改同一篇条目时，系统会保留冲突副本并在管理页提供行级 Diff 对比与一键合并。

## 验证与分发

运行 `python3 -m unittest discover -s tests -v`。回归测试在临时目录生成样本，并为客户端配置使用独立的临时 HOME。

发布源码仓库或从该仓库打包。备份个人记忆时备份数据目录。模板中写入示范格式，测试中使用虚构数据。

运行 `python3 scripts/package_release.py` 生成 `unified-memory-starter.zip`。打包脚本仅从源码根目录逐个读取清单中的文件，新增公开组件时同步更新清单。
