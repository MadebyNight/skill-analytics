import { spawn } from "node:child_process"
import { join } from "node:path"

const pythonExecutable = __PYTHON_EXECUTABLE__
const projectRoot = __PROJECT_ROOT__
const databasePath = __DATABASE_PATH__

export const SkillAnalyticsPlugin = async () => ({
  "tool.execute.after": async (input) => {
    try {
      if (input.tool !== "skill") return
      if (!input.sessionID || !input.callID || !input.args || typeof input.args.name !== "string") return

      const child = spawn(
        pythonExecutable,
        [join(projectRoot, "analytics.py"), "record", "--platform", "opencode", "--db", databasePath],
        {
          detached: true,
          windowsHide: true,
          stdio: ["pipe", "ignore", "ignore"],
          shell: false,
        },
      )
      child.on("error", () => {})
      child.stdin.on("error", () => {})
      child.stdin.end(JSON.stringify({
        tool: input.tool,
        sessionID: input.sessionID,
        callID: input.callID,
        args: { name: input.args.name },
      }))
      child.unref()
    } catch {
      return
    }
  },
})
