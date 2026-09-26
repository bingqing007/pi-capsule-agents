import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "capsule"))
from runtime import Broker, DemoModel, digest, docker_command, flat_name

spec = importlib.util.spec_from_file_location("capsule_worker", ROOT / "capsule" / "worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class CapturingModel(DemoModel):
    def __init__(self):
        self.histories = []

    def complete(self, role, messages, timeout):
        self.histories.append((role, json.loads(json.dumps(messages))))
        return super().complete(role, messages, timeout)


class CapabilityTests(unittest.TestCase):
    def test_paths_and_windows_reserved_names(self):
        for name in ("../secret", "/etc/passwd", "C:\\secret", "x/y", "x\\y", "CON.txt", "NUL", "a.", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                flat_name(name)

    def test_analyst_cannot_read_ungranted_data(self):
        with self.assertRaises(PermissionError):
            worker.execute("analyst", {"action": "read", "name": "secret.txt"}, {"a.txt": "a"}, {})

    def test_reviewer_cannot_write_or_delegate(self):
        for action in ({"action": "write", "name": "x.txt", "content": "x"}, {"action": "delegate"}):
            with self.assertRaises(PermissionError):
                worker.execute("reviewer", action, {}, {})

    def test_arithmetic_does_not_execute_code(self):
        self.assertEqual(worker.calculate("(10+20+30)/3"), 20)
        for expression in ('__import__("os").system("echo bad")', "2**100", "True", "1e309", "a.x"):
            with self.subTest(expression=expression), self.assertRaises((ValueError, SyntaxError)):
                worker.calculate(expression)

    def test_csv_rejects_nonfinite(self):
        with self.assertRaises(ValueError):
            worker.csv_stats("x\nnan\n", "x")

    def test_grant_cannot_be_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(DemoModel(), tmp, backend="test-process")
            with self.assertRaises(PermissionError):
                broker.delegate({"action": "delegate", "role": "analyst", "task": "read secret",
                                 "inputs": ["other-agent:secret"]})
            self.assertEqual(broker.delegations, 0)

    def test_result_validation_precedes_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(DemoModel(), tmp, backend="test-process")
            with self.assertRaises(ValueError):
                broker.accept("abc", "author", {"type": "result", "summary": "ok",
                                                "artifacts": {"good.txt": "ok", "../bad.txt": "bad"}})
            self.assertFalse((broker.output / "artifacts").exists())

    def test_main_cannot_publish_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(DemoModel(), tmp, backend="test-process")
            with self.assertRaises(PermissionError):
                broker.accept("abc", "main", {"type": "result", "summary": "ok", "artifacts": {"a.txt": "x"}})

    def test_docker_has_no_mount_network_or_host_pid(self):
        args = docker_command("test")
        for required in ("--network=none", "--read-only", "--cap-drop=ALL", "--user=65534:65534", "--pids-limit=32"):
            self.assertIn(required, args)
        for forbidden in ("--privileged", "--pid=host", "--network=host", "--mount", "-v", "--env-file"):
            self.assertNotIn(forbidden, args)


class ProcessIntegrationTests(unittest.TestCase):
    def test_stalled_provider_respects_deadline(self):
        class SlowModel:
            def complete(self, *args):
                time.sleep(2)
                return '{"action":"final","summary":"late"}'
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(SlowModel(), tmp, backend="test-process", timeout=1)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                broker.run("task", {})
            self.assertLess(time.monotonic() - started, 1.8)

    def test_main_analyst_reviewer_and_artifact_delivery(self):
        model = CapturingModel()
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(model, tmp, backend="test-process", timeout=30)
            report = broker.run("MAIN_PRIVATE_CANARY: analyze and verify", {
                "samples.csv": "sample,conductivity\nA,10\nB,20\nC,30\n",
                "private.txt": "UNGRANTED_FILE_CANARY",
            })
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["delegations"], 2)
            self.assertEqual(report["model_calls"], 10)
            self.assertIn("PASS", report["summary"])
            artifact = report["artifacts"][0]
            content = Path(artifact["path"]).read_text(encoding="utf-8")
            self.assertEqual(digest(content), artifact["sha256"])
            self.assertEqual(json.loads(content)["mean"], 20)
            children = [(role, history) for role, history in model.histories if role != "main"]
            self.assertTrue(children)
            for role, history in children:
                raw = json.dumps(history)
                self.assertNotIn("MAIN_PRIVATE_CANARY", raw)
                self.assertNotIn("UNGRANTED_FILE_CANARY", raw)
            # Reviewer gets source + proposed result, not analyst's transcript.
            review_initial = next(h for r, h in children if r == "reviewer")
            self.assertEqual(len(review_initial), 2)
            main_initial = model.histories[0][1]
            self.assertNotIn("A,10", json.dumps(main_initial))
            audit = (broker.output / "audit.jsonl").read_text()
            self.assertNotIn("MAIN_PRIVATE_CANARY", audit)
            self.assertNotIn("UNGRANTED_FILE_CANARY", audit)

    def test_model_failure_is_not_success(self):
        class BrokenModel:
            def complete(self, *args):
                raise RuntimeError("provider unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(BrokenModel(), tmp, backend="test-process", timeout=10)
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                broker.run("task", {})
            report = json.loads((broker.output / "result.json").read_text())
            self.assertEqual(report["status"], "failed")

    def test_invalid_json_exhausts_budget(self):
        class InvalidModel:
            def complete(self, *args):
                return "not JSON"
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(InvalidModel(), tmp, backend="test-process", timeout=10)
            with self.assertRaisesRegex(RuntimeError, "step budget"):
                broker.run("task", {})
            self.assertEqual(broker.calls, 18)

    def test_cli_rejects_unprotected_real_run(self):
        result = subprocess.run([sys.executable, str(ROOT / "capsule" / "cli.py"), "run", "--test-process",
                                 "--task", "test", "--model", "unused"], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"only", result.stderr)


@unittest.skipUnless(os.environ.get("CAPSULE_DOCKER_TESTS") == "1", "opt-in Docker integration tests")
class DockerIntegrationTests(unittest.TestCase):
    def test_container_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(DemoModel(), tmp, timeout=60)
            result = broker.run("analyze", {"samples.csv": "sample,conductivity\nA,10\nB,20\nC,30\n"})
            self.assertIn("PASS", result["summary"])

    def test_kernel_boundaries_and_fresh_tmp(self):
        script = (
            "import os,json,socket; from pathlib import Path; "
            "p=Path('/tmp/capsule-marker'); existed=p.exists(); p.write_text('x'); "
            "s=socket.socket(); s.settimeout(0.5); "
            "network=s.connect_ex(('1.1.1.1',53)); "
            "print(json.dumps({'uid':os.getuid(),'old_tmp':existed,'network':network,"
            "'docker_socket':Path('/var/run/docker.sock').exists(),"
            "'canary':os.environ.get('CAPSULE_HOST_CANARY')}))"
        )
        env = dict(os.environ, CAPSULE_HOST_CANARY="host-only")
        for _ in range(2):
            args = docker_command("capsule-probe-" + uuid.uuid4().hex)
            args[-1:-1] = ["--entrypoint", "python"]
            result = subprocess.run(args + ["-I", "-c", script], env=env, capture_output=True, timeout=30, check=True)
            value = json.loads(result.stdout)
            self.assertEqual(value["uid"], 65534)
            self.assertFalse(value["old_tmp"])
            self.assertNotEqual(value["network"], 0)
            self.assertFalse(value["docker_socket"])
            self.assertIsNone(value["canary"])


if __name__ == "__main__":
    unittest.main()
