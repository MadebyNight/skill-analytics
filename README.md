# Codex Skill 本机使用分析

这个工具从 Codex 本机会话转录中提取可观测的 Skill 使用记录，保存到 SQLite，并生成可直接打开的静态 HTML 仪表盘。它支持历史回填和 `Stop` Hook 增量更新，不需要常驻服务或第三方依赖。

## 3 分钟快速开始

需要 Python 3.10 或更高版本。以下命令均在项目根目录的 PowerShell 中执行。

```powershell
# 1. 回填已有 Codex 会话
python scanner.py backfill

# 2. 生成并打开仪表盘
python report.py
Invoke-Item .\data\dashboard.html
```

默认数据来自 `~/.codex/sessions/`，数据库写入 `data/analytics.db`，报告生成到 `data/dashboard.html`。重复回填不会重复计数。

如需在以后的 Codex 回合结束时自动更新报告，再执行：

```powershell
python install_hook.py install
```

安装后必须在 Codex 中运行 `/hooks`，检查并信任新 Hook。此后正常使用 Codex 即可，无需手工启动服务。

只想手动更新时，重复运行下面两条命令：

```powershell
python scanner.py backfill
python report.py
```

## 统计口径

一次“调用”指 Codex 在真实工具调用中读取某个具体的 `SKILL.md`。同一会话、同一回合、同一规范路径只计一次；同一 Skill 在不同回合再次读取会分别计数。

工具不会根据 Skill 清单、普通消息、提示词中的名称或工具输出推测调用，也不会判断某个 Skill 是否“本该使用”。主代理、子代理和无法可靠识别的代理分别记为 `main`、`subagent` 和 `unknown`。

当前解析器已按本机转录验证以下格式：

- 顶层事件为 `response_item`；
- `payload.type` 为 `custom_tool_call`，`payload.name` 为 `exec`；
- `payload.input` 中存在真实的 `tools.exec_command(...)` 调用；
- 实际命令使用 `Get-Content`、`gc`、`cat`、`type`、`head`、`tail`、`sed` 或 `bat` 读取具体 `SKILL.md`；
- 优先使用 `internal_chat_message_metadata_passthrough.turn_id` 去重。

当前版本不支持尚未在本机转录中观测到的直接 Skill tool、MCP Skill 读取事件或其他内部调用格式。若 Codex 没有把行为写入本地转录，也无法统计。

## 环境

- 需要 Python 3.10 或更高版本；
- 已在 Windows、Python 3.13.13、Codex CLI 0.153.4 验证；
- 只使用 Python 标准库；
- 默认 Codex 目录为环境变量 `CODEX_HOME`，未设置时使用 `~/.codex`。

以下命令均在项目根目录执行。

## 历史回填与单文件扫描

回填 `CODEX_HOME/sessions/**/*.jsonl`：

```powershell
python scanner.py backfill
```

成功后会输出本次处理的文件数、行数、新增调用数、重复数和错误数。示例：

```text
{"files": 12, "lines": 850, "inserted": 24, "duplicates": 0, "parse_errors": 0, "retained_bytes": 0}
```

增量扫描一个转录文件：

```powershell
python scanner.py scan <transcript-path>
```

扫描游标保存在数据库中。重复回填或重复扫描不会重复计数；转录追加后只处理新增的完整 JSONL 行。

## 生成报告

```powershell
python report.py
Invoke-Item .\data\dashboard.html
```

也可指定数据库和输出文件：

```powershell
python report.py --db <database-path> --output <html-path>
```

报告包含总览、日历热力图、周/月趋势、代理和项目目录分布、Skill 排行、从未使用及 30 天未使用清单、数据质量统计。生成结果是自包含 HTML，无需 Web 服务。

报告生成时会枚举当前安装的 Skill，包括 `CODEX_HOME/skills`、`~/.agents/skills` 和 `CODEX_HOME/plugins/cache`，因此从未调用的已安装 Skill 也能显示。

### 使用自定义 CODEX_HOME

如果 Codex 数据不在默认目录，先在当前 PowerShell 会话设置环境变量：

```powershell
$env:CODEX_HOME = "D:\my-codex-home"
python scanner.py backfill
python report.py
```

安装器和卸载器也会读取同一个 `CODEX_HOME`。环境变量未设置时，默认使用当前用户的 `~/.codex`。

## 安装 Stop Hook

```powershell
python install_hook.py install
```

安装器向 `CODEX_HOME/hooks.json` 的 `Stop` 数组追加本工具定义，保留其他事件和已有 Hook。已有配置首次发生实际变更前，会在同一目录生成 `hooks.json.backup-<时间戳>`；重复安装不会再次修改配置或创建备份。

