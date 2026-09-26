"""One host-independent request contract shared by every adapter."""
from pathlib import Path

from .runtime import Broker, DemoModel, Ollama, doctor, flat_name, text

DEMO_TASK = "Analyze conductivity samples and independently verify the result."
DEMO_INPUTS = {"samples.csv": "sample,conductivity\nA,10\nB,20\nC,30\n"}


def validate_request(request):
    if not isinstance(request, dict) or set(request) - {"task", "inputs", "timeout"}:
        raise ValueError("request accepts only task, inputs and timeout")
    task = text(request.get("task"), 8192)
    inputs = request.get("inputs", {})
    timeout = request.get("timeout", 300)
    if type(timeout) is not int or not 1 <= timeout <= 1800:
        raise ValueError("timeout must be an integer in 1..1800")
    if not isinstance(inputs, dict) or len(inputs) > 8:
        raise ValueError("inputs must map at most eight flat names to text")
    for name, content in inputs.items():
        flat_name(name)
        text(content)
    if sum(len(content.encode("utf-8")) for content in inputs.values()) > 65536:
        raise ValueError("combined input limit is 64KiB")
    return {"task": task, "inputs": dict(inputs), "timeout": timeout}


def run_request(request, *, model=None, output=None, demo=False, test_process=False, cancel=None):
    request = validate_request(request)
    if test_process and not demo:
        raise ValueError("test-process is permitted only for the deterministic demo")
    if not demo and not model:
        raise ValueError("configure CAPSULE_MODEL or supply --model")
    if not test_process:
        health = doctor()
        if not health["ok"]:
            raise RuntimeError(health["reason"])
    broker = Broker(DemoModel() if demo else Ollama(model), output or Path.cwd() / ".capsule-output",
                    backend="test-process" if test_process else "docker", timeout=request["timeout"],
                    cancel=cancel)
    return broker.run(request["task"], request["inputs"]) | {"output_directory": str(broker.output)}
