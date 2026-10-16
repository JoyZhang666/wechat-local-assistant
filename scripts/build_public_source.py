"""Build a source-only candidate from an explicit, privacy-conscious allowlist."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from wechat_local import __version__


REQUIRED = (
    "LICENSE", "LICENSE-SCOPE.md", ".gitignore",
    "README.md", "config.example.json", "requirements-gui.txt", "requirements-asr.txt", "requirements-voice.txt",
    "scripts/wechat_ui.py", "scripts/create_gui_launcher.py", "scripts/build_public_source.py",
    "scripts/cloud_api_worker.py",
    "requirements-membership.txt",
    "skills/wechat-local-summary/SKILL.md", "docs/distribution.md",
    "third_party/wechat-decrypt/LICENSE", "third_party/wechat-decrypt/NOTICE.md",
    ".github/workflows/tests.yml", "tests/test_builtin_provider.py", "tests/test_release.py",
    "docs/release-notes.md",
)
OPTIONAL = (
    "docs/public-install.md", "tests/test_history.py", "tests/test_selected_summary.py",
    "tests/test_gui_workflow.py", "tests/test_model_runtime.py", "tests/test_export.py",
    "tests/test_media.py", "tests/test_voice.py", "tests/test_image.py", "tests/test_asr.py",
    "tests/test_onboarding.py",
    "tests/test_bundle.py", "tests/test_cloud.py", "tests/test_membership.py", "tests/test_summary_reading.py", "tests/test_summary_structure.py", "tests/test_media_summary.py",
)
SOURCE_VERSION = "v" + __version__
STATUS = f"""SOURCE PRERELEASE — EXPERIMENTAL WINDOWS COMPATIBILITY

Version: {SOURCE_VERSION}

This source package was built from an explicit file allowlist.
It excludes private configuration, keys, chat data, reports, models, runtime,
virtual environments, vendor binaries and Git history.

Independent newly written code in this candidate is covered by MIT, subject
to LICENSE-SCOPE.md. This does not license the existing local installation,
original vendor package, model weights, or third-party WeChat components.
Final source and generated assets still require release review.

An MIT built-in reader is included with preserved third-party license notices.
Each user must complete their own authorized local database-key onboarding.
External native provider binaries are not bundled.
No access consent, account material or authorization transfers with this pack.
The synthetic demo does not prove compatibility with a real WeChat install.

This build is not a complete security, privacy or license audit. Hashes prove
file integrity only. Public publishing and installing this Skill are separate
user-authorized actions; this script performs neither.
"""
BLOCKED_TEXT = (
    ("private user directory", re.compile(r"\b[A-Z]:[\\/]+(?:Users|Documents and Settings)[\\/]+[^\\/\s\"']+", re.I)),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("literal API credential", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
)
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024


def blocked_patterns(root: Path):
    patterns = list(BLOCKED_TEXT)
    for path in (root.resolve(), Path.home().resolve()):
        for value in {str(path), str(path).replace("\\", "/"), str(path).replace("\\", "\\\\")}:
            patterns.append(("current machine path", re.compile(re.escape(value), re.I)))
    return patterns


def plain_path(path: Path, root: Path) -> None:
    """Reject symlinks and Windows junctions anywhere in a selected path."""
    relative = path.relative_to(root)
    for candidate in (root, *(root.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts) + 1))):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("selected path is a link or reparse point")


def selected_paths(root: Path) -> list[Path]:
    paths = []
    for relative in REQUIRED + OPTIONAL:
        path = root / relative
        if relative == "README.md" and (root / "README.public.md").is_file():
            path = root / "README.public.md"
        if path.exists():
            plain_path(path, root)
            paths.append(path)
        elif relative in REQUIRED:
            raise ValueError(f"required public file missing: {relative}")
    modules = root / "src/wechat_local"
    plain_path(modules, root)
    paths.extend(path for path in modules.iterdir() if path.name.endswith(".py"))
    skill = root / "skills/wechat-local-summary"
    queue = [skill]
    while queue:
        for path in queue.pop().iterdir():
            plain_path(path, root)
            if path.is_dir():
                queue.append(path)
            elif path.suffix in {".md", ".yaml", ".yml", ".py", ".json", ".txt"}:
                paths.append(path)
            else:
                raise ValueError("unexpected non-text Skill resource")
    return sorted(set(paths), key=lambda path: path.relative_to(root).as_posix())


def payloads(root: Path) -> dict[str, bytes]:
    content = {}
    patterns = blocked_patterns(root)
    for path in selected_paths(root):
        plain_path(path, root)
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("selected source is not a small regular file")
        data = path.read_bytes()
        text = data.decode("utf-8-sig")
        for reason, pattern in patterns:
            if pattern.search(text):
                raise ValueError(f"candidate blocked: {reason}; inspect locally")
        name = path.relative_to(root).as_posix()
        content["README.md" if name == "README.public.md" else name] = data
    if sum(map(len, content.values())) > MAX_TOTAL_BYTES:
        raise ValueError("source candidate exceeds size limit")
    content["RELEASE-STATUS.txt"] = STATUS.encode("utf-8")
    checksums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(content.items()))
    content["SHA256SUMS.txt"] = checksums.encode("utf-8")
    return content


def verify_archive(path: Path, content: dict[str, bytes]) -> None:
    """Read the written members and verify their bytes and checksum manifest."""
    with zipfile.ZipFile(path, "r") as archive:
        names = archive.namelist()
        if len(names) != len(content) or set(names) != set(content):
            raise ValueError("archive members differ from the selected source")
        checksums = []
        for name, expected in sorted(content.items()):
            actual = archive.read(name)
            digest = hashlib.sha256(actual).hexdigest()
            if digest != hashlib.sha256(expected).hexdigest():
                raise ValueError("archive member checksum mismatch")
            if name != "SHA256SUMS.txt":
                checksums.append(f"{digest}  {name}\n")
        if "".join(checksums).encode("utf-8") != content["SHA256SUMS.txt"]:
            raise ValueError("archive checksum manifest mismatch")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--output", type=Path, help="new ZIP path inside PROJECT/outputs/public")
    parser.add_argument("--check", action="store_true", help="validate candidate without writing an archive")
    args = parser.parse_args()
    root = args.project_root.absolute()
    try:
        content = payloads(root)
        if args.check:
            print(json.dumps({"status": "source-candidate-checked", "version": SOURCE_VERSION, "files": len(content), "audit": "not-performed", "license": "MIT-with-scoped-coverage"}))
            return 0
        output = args.output or Path("outputs/public/wechat-local-summary-source.zip")
        output = (output if output.is_absolute() else root / output).absolute()
        public = root / "outputs/public"
        output.relative_to(public)
        output.resolve().relative_to(public.resolve())
        if output.suffix.lower() != ".zip":
            raise ValueError("output must be a ZIP in outputs/public")
        for ancestor in (output.parent, *output.parent.parents):
            ancestor.relative_to(root)
            if ancestor.exists():
                plain_path(ancestor, root)
            if ancestor == root:
                break
        output.parent.mkdir(parents=True, exist_ok=True)
        plain_path(output.parent, root)
        if output.exists():
            raise ValueError("output exists; choose a new archive name")
        with output.open("xb") as stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(content.items()):
                entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, data)
        verify_archive(output, content)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        print(json.dumps({"status": "source-candidate-built", "version": SOURCE_VERSION, "files": len(content), "sha256": digest, "license": "MIT-with-scoped-coverage", "published": False}))
        return 0
    except (ValueError, OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