安装后，请在 Codex 中通过 `/hooks` 检查并信任该 Hook。配置遵循 [OpenAI Codex Hooks 官方文档](https://developers.openai.com/codex/hooks) 的三层结构：事件数组包含 Hook group，group 的 `hooks` 数组包含 command handler；handler 使用 `type`、`command`、`statusMessage` 和 `async: true`。本机运行级信任仍由用户审查，安装器不会绕过该机制。

Hook 从标准输入接收 snake_case 或 camelCase 的转录与会话字段，增量扫描当前转录并重新生成报告。扫描或报告失败只写本地错误日志，Hook 始终成功退出，不向 Codex 返回阻断决定。

卸载：

```powershell
python install_hook.py uninstall
```

卸载器只移除与当前 Python 解释器及本项目 `hook.py` 完全匹配的定义，保留其他 Hook。若需要完整回滚，请先退出 Codex，再将安装输出指明的 `hooks.json.backup-<时间戳>` 手工复制回 `CODEX_HOME/hooks.json`。卸载不会自动覆盖配置，也不会删除备份。

## 常用命令

```text
python scanner.py backfill
python scanner.py scan <transcript-path>
python report.py [--db <database-path>] [--output <html-path>]
python install_hook.py install
python install_hook.py uninstall
```

扫描单个转录的示例：

```powershell
python scanner.py scan "C:\Users\<用户名>\.codex\sessions\2026\09\08\rollout-<id>.jsonl"
```

指定报告数据库和输出位置：

```powershell
python report.py --db .\data\analytics.db --output .\data\my-dashboard.html
```

查看命令帮助：

```powershell
python scanner.py --help
python scanner.py scan --help
python report.py --help
python install_hook.py --help
```

## 默认路径

| 内容 | 默认位置 |
| --- | --- |
| 会话转录 | `CODEX_HOME/sessions/` |
| Hook 配置 | `CODEX_HOME/hooks.json` |
| SQLite 数据库 | `<项目目录>/data/analytics.db` |
| 静态报告 | `<项目目录>/data/dashboard.html` |
| Hook 错误日志 | `<项目目录>/data/errors.log` |

## 数据与隐私

所有数据只保存在本机。数据库记录会话 ID、回合 ID、Skill 路径、调用时间、工作目录、代理分类、模型和采集来源；不会保存用户提示词、助手正文、工具输出、Skill 内容或完整 shell 命令。Skill 移动或删除后，历史调用路径仍会保留。

损坏的 JSONL 行会跳过并记录诊断，未知事件会忽略。解析失败、未知代理和扫描失败统计保存在 SQLite 的 `diagnostics` 表并展示在报告中；Hook 自身或报告生成异常写入 `data/errors.log`。日志只记录转录路径、异常类型和摘要，不写入原始 JSONL 内容。

## 故障排查

### 回填结果一直为 0

先确认实际使用的 Codex 目录和转录文件：

```powershell
Write-Output $env:CODEX_HOME
Get-ChildItem "$env:USERPROFILE\.codex\sessions" -Recurse -Filter *.jsonl | Select-Object -First 5
```

如果使用自定义目录，请先设置 `CODEX_HOME`。目录中有转录但调用量仍为 0，通常表示转录中没有当前版本支持的真实 Skill 读取格式。

### Hook 没有自动更新

依次检查：

1. 是否已经运行 `python install_hook.py install`；
2. 是否在 Codex `/hooks` 中信任了该 Hook；
3. 安装时使用的 Python 解释器和项目绝对路径是否仍然存在；
4. `data/errors.log` 是否记录异常；
5. 手工运行 `python scanner.py backfill` 和 `python report.py` 是否成功。

### 报告没有变化

重新生成并检查文件更新时间：

```powershell
python report.py
Get-Item .\data\dashboard.html | Select-Object FullName,Length,LastWriteTime
Invoke-Item .\data\dashboard.html
```

浏览器可能缓存本地文件。关闭页面后重新打开即可。

## 设计文档

完整统计口径、架构与成功标准见 [设计文档](docs/superpowers/specs/2026-09-07-codex-skill-analytics-design.md)。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试使用脱敏 fixture、临时数据库和临时 `CODEX_HOME`，覆盖回填幂等、增量扫描、误计数边界、代理分类、报告聚合、Hook 容错以及安装/重复安装/卸载。安装器测试不会修改真实用户的 `hooks.json`。
