# 安装参考

所有命令都在仓库根目录执行。先把 `REPO` 设为检出的绝对路径，并保持固定。

```bash
cd <仓库检出目录>
```

按用户实际使用的宿主选择对应小节。除用户要求，不要一次给多个平台安装。

## Codex

1. 安装 Skill，让 Codex 能加载。Codex 从 `~/.codex/skills/` 读取个人 Skill：

   ```bash
   # macOS / Linux
   mkdir -p ~/.codex/skills && cp -r skills/skill-analytics ~/.codex/skills/
   ```

   ```powershell
   # Windows
   New-Item -ItemType Directory -Force "$HOME\.codex\skills" | Out-Null
   Copy-Item -Recurse skills\skill-analytics "$HOME\.codex\skills\"
   ```

2. 安装 Hook：

   ```bash
   python analytics.py install --platform codex
   ```

   这会把一个异步 `Stop` Hook 合并进 `~/.codex/hooks.json`，文件已存在时会创建带时间戳的备份。

3. **必须执行的信任步骤。** 让用户在 Codex 中打开 `/hooks`，检查并信任新增的 Hook。Codex 会跳过未受信任的 Hook，表现为安装成功但没有任何数据。

4. 验证：

   ```bash
   python analytics.py status
   ```

## Claude Code

1. 安装 Skill。Claude Code 从 `~/.claude/skills/` 读取个人 Skill：

   ```bash
   mkdir -p ~/.claude/skills && cp -r skills/skill-analytics ~/.claude/skills/   # macOS / Linux
   ```

   ```powershell
   Copy-Item -Recurse skills\skill-analytics "$HOME\.claude\skills\"   # Windows
   ```

2. 安装 Hook：

   ```bash
   python analytics.py install --platform claude
   ```

   这会把 `PostToolUse` 和 `UserPromptExpansion` 两个 Hook 合并进 `~/.claude/settings.json`，并创建备份。

3. **必须执行的信任步骤。** Claude Code 对 Hook 施加 workspace trust 约束。若提示信任工作区或 Hook，用户必须接受，否则 Hook 不会运行。

4. 验证：

   ```bash
   python analytics.py status
   ```

## OpenCode

1. 安装 Skill。OpenCode 从 `~/.config/opencode/skills/` 读取全局 Skill：

   ```bash
   mkdir -p ~/.config/opencode/skills && cp -r skills/skill-analytics ~/.config/opencode/skills/   # macOS / Linux
   ```

   ```powershell
   Copy-Item -Recurse skills\skill-analytics "$HOME\.config\opencode\skills\"   # Windows
   ```

2. 安装插件：

   ```bash
   python analytics.py install --platform opencode
   ```

   这会写入单个生成文件 `~/.config/opencode/plugins/opencode-skill-analytics.js`。若该路径已存在文件但没有本工具的生成标记，安装器会拒绝覆盖并报告 conflict。不要绕过该冲突，先向用户确认文件归属。

3. 无独立信任步骤。重启 OpenCode 使其加载插件。

4. 验证：

   ```bash
   python analytics.py status
   ```

## Pi

1. 安装 Skill。Pi 从 `~/.pi/agent/skills/` 读取 Skill：

   ```bash
   mkdir -p ~/.pi/agent/skills && cp -r skills/skill-analytics ~/.pi/agent/skills/   # macOS / Linux
   ```

   ```powershell
   Copy-Item -Recurse skills\skill-analytics "$HOME\.pi\agent\skills\"   # Windows
   ```

2. 安装扩展：

   ```bash
   python analytics.py install --platform pi
   ```

   这会写入 `~/.pi/agent/extensions/pi-skill-analytics.ts`，具备与 OpenCode 相同的生成文件保护。

3. 无独立信任步骤。重启 Pi 使其加载扩展。

4. 验证：

   ```bash
   python analytics.py status
   ```

## 安装后

安装完成后不需要定期维护。数据库位于用户数据目录，增长缓慢。若用户日后要求清理历史，按仓库 README 的“历史数据清理”一节执行 `prune` 与 `compact`，并始终先预览再删除。

## 通用验证

`python analytics.py status` 会按平台输出。重点看：

- `integration_status` —— `installed` 表示 Hook 或生成文件存在，且与安装器将要写入的内容一致。`not_installed`、`partial`、`conflict` 都需要处理。
- `last_history_scan_at` —— 由 `scan` / `scan-all` 更新。
- `last_realtime_at` —— 由 Hook 或插件事件更新。若 `integration_status` 为 `installed` 但真实使用 Skill 后该时间不推进，说明宿主没有投递事件，回到信任步骤复查。

端到端确认：在宿主中真实触发一次 Skill 调用，然后执行：

```bash
python analytics.py report
```

## Python

需要 Python 3.10 或更高版本，只使用标准库。

已安装的 Hook 会记录执行安装的那个解释器的绝对路径。若该解释器之后被删除或替换，先卸载再重装：

```bash
python analytics.py uninstall --platform <平台>
python analytics.py install --platform <平台>
```

Windows 上可执行文件通常是 `python`，macOS 和 Linux 通常是 `python3`。请用用户会持续使用的那个命令安装。
