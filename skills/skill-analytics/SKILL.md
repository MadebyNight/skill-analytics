---
name: skill-analytics
description: 安装、运行和排查本机 Skill 使用分析工具，覆盖 Codex、Claude Code、OpenCode 和 Pi。当用户想要自动统计 Skill 调用、生成本机 Skill 排行榜或仪表盘、查看从未使用的 Skill，或需要安装、验证、卸载这些 Hook 时使用。
metadata:
  short-description: 本机 Skill 使用统计与离线仪表盘
---

# Skill Analytics

把多个编码工具的 Skill 调用汇总到一个本机 SQLite 数据库，并生成一个可离线打开的 HTML 仪表盘。所有数据只留在本机。

本仓库既是安装器也是运行时。Skill 不内置工具副本，而是使用它当前所在的仓库检出。

## 硬性约束

以下不是风格偏好。违反会让工具失效或破坏用户配置。

1. **绝不手工编写或修改 Hook 配置。** 一律通过 `python analytics.py install --platform <平台>` 安装。安装器会在改动前备份、可重复执行、拒绝覆盖非自有文件，并能精确卸载自己写入的内容。手改 `hooks.json` 或 OpenCode 插件文件会丢掉这些保证。
2. **安装前先确认检出路径是固定的。** 已安装的 Hook 会记录本仓库和当前 Python 解释器的绝对路径。从临时目录安装，或之后移动、删除检出，都会让 Hook 静默失效。若用户的检出已移动，先尽力从旧路径卸载，再从新路径重装。
3. **未获用户确认不得安装。** 安装会写入 `~/.codex/hooks.json`、`~/.claude/settings.json`、`~/.config/opencode/plugins/` 或 `~/.pi/agent/extensions/`。先说明将改动哪个文件、会创建备份，得到用户同意后再执行。
4. **没有完成信任步骤，就不得告知用户安装完成。** 宿主在用户信任 Hook 之前可能跳过它。这一步不可省略。
5. **不得上传、同步或传输统计数据。** 本工具的设计前提是纯本机。不要添加遥测或远程上报。

## 安装

先判断用户实际在用的平台，再按 `references/install.md` 中对应小节执行。安装是按平台进行的，默认不为本机所有平台都装。

流程概要：

```text
1. 确认检出路径固定，并取得用户同意
2. 安装 Skill，让宿主能加载（见 references/install.md）
3. python analytics.py install --platform <平台>
4. 告知用户如何信任 Hook
5. python analytics.py status
```

第 5 步是验证关口。在 `status` 显示集成已安装之前，安装都算未完成。

## 使用

```text
python analytics.py scan-all      # 回填所有已安装平台的历史
python analytics.py scan --platform <平台>
python analytics.py report        # 生成离线仪表盘
python analytics.py status        # 路径、扫描时间、集成状态
python analytics.py uninstall --platform <平台>
python analytics.py prune --older-than <天数>   # 预览将删除的历史记录
python analytics.py compact                      # 回收已删除数据占用的空间
```

首次扫描后，从 `status` 或 `report` 的输出读取仪表盘路径，并把该路径给用户。仪表盘是单个自包含 HTML 文件，不依赖 CDN 或网络。

汇报数字时使用工具自身输出，不要估算，也不要依据报表“从未使用”之外的信息判断某个 Skill 未被使用。

## 排查

当 `status` 不是 `ready`、计数异常，或实时数据不更新时，读 `references/troubleshooting.md`。

## 清理历史数据

数据库在用户数据目录下缓慢增长，日常使用多年通常只有几十 MB，不是必须定期清理。仅当用户明确要求清理、或磁盘空间确实紧张时才执行。

```text
python analytics.py prune --older-than <天数>          # 默认只预览，不删除
python analytics.py prune --older-than <天数> --yes    # 实际删除
python analytics.py prune --diagnostics-only --yes     # 只清诊断记录
python analytics.py compact                            # 真正释放磁盘空间
```

必须遵守：

1. **先预览再删除。** 不带 `--yes` 的 `prune` 只报告将删除多少行。除非用户已明确同意删除，否则不要加 `--yes`。
2. **删除后提醒执行 `compact`。** 只删行不会缩小文件，空间不会真正归还。
3. **不要用清理替代卸载。** 想停止统计应使用 `uninstall`，不要靠删除数据达到目的。
4. **不要删除数据库文件本身。** 需要缩减历史时用 `prune`，直接删除会丢掉全部统计。

`installed_skills` 不会被清理，否则“从未使用”会算错。更完整的说明见仓库 README 的“历史数据清理”一节。

## 卸载

```text
python analytics.py uninstall --platform <平台>
```

卸载只移除本工具自己的 Hook 定义或生成的集成文件，不删除数据库、仪表盘或备份。除用户明确要求外，不要删除这些内容。
