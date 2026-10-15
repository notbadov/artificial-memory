import { spawnSync } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const configRoot = dirname(dirname(fileURLToPath(import.meta.url)))
const runtimeRoot = join(configRoot, "agent-platform")
const gateScript = join(runtimeRoot, "scripts", "learning_gate.py")

function sessionId(event) {
  return event?.properties?.sessionID || event?.properties?.sessionId || event?.sessionID || "opencode"
}

function gateStatus(id) {
  if (!existsSync(gateScript)) return null
  const result = spawnSync(process.env.PYTHON || "python3", [gateScript, "status"], {
    env: { ...process.env, AGENT_SESSION_ID: id }, encoding: "utf8", timeout: 5000,
  })
  if (result.status !== 0 || !result.stdout.trim()) return null
  try { return JSON.parse(result.stdout) } catch { return null }
}

export const AgentPlatform = async ({ client }) => ({
  "shell.env": async (_input, output) => {
    output.env.AGENT_PLATFORM_PROVIDER = "opencode"
    output.env.AGENT_PLATFORM_MODE ||= process.env.AGENT_PLATFORM_MODE || "balanced-economy"
    output.env.AGENT_SKILLS_ROOT = join(configRoot, "skills")
  },
  "experimental.session.compacting": async (_input, output) => {
    const file = join(runtimeRoot, "compaction.md")
    if (existsSync(file)) output.context.push(readFileSync(file, "utf8"))
  },
  event: async ({ event }) => {
    if (event?.type !== "session.idle") return
    const state = gateStatus(sessionId(event))
    if (!state || state.status !== "armed") return
    await client.app.log({ body: { service: "agent-platform", level: "error", message: "Defect learning gate is still armed", extra: state } })
    throw new Error("Defect learning gate is still armed; finish regression evidence and the Agent Memory learning proposal.")
  },
})
