import test from "node:test";
import assert from "node:assert/strict";
import plugin from "../extensions/index.ts";

test("Pi extension registers commands and checks required model", async () => {
  const commands = new Map();
  plugin({ registerCommand: (name, command) => commands.set(name, command) });
  assert.deepEqual([...commands.keys()], ["capsule-doctor", "capsule-demo", "capsule-run"]);
  const messages = [];
  // Empty task always fails preflight, regardless of environment model setting.
  await commands.get("capsule-run").handler("", {ui: {notify: (...args) => messages.push(args)}});
  assert.equal(messages[0][1], "error");
});
