"""Trusted host broker. Agents never receive Docker control or model credentials."""
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

LIMIT = 262144
IMAGE = "pi-capsule-agents:1.0.0"
ROOT = Path(__file__).resolve().parent.parent
ROLES = {"analyst", "author", "reviewer"}


def text(value, maximum=32768):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise ValueError("missing text or size limit exceeded")
    return value


def flat_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise ValueError("invalid flat filename")
    # Portable output: reject Windows special filenames too.
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}:
        raise ValueError("reserved filename")
    if value.endswith("."):
        raise ValueError("trailing dot is not allowed")
    return value


def digest(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def bounded_call(operation, timeout, cancel=None):
    """Bound host waits too, including a stalled provider or Docker stdin.

    A timed-out daemon thread cannot be forcibly killed by Python. Provider
    sockets have their own timeout; child pipes are closed during cleanup.
    """
    answers = queue.Queue(maxsize=1)

    def invoke():
        try:
            answers.put((True, operation()))
        except BaseException as error:
            answers.put((False, error))

    threading.Thread(target=invoke, daemon=True).start()
    deadline = time.monotonic() + timeout
    while True:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("run cancelled")
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("host operation exceeded total run deadline")
        try:
            success, value = answers.get(timeout=min(left, 0.1))
            break
        except queue.Empty:
            continue
    if not success:
        raise value
    return value


def docker_command(name):
    return ["docker", "run", "--rm", "--pull=never", "-i", "--name", name,
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true", "--user=65534:65534",
            "--pids-limit=32", "--memory=128m", "--memory-swap=128m", "--cpus=0.5",
            "--ipc=none", "--ulimit=nofile=64:64", "--log-driver=none",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777", IMAGE]


class Ollama:
    """Fixed host-only endpoint; untrusted model messages cannot override URL/model."""
    def __init__(self, model, url="http://127.0.0.1:11434/api/chat"):
        self.model = text(model, 200)
        self.url = url

    def complete(self, role, messages, timeout):
        data = json.dumps({"model": self.model, "messages": messages,
                           "stream": False, "format": "json",
                           "options": {"temperature": 0, "num_predict": 2048}}).encode()
        request = urllib.request.Request(self.url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=max(0.1, min(timeout, 90))) as response:
            raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("model response too large")
        return text(json.loads(raw)["message"]["content"])


class DemoModel:
    """Deterministic test double, explicitly NOT a real LLM or an accuracy benchmark."""
    def complete(self, role, messages, timeout):
        initial = json.loads(messages[1]["content"])
        results = [json.loads(m["content"])["tool_result"] for m in messages[2:]
                   if m["role"] == "user" and '"tool_result"' in m["content"]]
        if role == "main":
            if not results:
                action = {"action": "delegate", "role": "analyst",
                          "task": "Read samples.csv, compute conductivity statistics and produce analysis.json.",
                          "inputs": ["samples.csv"]}
            elif len(results) == 1:
                artifact = results[0]["artifacts"][0]["ref"]
                action = {"action": "delegate", "role": "reviewer",
                          "task": "Independently recompute the conductivity mean and check analysis.json.",
                          "inputs": ["samples.csv", artifact]}
            else:
                action = {"action": "final", "summary": "Completed independent analysis and review. " + results[-1]["summary"]}
        elif role == "analyst":
            actions = [
                {"action": "read", "name": "samples.csv"},
                {"action": "csv_stats", "name": "samples.csv", "column": "conductivity"},
            ]
            if len(results) < 2:
                action = actions[len(results)]
            elif len(results) == 2:
                action = {"action": "write", "name": "analysis.json", "content": json.dumps(results[1])}
            else:
                action = {"action": "final", "summary": "Statistics computed from samples.csv; see analysis.json."}
        elif role == "reviewer":
            if not results:
                action = {"action": "read", "name": "analysis.json"}
            elif len(results) == 1:
                action = {"action": "csv_stats", "name": "samples.csv", "column": "conductivity"}
            else:
                same = json.loads(results[0]) == results[1]
                action = {"action": "final", "summary": f"Independent review {'PASS' if same else 'FAIL'}: {json.dumps(results[1])}"}
        else:
            action = {"action": "final", "summary": "Demo author has no work."}
        return json.dumps(action)


class Broker:
    def __init__(self, model, output, *, backend="docker", timeout=300, cancel=None):
        if backend not in {"docker", "test-process"}:
            raise ValueError("unknown backend")
        if not 1 <= timeout <= 1800:
            raise ValueError("timeout must be 1..1800 seconds")
        self.model = model
        self.backend = backend
        self.timeout = timeout
        self.cancel = cancel
        self.run_id = uuid.uuid4().hex
        self.output = Path(output).resolve() / self.run_id
        self.output.mkdir(parents=True, exist_ok=False)
        self.catalog = {}
        self.calls = 0
        self.delegations = 0
        self.deadline = 0
        self.audit = []

    def event(self, kind, **fields):
        record = {"time": time.time(), "event": kind, **fields}
        self.audit.append(record)
        with (self.output / "audit.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def remaining(self):
        if self.cancel is not None and self.cancel.is_set():
            raise InterruptedError("run cancelled")
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("total run deadline exceeded")
        return left

    def run(self, task, inputs):
        self.deadline = time.monotonic() + self.timeout
        try:
            text(task, 8192)
            if not isinstance(inputs, dict) or len(inputs) > 8:
                raise ValueError("at most eight explicit input files")
            for name, content in inputs.items():
                flat_name(name)
                text(content)
            if sum(len(c.encode()) for c in inputs.values()) > 65536:
                raise ValueError("total input limit is 64KiB")
            self.catalog = {name: {"name": name, "content": content, "sha256": digest(content)}
                            for name, content in inputs.items()}
            self.event("run_started", backend=self.backend,
                       inputs={n: v["sha256"] for n, v in self.catalog.items()})
            # Main receives names only. It cannot read input bytes itself.
            result = self.agent("main", task, {name: "" for name in inputs})
            report = {"status": "completed", "run_id": self.run_id, "backend": self.backend,
                      "model": "DETERMINISTIC_TEST_DOUBLE" if isinstance(self.model, DemoModel) else self.model.model,
                      "summary": result["summary"], "model_calls": self.calls,
                      "delegations": self.delegations,
                      "artifacts": [{k: v for k, v in item.items() if k != "content"} | {"ref": ref}
                                    for ref, item in self.catalog.items() if ref.startswith("artifact:")]}
            (self.output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            self.event("run_completed")
            return report
        except BaseException as error:
            self.event("run_failed", error=type(error).__name__)
            (self.output / "result.json").write_text(json.dumps({"status": "failed", "error": str(error),
                                                               "run_id": self.run_id}), encoding="utf-8")
            raise

    def delegate(self, request):
        if not isinstance(request, dict) or set(request) != {"action", "role", "task", "inputs"}:
            raise ValueError("invalid delegation schema")
        if request["action"] != "delegate" or not isinstance(request["role"], str) or request["role"] not in ROLES:
            raise ValueError("invalid delegation role/action")
        task = text(request["task"], 8192)
        refs = request["inputs"]
        if not isinstance(refs, list) or len(refs) > 8 or any(not isinstance(r, str) for r in refs):
            raise ValueError("invalid grants")
        if len(set(refs)) != len(refs):
            raise ValueError("duplicate grants")
        granted = {}
        for ref in refs:
            if ref not in self.catalog:
                raise PermissionError("reference not granted by user or produced in this run")
            item = self.catalog[ref]
            if item["name"] in granted:
                raise ValueError("ambiguous input basename")
            granted[item["name"]] = item["content"]
        if sum(len(v.encode()) for v in granted.values()) > 65536:
            raise ValueError("delegated input budget exceeded")
        if self.delegations >= 6:
            raise ValueError("delegation budget exhausted")
        self.delegations += 1
        self.event("delegated", role=request["role"], grants=refs)
        return self.agent(request["role"], task, granted)

    def accept(self, agent_id, role, result):
        if set(result) != {"type", "summary", "artifacts"} or result["type"] != "result":
            raise ValueError("invalid result schema")
        summary = text(result["summary"])
        artifacts = result["artifacts"]
        if not isinstance(artifacts, dict) or len(artifacts) > 4:
            raise ValueError("invalid artifacts")
        if artifacts and role not in {"analyst", "author"}:
            raise PermissionError("role cannot publish artifacts")
        # Validate ALL entries before writing any files.
        for name, content in artifacts.items():
            flat_name(name)
            if not isinstance(content, str) or len(content.encode()) > 32768:
                raise ValueError("invalid artifact size/type")
        published = []
        target = self.output / "artifacts" / agent_id
        if artifacts:
            target.mkdir(parents=True, exist_ok=False)
        for name, content in artifacts.items():
            path = target / name
            path.write_bytes(content.encode("utf-8"))
            ref = f"artifact:{agent_id}:{name}"
            info = {"name": name, "content": content, "sha256": digest(content),
                    "bytes": len(content.encode()), "path": str(path)}
            self.catalog[ref] = info
            published.append({"ref": ref, "name": name, "sha256": info["sha256"], "bytes": info["bytes"]})
        self.event("agent_completed", agent_id=agent_id, role=role,
                   summary_sha256=digest(summary), artifacts=published)
        return {"summary": summary, "artifacts": published}

    def agent(self, role, task, inputs):
        agent_id = uuid.uuid4().hex
        name = f"pi-capsule-{agent_id}"
        self.event("agent_started", agent_id=agent_id, role=role,
                   inputs={n: digest(c) for n, c in inputs.items()})
        with tempfile.TemporaryDirectory(prefix="capsule-test-") as cwd:
            command = docker_command(name)
            env = None
            if self.backend == "test-process":
                # TEST ONLY: separate process is not an OS sandbox.
                command = [sys.executable, "-I", "-u", str(ROOT / "capsule" / "worker.py")]
                env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "PATH") if k in os.environ}
                env.update({"HOME": cwd, "USERPROFILE": cwd, "TMP": cwd, "TEMP": cwd})
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=cwd, env=env)
            messages = queue.Queue(maxsize=32)
            errors = bytearray()
            stop = threading.Event()

            def emit(value):
                while not stop.is_set():
                    try:
                        messages.put(value, timeout=0.1)
                        return
                    except queue.Full:
                        pass

            def read_stdout():
                try:
                    while not stop.is_set():
                        line = proc.stdout.readline(LIMIT + 2)
                        if not line:
                            break
                        if len(line) > LIMIT or not line.endswith(b"\n"):
                            raise ValueError("invalid protocol frame")
                        emit(json.loads(line))
                except Exception as error:
                    emit(error)
                finally:
                    emit(EOFError("capsule exited before a valid result"))

            def read_stderr():
                while chunk := proc.stderr.read(4096):
                    if len(errors) < 8192:
                        errors.extend(chunk[:8192-len(errors)])

            threads = [threading.Thread(target=read_stdout, daemon=True),
                       threading.Thread(target=read_stderr, daemon=True)]
            for thread in threads:
                thread.start()

            def reply(value):
                wire = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()
                if len(wire) > LIMIT:
                    raise ValueError("outgoing protocol frame too large")
                def write():
                    proc.stdin.write(wire)
                    proc.stdin.flush()
                bounded_call(write, self.remaining(), self.cancel)

            try:
                reply({"role": role, "task": task, "inputs": inputs})
                while True:
                    try:
                        message = messages.get(timeout=min(self.remaining(), 0.2))
                    except queue.Empty:
                        continue
                    if isinstance(message, Exception):
                        raise RuntimeError(f"{message}; {errors.decode('utf-8', 'replace')}")
                    if not isinstance(message, dict):
                        raise ValueError("protocol object required")
                    kind = message.get("type")
                    if kind == "model":
                        history = message.get("messages")
                        if not isinstance(history, list) or not 2 <= len(history) <= 40:
                            raise ValueError("invalid model history")
                        if any(not isinstance(m, dict) or set(m) != {"role", "content"}
                               or m["role"] not in {"system", "user", "assistant"}
                               or not isinstance(m["content"], str) for m in history):
                            raise ValueError("invalid model message")
                        if self.calls >= 64:
                            raise ValueError("model call budget exhausted")
                        self.calls += 1
                        self.event("model_call", agent_id=agent_id, role=role, number=self.calls)
                        remaining = self.remaining()
                        content = bounded_call(lambda: self.model.complete(role, history, remaining), remaining, self.cancel)
                        self.remaining()
                        reply({"content": content})
                    elif kind == "delegate" and role == "main":
                        try:
                            value = self.delegate(message.get("request"))
                        except (ValueError, PermissionError) as error:
                            self.event("delegation_denied", agent_id=agent_id, reason=str(error))
                            value = {"denied": str(error)}
                        reply(value)
                    elif kind == "result":
                        proc.stdin.close()
                        code = proc.wait(timeout=min(self.remaining(), 5))
                        if code != 0:
                            raise RuntimeError("capsule failed after producing result")
                        return self.accept(agent_id, role, message)
                    elif kind == "error":
                        raise RuntimeError(str(message.get("error", "capsule error")))
                    else:
                        raise PermissionError("unexpected protocol operation for role")
            finally:
                stop.set()
                if self.backend == "docker":
                    # Removing the named container terminates the capsule, not just the CLI.
                    try:
                        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=10, check=False)
                    except (OSError, subprocess.TimeoutExpired):
                        self.event("cleanup_warning", container=name)
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if not stream.closed:
                        stream.close()
                for thread in threads:
                    thread.join(timeout=1)


def doctor():
    binary = shutil.which("docker")
    if not binary:
        return {"ok": False, "reason": "Docker CLI not found; install/start Docker Desktop with Linux containers"}
    try:
        result = subprocess.run([binary, "image", "inspect", IMAGE], capture_output=True, timeout=15)
        return {"ok": result.returncode == 0, "image": IMAGE,
                "reason": "ready" if result.returncode == 0 else "Start Docker and build the image first"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "Docker did not respond within 15 seconds"}
