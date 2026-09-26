"""Build a clean source ZIP and checksum; never include runs, caches or classmates' code."""
import hashlib
import json
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
destination = root.parent / "deliverables"
destination.mkdir(exist_ok=True)
version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]
archive = destination / f"pi-capsule-agents-{version}-source.zip"
allowed_dirs = {"capsule", "extensions", "examples", "docs", "tests", "scripts", ".github"}
allowed_files = {"package.json", "pyproject.toml", "Dockerfile", ".dockerignore", ".gitignore", "README.md", "LICENSE"}
included = []
with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if relative.parts[0] not in allowed_dirs and str(relative) not in allowed_files:
            continue
        name = root.name + "/" + relative.as_posix()
        info = zipfile.ZipInfo(name, date_time=(2026, 9, 26, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        output.writestr(info, path.read_bytes())
        included.append(name)
checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
(destination / (archive.name + ".sha256")).write_text(checksum + "  " + archive.name + "\n", encoding="ascii")
with zipfile.ZipFile(archive) as check:
    assert check.testzip() is None
print(f"{archive}\n{len(included)} files\nSHA256 {checksum}")
