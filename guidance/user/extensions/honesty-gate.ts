// honesty-gate — deterministic checks before the interactive agent finishes a turn (no model tokens).
// Source: /etc/agentic/guidance (root-owned). Two checks, at most one continuation per turn:
//   1. every file the agent wrote or edited this turn is named in its final reply, and still exists;
//   2. success words ("통과했다", "고쳤다", "passed", "fixed"...) need at least one command or test run this turn.
// @ts-nocheck
import fs from "node:fs";
import path from "node:path";

const VERDICT = /(완료했|고쳤|해결했|통과했|확인했|정상\s*동작|\b(fixed|passed|verified|resolved|works now)\b)/i;
const WRITE_TOOLS = /^(edit|write|ast_edit|apply_patch|notebook)$/;

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

function textOf(message: any): string {
  const content = message?.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.filter((b) => b?.type === "text").map((b) => String(b.text || "")).join("\n");
}

export default function (pi) {
  let written = new Map<string, string>(); // absolute path -> as given
  let ranCommand = false;
  let lastText = "";
  let nudged = false;

  pi.on("agent_start", () => {
    written = new Map(); ranCommand = false; lastText = ""; nudged = false;
  });

  pi.on("tool_call", async (event, ctx) => {
    const tool = String(event.toolName || "");
    if (tool === "bash" || tool === "eval") ranCommand = true;
    if (WRITE_TOOLS.test(tool)) {
      for (const p of pathsOf(event.input)) {
        const abs = path.resolve(ctx?.cwd || process.cwd(), p.replace(/^~(?=\/)/, process.env.HOME || ""));
        written.set(abs, p);
      }
    }
  });

  pi.on("assistant_message", async (event) => {
    const t = textOf(event?.message);
    if (t.trim()) lastText = t;
  });

  pi.on("session_stop", async () => {
    if (nudged) return;
    const problems: string[] = [];
    const unreported = [...written.keys()].filter((abs) => !lastText.includes(path.basename(abs)));
    if (unreported.length) {
      problems.push(`You changed these files but your reply does not name them: ${unreported.map((a) => written.get(a)).join(", ")}. List every changed file.`);
    }
    const missing = [...written.keys()].filter((abs) => !fs.existsSync(abs));
    if (missing.length) {
      problems.push(`These files you wrote no longer exist: ${missing.map((a) => written.get(a)).join(", ")}. Say so plainly.`);
    }
    if (VERDICT.test(lastText) && !ranCommand && written.size > 0) {
      problems.push("Your reply claims success, but you ran no command or test this turn. Run the check now, or remove the claim.");
    }
    if (!problems.length) return;
    nudged = true;
    return { continue: true, additionalContext: `honesty-gate: ${problems.join(" ")} Then give the corrected final reply.` };
  });
}
