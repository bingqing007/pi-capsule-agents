"""MCP stdio tools server. No HTTP port or client-supplied host paths.

Initialization, ping, tools/list, tools/call and cancellation; one active run.
Model and output directory are configured by the host, never by tool arguments.
"""
import json
import sys
import threading

from . import __version__
from .api import DEMO_INPUTS, DEMO_TASK, run_request, validate_request
from .runtime import LIMIT, doctor

VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
REQUEST_SCHEMA = {
    "type": "object", "required": ["task"], "additionalProperties": False,
    "properties": {
        "task": {"type": "string", "minLength": 1, "maxLength": 8192},
        "inputs": {"type": "object", "maxProperties": 8,
                   "description": "Explicit text copies keyed by flat filenames; never host paths.",
                   "additionalProperties": {"type": "string", "maxLength": 32768}},
        "timeout": {"type": "integer", "minimum": 1, "maximum": 1800, "default": 300},
    },
}
TOOLS = [
    {"name": "capsule_run", "description": "Run an isolated main/sub-agent team on explicit task and text copies. Requires Docker and a host-configured Ollama model.",
     "inputSchema": REQUEST_SCHEMA,
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}},
    {"name": "capsule_demo", "description": "Deterministic demo in actual Docker capsules; does not test a real model.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}},
    {"name": "capsule_doctor", "description": "Read-only Docker image readiness check.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
]


class Server:
    def __init__(self, send, model=None, output=None):
        self.send = send
        self.model = model
        self.output = output
        self.initialized = False
        self.ready = False
        self.active = None
        self.lock = threading.Lock()

    def error(self, rid, code, message):
        self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    def result(self, rid, result):
        self.send({"jsonrpc": "2.0", "id": rid, "result": result})

    def handle(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            self.error(None, -32600, "Invalid Request")
            return
        method = message["method"]
        has_id = "id" in message
        rid = message.get("id")
        if has_id and type(rid) not in (int, str):
            self.error(None, -32600, "Invalid request id")
            return
        params = message.get("params", {})
        if not isinstance(params, dict):
            if has_id:
                self.error(rid, -32602, "params must be an object")
            return
        if not has_id:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            elif method == "notifications/cancelled":
                with self.lock:
                    if self.active and self.active[0] == params.get("requestId"):
                        self.active[1].set()
            return
        if method == "ping":
            self.result(rid, {})
        elif method == "initialize":
            if self.initialized:
                self.error(rid, -32600, "Already initialized")
                return
            version = params.get("protocolVersion")
            if not isinstance(version, str) or not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict):
                self.error(rid, -32602, "protocolVersion, capabilities and clientInfo required")
                return
            self.initialized = True
            self.result(rid, {"protocolVersion": version if version in VERSIONS else "2025-11-25",
                              "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": {"name": "pi-capsule-agents", "version": __version__},
                              "instructions": "Only explicit task/input text is forwarded. The main Agent runs in its own capsule. Host history and host paths are not accepted."})
        elif not self.ready:
            self.error(rid, -32002, "Complete initialization first")
        elif method == "tools/list":
            self.result(rid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or name not in {tool["name"] for tool in TOOLS}:
                self.error(rid, -32602, "Unknown tool")
                return
            try:
                if name == "capsule_run":
                    arguments = validate_request(arguments)
                elif arguments != {}:
                    raise ValueError("tool takes no arguments")
            except (ValueError, TypeError) as error:
                self.error(rid, -32602, str(error))
                return
            with self.lock:
                if self.active:
                    self.error(rid, -32000, "One active tool call per server; wait or cancel it")
                    return
                cancel = threading.Event()
                thread = threading.Thread(target=self.execute, args=(rid, name, arguments, cancel), daemon=True)
                self.active = (rid, cancel, thread)
                thread.start()
        else:
            self.error(rid, -32601, "Method not found")

    def execute(self, rid, name, arguments, cancel):
        try:
            if name == "capsule_doctor":
                report = doctor()
            else:
                request = {"task": DEMO_TASK, "inputs": DEMO_INPUTS} if name == "capsule_demo" else arguments
                report = run_request(request, model=self.model, output=self.output,
                                     demo=name == "capsule_demo", cancel=cancel)
            result = {"content": [{"type": "text", "text": json.dumps(report, ensure_ascii=False)}], "isError": False}
        except Exception as error:
            result = {"content": [{"type": "text", "text": str(error)[:4000]}], "isError": True}
        with self.lock:
            self.active = None
        self.result(rid, result)

    def close(self):
        with self.lock:
            active = self.active
        if active:
            active[1].set()
            active[2].join(timeout=30)


def serve(model=None, output=None):
    output_lock = threading.Lock()

    def send(message):
        wire = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        with output_lock:
            sys.stdout.buffer.write(wire)
            sys.stdout.buffer.flush()

    server = Server(send, model, output)
    try:
        while True:
            line = sys.stdin.buffer.readline(LIMIT + 2)
            if not line:
                break
            if len(line) > LIMIT or not line.endswith(b"\n"):
                server.error(None, -32700, "Frame exceeds limit or lacks newline")
                break
            try:
                request = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                server.error(None, -32700, "Parse error")
                continue
            server.handle(request)
    finally:
        server.close()
