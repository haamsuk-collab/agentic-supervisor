// workspace-state — one short line of facts at the start of each user turn, so the agent does not spend
// tool calls rediscovering where it is. Source: /etc/agentic/guidance (root-owned). About 30 tokens per turn.
// @ts-nocheck
import { execFileSync } from "node:child_process";

function run(cmd: string, args: string[], cwd: string): string {
  try {
    return execFileSync(cmd, args, { cwd, timeout: 2000, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  } catch {
    return "";
  }
}

export default function (pi) {
  pi.on("before_agent_start", async (_event, ctx) => {
    const cwd = ctx?.cwd || process.cwd();
    const branch = run("git", ["rev-parse", "--abbrev-ref", "HEAD"], cwd);
    let git = "not a git repo";
    if (branch) {
      const changed = run("git", ["status", "--short"], cwd).split("\n").filter(Boolean).length;
      git = `git ${branch}, ${changed} changed file(s)`;
    }
    const missions = run("systemctl", ["list-units", "agentic@*", "--state=active", "--no-legend", "--plain"], cwd)
      .split("\n").filter(Boolean).length;
    const content = `[workspace] cwd ${cwd} · ${git} · active missions ${missions}`;
    return { message: { customType: "workspace-state", content, display: false } };
  });
}
