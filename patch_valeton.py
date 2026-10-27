#!/usr/bin/env python3
import argparse
import hashlib
from pathlib import Path
import shlex
import stat
import sys


SUPPORTED_SHA256 = "cb4f7e546a69918c77eca62a524413f76e5ef9d373905be954d7b4f548bdcaa5"
PATCH_OFFSET = 0x280C
ORIGINAL_CALL = bytes.fromhex("ff 15 06 40 00 00")
PATCHED_NAME = "Valeton Suite Wine.exe"
LAUNCHER_NAME = "run-valeton.sh"


class PatchError(Exception):
    pass


def patch_bytes(original: bytes) -> bytes:
    digest = hashlib.sha256(original).hexdigest()
    if digest != SUPPORTED_SHA256:
        raise PatchError(
            "Unsupported executable. Only the tested Valeton Suite 2.1.0 "
            f"Windows x64 build is supported.\nSHA-256: {digest}"
        )

    end_offset = PATCH_OFFSET + len(ORIGINAL_CALL)
    if original[PATCH_OFFSET:end_offset] != ORIGINAL_CALL:
        raise PatchError("The expected speech startup call was not found.")

    return original[:PATCH_OFFSET] + b"\x90" * len(ORIGINAL_CALL) + original[end_offset:]


def launcher_contents(executable: Path) -> bytes:
    lines = [
        "#!/bin/sh",
        "set -eu",
        "",
        'cd -- "$(dirname -- "$0")"',
    ]

    for parent in executable.parents:
        if parent.name == "drive_c":
            lines.append(f"export WINEPREFIX={shlex.quote(str(parent.parent))}")
            break

    lines.append(f'WINEDLLOVERRIDES="d3d11=" exec wine "{PATCHED_NAME}" "$@"')
    return ("\n".join(lines) + "\n").encode()


def patch_installation(executable: Path, check_only: bool = False) -> tuple[Path, Path]:
    executable = executable.expanduser().resolve(strict=True)
    patched = patch_bytes(executable.read_bytes())
    patched_path = executable.parent / PATCHED_NAME
    launcher_path = executable.parent / LAUNCHER_NAME
    outputs = {
        patched_path: patched,
        launcher_path: launcher_contents(executable),
    }

    if check_only:
        return patched_path, launcher_path

    for path, contents in outputs.items():
        if path.is_symlink():
            raise PatchError(f"Refusing to replace a symbolic link: {path}")
        if path.exists() and path.read_bytes() != contents:
            raise PatchError(f"Refusing to overwrite an existing file with different contents: {path}")

    for path, contents in outputs.items():
        if not path.exists():
            with path.open("xb") as output:
                output.write(contents)

    launcher_path.chmod(launcher_path.stat().st_mode | stat.S_IXUSR)
    return patched_path, launcher_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a Wine-compatible copy of the tested Valeton Suite 2.1.0 executable."
    )
    parser.add_argument("executable", type=Path, help="path to the original Valeton Suite.exe")
    parser.add_argument("--check", action="store_true", help="check support without writing files")
    args = parser.parse_args()

    try:
        patched_path, launcher_path = patch_installation(
            executable=args.executable,
            check_only=args.check,
        )
    except (OSError, PatchError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if args.check:
        print("Supported executable: Valeton Suite 2.1.0 (Windows x64).")
        return 0

    print(f"Patched executable: {patched_path}")
    print(f"Launcher: {launcher_path}")
    print("The original executable is unchanged. Spoken prompts are disabled in the copy.")
    print(f"Run: {shlex.quote(str(launcher_path))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
