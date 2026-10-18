// agentic guardrails — hard blocks for the interactive omp of the `agent` account.
// Source: /etc/agentic/guidance (root-owned). Edits in ~/.omp/agent are overwritten.
// The agent has full root (sudo). Only two things are blocked: wiping a whole home/work tree by mistake, and
// writing the files that define how the agent is judged (guidance, specs, supervisor state). No block asks the user.
// @ts-nocheck

const HOME = process.env.HOME || "/home/agent";

// Paths whose contents only root manages. Writing or deleting them is always blocked.
const MANAGED = [
  `${HOME}/.omp/agent/RULES.md`,
  `${HOME}/.omp/agent/AGENTS.md`,
  `${HOME}/.omp/agent/WATCHDOG.md`,
  `${HOME}/.omp/agent/rules`,
  `${HOME}/.omp/agent/skills`,
  `${HOME}/.omp/agent/extensions`,
  `${HOME}/.omp/agent/models.yml`,
  "/etc/agentic", "/opt/agentic", "/var/lib/agentic", "/var/lib/agplan", "/var/spool/agentic",
];
const MANAGED_NAME = /(^|\/)SPEC(-[A-Z])?\.md$/;

// Targets that rm -r must never touch.
const PROTECTED_RM = new Set(["/", "~", "~/", "$HOME", "$HOME/", HOME, `${HOME}/`, "~/work", "~/work/",
  `${HOME}/work`, `${HOME}/work/`, "~/.omp", `${HOME}/.omp`, "*", ".", "..", ".git", "./.git"]);

function expand(p: string): string {
  if (p === "~") return HOME;
  if (p.startsWith("~/")) return HOME + p.slice(1);
  return p.replace(/^\$HOME/, HOME);
}

function isManaged(path: string): boolean {
  const p = expand(String(path)).replace(/\/+$/, "");
  if (MANAGED_NAME.test(p)) return true;
  return MANAGED.some((m) => p === m || p.startsWith(m + "/"));
}

// Split a shell line into simple commands (good enough for policy: we only look for clear patterns).
function commands(line: string): string[][] {
  return line
    .split(/&&|\|\||;|\||\n/)
    .map((part) => part.trim().split(/\s+/).filter(Boolean))
    .filter((words) => words.length > 0);
}

function checkBash(line: string): string | null {
  for (let words of commands(line)) {
    while (words.length && /^(sudo|env|nice|nohup|timeout|doas)$/.test(words[0].split("/").pop() || "")) {
      words = words.slice(1).filter((w, i) => i > 0 || !w.startsWith("-"));  // drop the wrapper and its first flags
    }
    if (!words.length) continue;
    const [cmd, ...args] = words;
    const name = cmd.split("/").pop();
    if (name === "chattr" && args.some(isManaged)) return "these files define how you are judged";
    if (name === "rm") {
      const flags = args.filter((a) => a.startsWith("-")).join("");
      const recursive = /r|R|--recursive/.test(flags);
      const targets = args.filter((a) => !a.startsWith("-"));
      if (recursive && targets.some((t) => PROTECTED_RM.has(t) || PROTECTED_RM.has(expand(t)))) {
        return "this would wipe a whole home, work, .omp, or .git tree; remove the specific paths you mean";
      }
      if (targets.some(isManaged)) return "these files define how you are judged";
    }
    // Writes into managed paths through the shell. Reading them stays allowed.
    const operands = args.filter((a) => !a.startsWith("-"));
    let written: string[] = [];
    if (name === "mv" || name === "tee" || name === "truncate") written = operands;
    else if (name === "cp" || name === "ln" || name === "install") written = operands.slice(-1);
    else if ((name === "sed" || name === "perl") && args.some((a) => /^-[a-zA-Z]*i/.test(a))) written = operands;
    if (written.some(isManaged)) return "these files define how you are judged";
  }
  const redirect = line.match(/>{1,2}\s*([^\s;&|]+)/g) || [];
  for (const r of redirect) {
    const target = r.replace(/^>{1,2}\s*/, "");
    if (isManaged(target)) return "these files define how you are judged";
  }
  return null;
}

function pathsOf(input: unknown): string[] {
  const found: string[] = [];
  const visit = (v: unknown, key: string) => {
    if (typeof v === "string" && /path|file/i.test(key)) found.push(v);
    else if (Array.isArray(v)) v.forEach((x) => visit(x, key));
    else if (v && typeof v === "object") for (const [k, x] of Object.entries(v)) visit(x, k);
  };
  visit(input, "");
  return found;
}

export default function (pi) {
  pi.on("tool_call", async (event) => {
    const tool = String(event.toolName || "");
    const input = event.input || {};
    let reason: string | null = null;
    if (tool === "bash" && typeof input.command === "string") {
      reason = checkBash(input.command);
    } else if (/^(edit|write|ast_edit|notebook|apply_patch)$/.test(tool)) {
      if (pathsOf(input).some(isManaged)) reason = "these files are managed by root";
    }
    if (reason) {
      return { block: true, reason: `Blocked by guardrails: ${reason}. Choose another way to do the job yourself; do not ask the user.` };
    }
  });
}
