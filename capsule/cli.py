"""CLI adapter. All adapters use api.run_request; production requires Docker."""
import argparse
import json
import os
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from capsule.api import DEMO_INPUTS, DEMO_TASK, run_request
from capsule.runtime import LIMIT, doctor, flat_name


def read_request(stream):
    content = stream.read(LIMIT + 1)
    if len(content) > LIMIT:
        raise ValueError("request JSON exceeds 256KiB")
    return json.loads(content)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["doctor", "demo", "run", "mcp"])
    parser.add_argument("--task")
    parser.add_argument("--input", action="append", default=[], metavar="NAME=FILE")
    parser.add_argument("--request", help="UTF-8 JSON request file")
    parser.add_argument("--request-stdin", action="store_true")
    parser.add_argument("--model", default=os.environ.get("CAPSULE_MODEL"))
    parser.add_argument("--output", default=os.environ.get("CAPSULE_OUTPUT", str(Path.cwd() / ".capsule-output")))
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--test-process", action="store_true", help="DEMO ONLY: unprotected test subprocesses")
    args = parser.parse_args(argv)
    if args.test_process and args.command != "demo":
        parser.error("--test-process is permitted only for the deterministic demo")
    if args.command == "mcp":
        from capsule.mcp import serve
        serve(model=args.model, output=args.output)
        return 0
    if args.command == "doctor":
        report = doctor()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    if args.request and args.request_stdin:
        parser.error("choose --request or --request-stdin")
    if args.request or args.request_stdin:
        if args.task or args.input or args.timeout is not None or args.command != "run":
            parser.error("JSON requests are exclusive with --task/--input/--timeout and require run")
        if args.request:
            with Path(args.request).open("rb") as stream:
                request = read_request(stream)
        else:
            request = read_request(sys.stdin.buffer)
    else:
        inputs = dict(DEMO_INPUTS) if args.command == "demo" else {}
        for spec in args.input:
            name, separator, path = spec.partition("=")
            if not separator:
                parser.error("input syntax: NAME=FILE")
            flat_name(name)
            if name in inputs:
                parser.error("duplicate input name")
            with Path(path).open("rb") as stream:
                content = stream.read(32769)
            if len(content) > 32768:
                parser.error("each input must be <=32KiB")
            inputs[name] = content.decode("utf-8")
        request = {"task": args.task or (DEMO_TASK if args.command == "demo" else None),
                   "inputs": inputs, "timeout": args.timeout if args.timeout is not None else 300}
    report = run_request(request, model=args.model, output=args.output,
                         demo=args.command == "demo", test_process=args.test_process)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def entrypoint():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
