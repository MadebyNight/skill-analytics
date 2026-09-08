import { spawn } from "node:child_process"
import { basename, isAbsolute, join, resolve } from "node:path"

const pythonExecutable = __PYTHON_EXECUTABLE__
const projectRoot = __PROJECT_ROOT__
const databasePath = __DATABASE_PATH__

export default function (pi) {
  const readPaths = new Map()

  pi.on("tool_call", (event) => {
    try {
      if (event.toolName !== "read") return
      const path = event.input && event.input.path
      if (typeof path === "string") readPaths.set(event.toolCallId, path)
    } catch {
      return
    }
  })

  pi.on("tool_execution_end", (event, ctx) => {
    try {
      const path = readPaths.get(event.toolCallId)
      readPaths.delete(event.toolCallId)
      if (event.toolName !== "read" || event.isError) return
      if (typeof path !== "string" || basename(path).toLowerCase() !== "skill.md") return

      const normalizedPath = path.replace(/^@/, "")
      const absolutePath = isAbsolute(normalizedPath)
        ? resolve(normalizedPath)
        : resolve(ctx.cwd, normalizedPath)
      const child = spawn(
        pythonExecutable,
        [join(projectRoot, "analytics.py"), "record", "--platform", "pi", "--db", databasePath],
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
        sessionId: ctx.sessionManager.getSessionId(),
        toolCallId: event.toolCallId,
        toolName: event.toolName,
        args: { path: absolutePath },
        cwd: ctx.cwd,
        provider: ctx.model && ctx.model.provider,
        model: ctx.model && ctx.model.id,
        timestamp: new Date().toISOString(),
      }))
      child.unref()
    } catch {
      return
    }
  })
}
