import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import patch_valeton


class PatcherTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.directory = Path(temporary_directory.name)
        self.prefix = self.directory / "Wine's prefix $value `false`"
        self.installation = self.prefix / "drive_c" / "Program Files" / "Valeton Suite"
        self.installation.mkdir(parents=True)
        self.executable = self.installation / "Valeton Suite.exe"
        self.original = (
            b"\xa5" * patch_valeton.PATCH_OFFSET
            + patch_valeton.ORIGINAL_CALL
            + b"\x5a" * 128
        )
        self.executable.write_bytes(self.original)
        supported_hash = mock.patch.object(
            patch_valeton,
            "SUPPORTED_SHA256",
            hashlib.sha256(self.original).hexdigest(),
        )
        supported_hash.start()
        self.addCleanup(supported_hash.stop)

    def test_preserves_original_and_changes_only_six_bytes_in_copy(self):
        patched_path, launcher_path = patch_valeton.patch_installation(self.executable)
        patched = patched_path.read_bytes()

        self.assertEqual(self.executable.read_bytes(), self.original)
        self.assertEqual(len(patched), len(self.original))
        differences = [
            offset
            for offset, (before, after) in enumerate(zip(self.original, patched))
            if before != after
        ]
        self.assertEqual(differences, list(range(0x280C, 0x2812)))
        self.assertEqual(patched[0x280C:0x2812], b"\x90" * 6)
        self.assertTrue(os.access(launcher_path, os.X_OK))

    def test_rejects_unknown_build_without_writing_outputs(self):
        unknown = b"unknown executable"
        self.executable.write_bytes(unknown)

        with self.assertRaisesRegex(patch_valeton.PatchError, "Unsupported executable"):
            patch_valeton.patch_installation(self.executable)

        self.assertEqual(self.executable.read_bytes(), unknown)
        self.assertEqual(list(self.installation.iterdir()), [self.executable])

    def test_rejects_wrong_instruction_without_writing_outputs(self):
        changed = self.original[:0x280C] + b"\xcc" * 6 + self.original[0x2812:]
        self.executable.write_bytes(changed)

        with mock.patch.object(
            patch_valeton,
            "SUPPORTED_SHA256",
            hashlib.sha256(changed).hexdigest(),
        ):
            with self.assertRaisesRegex(patch_valeton.PatchError, "startup call was not found"):
                patch_valeton.patch_installation(self.executable)

        self.assertEqual(list(self.installation.iterdir()), [self.executable])

    def test_check_does_not_write_files(self):
        patch_valeton.patch_installation(executable=self.executable, check_only=True)
        self.assertEqual(list(self.installation.iterdir()), [self.executable])

    def test_repeating_patch_keeps_existing_outputs(self):
        patched_path, launcher_path = patch_valeton.patch_installation(self.executable)
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in [self.executable, patched_path, launcher_path]
        }

        patch_valeton.patch_installation(self.executable)

        for path, (contents, modified) in before.items():
            self.assertEqual(path.read_bytes(), contents)
            self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_existing_conflicting_file_prevents_both_outputs(self):
        for filename in [patch_valeton.PATCHED_NAME, patch_valeton.LAUNCHER_NAME]:
            with self.subTest(filename=filename):
                conflict = self.installation / filename
                conflict.write_bytes(b"keep this file")

                with self.assertRaisesRegex(patch_valeton.PatchError, "Refusing to overwrite"):
                    patch_valeton.patch_installation(self.executable)

                self.assertEqual(conflict.read_bytes(), b"keep this file")
                self.assertEqual(set(self.installation.iterdir()), {self.executable, conflict})
                conflict.unlink()

    def test_dangling_output_symlink_is_preserved(self):
        link = self.installation / patch_valeton.PATCHED_NAME
        target = self.directory / "missing.exe"
        link.symlink_to(target)

        with self.assertRaisesRegex(patch_valeton.PatchError, "symbolic link"):
            patch_valeton.patch_installation(self.executable)

        self.assertTrue(link.is_symlink())
        self.assertFalse(target.exists())
        self.assertFalse((self.installation / patch_valeton.LAUNCHER_NAME).exists())

    def test_launcher_handles_prefix_and_arguments_with_shell_characters(self):
        _, launcher_path = patch_valeton.patch_installation(self.executable)
        binary_directory = self.directory / "bin"
        binary_directory.mkdir()
        wine = binary_directory / "wine"
        wine.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['RESULT_FILE']).write_text(json.dumps({"
            "'args': sys.argv[1:], 'cwd': os.getcwd(), "
            "'prefix': os.environ.get('WINEPREFIX'), "
            "'overrides': os.environ.get('WINEDLLOVERRIDES')}))\n"
        )
        wine.chmod(0o700)
        result_file = self.directory / "result.json"
        environment = {
            **os.environ,
            "PATH": str(binary_directory) + os.pathsep + os.environ["PATH"],
            "RESULT_FILE": str(result_file),
            "WINEPREFIX": str(self.directory / "unrelated prefix"),
        }
        subprocess.run(
            [str(launcher_path), "argument with spaces", "$HOME", "$(false)"],
            cwd=self.directory,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(result_file.read_text())

        self.assertEqual(
            result["args"],
            ["Valeton Suite Wine.exe", "argument with spaces", "$HOME", "$(false)"],
        )
        self.assertEqual(result["cwd"], str(self.installation))
        self.assertEqual(result["prefix"], str(self.prefix))
        self.assertEqual(result["overrides"], "d3d11=")

    def test_installation_outside_drive_c_keeps_prefix_from_environment(self):
        executable = self.directory / "Valeton Suite.exe"
        executable.write_bytes(self.original)
        _, launcher_path = patch_valeton.patch_installation(executable)

        self.assertNotIn(b"export WINEPREFIX=", launcher_path.read_bytes())

    def test_cli_reports_unsupported_build_without_traceback(self):
        result = subprocess.run(
            [sys.executable, str(Path(patch_valeton.__file__)), str(self.executable)],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Unsupported executable", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
