import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// This adapter is a UI command, not the main Agent. The main Agent lives inside
// its own capsule and has only delegate. Pi's current history is never forwarded.
export default function capsulePlugin(pi: ExtensionAPI) {
  function register(name: string, command: "doctor" | "demo" | "run") {
    pi.registerCommand(name, {
      description: command === "run"
        ? "Run an isolated main/child team. Argument is a self-contained task; set CAPSULE_MODEL first."
        : `Capsule ${command} (Docker required for execution).`,
      handler: async (args, ctx) => {
        const model = process.env.CAPSULE_MODEL;
        if (command === "run" && (!args.trim() || !model)) {
          ctx.ui.notify("Set CAPSULE_MODEL to an installed Ollama model and supply a task.", "error");
          return;
        }
        let request: unknown;
        if (command === "run") {
          try {
            request = args.trim().startsWith("{") ? JSON.parse(args) : { task: args.trim() };
          } catch {
            ctx.ui.notify("Invalid JSON request. Use a task sentence or {task, inputs, timeout}.", "error");
            return;
          }
        }
        const argv = [path.join(root, "capsule", "cli.py"), command,
          "--output", path.join(ctx.cwd, ".capsule-output")];
        if (command === "run") argv.push("--request-stdin", "--model", model!);
        // User-selected files are passed through the CLI, not silently discovered.
        const proc = spawn(process.env.CAPSULE_PYTHON || "python", argv,
          { cwd: root, shell: false, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
        proc.stdin.on("error", () => {}); // Spawn/close handlers report process failures.
        proc.stdin.end(command === "run" ? JSON.stringify(request) : undefined);
        let output = "";
        let error = "";
        proc.stdout.on("data", chunk => { output = (output + chunk.toString()).slice(-100000); });
        proc.stderr.on("data", chunk => { error = (error + chunk.toString()).slice(-8000); });
        await new Promise<void>(resolve => {
          proc.on("error", err => { ctx.ui.notify(err.message, "error"); resolve(); });
          proc.on("close", code => {
            ctx.ui.notify(code === 0 ? output : error || output || `Exit ${code}`,
              code === 0 ? "info" : "error");
            resolve();
          });
        });
      },
    });
  }
  register("capsule-doctor", "doctor");
  register("capsule-demo", "demo");
  register("capsule-run", "run");
}
