import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from capsule.api import validate_request, run_request, DEMO_TASK, DEMO_INPUTS
from capsule.mcp import Server
from capsule.runtime import Broker


class RequestTests(unittest.TestCase):
    def test_rejects_history_environment_path_and_backend(self):
        for key in ("history", "messages", "environment", "workspace", "backend", "model", "output"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_request({"task": "x", key: "untrusted"})

    def test_request_rejects_noninteger_timeout_and_oversized_text(self):
        for timeout in (True, 0, 1801, "300", 1.5):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                validate_request({"task": "x", "timeout": timeout})
        with self.assertRaises(ValueError):
            validate_request({"task": "x", "inputs": {"a.txt": "中" * 12000}})

    def test_all_adapters_share_docker_required_api(self):
        with self.assertRaises(ValueError):
            run_request({"task": "x"}, model="m", test_process=True)
        with patch("capsule.api.doctor", return_value={"ok": False, "reason": "no Docker"}):
            with self.assertRaisesRegex(RuntimeError, "no Docker"):
                run_request({"task": "x"}, model="m")

    def test_new_api_runs_end_to_end_without_source_tree_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_request({"task": DEMO_TASK, "inputs": DEMO_INPUTS},
                                 demo=True, test_process=True, output=tmp)
            self.assertEqual(report["delegations"], 2)
            self.assertIn("PASS", report["summary"])

    def test_cancellation_cleans_active_agent(self):
        class Slow:
            def complete(self, *args):
                time.sleep(3)
                return '{}'
        cancel = threading.Event()
        timer = threading.Timer(0.25, cancel.set)
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(Slow(), tmp, backend="test-process", cancel=cancel)
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaises(InterruptedError):
                    broker.run("x", {})
            finally:
                timer.cancel()
            self.assertLess(time.monotonic() - started, 2)


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.responses = queue.Queue()
        self.server = Server(self.responses.put)

    def tearDown(self):
        self.server.close()

    def call(self, method, params=None, rid=1):
        self.server.handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return self.responses.get(timeout=3)

    def initialize(self):
        result = self.call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                          "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(result["result"]["protocolVersion"], "2025-11-25")
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def test_initialization_and_discovery(self):
        self.assertIn("error", self.call("tools/list"))
        self.initialize()
        tools = self.call("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["capsule_run", "capsule_demo", "capsule_doctor"])
        self.assertNotIn("workspace", tools[0]["inputSchema"]["properties"])

    def test_protocol_errors_and_tool_errors_differ(self):
        self.initialize()
        result = self.call("tools/call", {"name": ["invalid"]})
        self.assertEqual(result["error"]["code"], -32602)
        result = self.call("tools/call", {"name": "not_real"})
        self.assertEqual(result["error"]["code"], -32602)
        result = self.call("tools/call", {"name": "capsule_run", "arguments": {"task": "x"}})
        self.assertTrue(result["result"]["isError"])

    def test_cancel_keeps_server_responsive(self):
        self.initialize()
        started = threading.Event()

        def fake_run(*args, **kwargs):
            started.set()
            if not kwargs["cancel"].wait(3):
                raise RuntimeError("not cancelled")
            raise InterruptedError("cancelled")
        with patch("capsule.mcp.run_request", side_effect=fake_run):
            self.server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                "params": {"name": "capsule_run", "arguments": {"task": "x"}}})
            self.assertTrue(started.wait(1))
            self.assertEqual(self.call("ping", rid=8)["result"], {})
            self.server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}})
            self.assertTrue(self.responses.get(timeout=3)["result"]["isError"])

    def test_real_stdio_transport_utf8_and_notifications(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "中文", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        wire = "\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\n"
        result = subprocess.run([sys.executable, "-m", "capsule", "mcp"], cwd=ROOT,
                                input=wire.encode(), capture_output=True, timeout=10, check=True)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[1]["id"], 2)
        self.assertEqual(len(replies[1]["result"]["tools"]), 3)


if __name__ == "__main__":
    unittest.main()
