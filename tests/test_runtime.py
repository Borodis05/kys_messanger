import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from shared.runtime import application_directory, runtime_path


class RuntimePathTests(unittest.TestCase):
    def test_source_mode_uses_current_directory(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            with patch("pathlib.Path.cwd", return_value=Path(temporary_directory)):
                self.assertEqual(application_directory(), Path(temporary_directory))
                self.assertEqual(
                    runtime_path("certs/ca.crt"),
                    str(Path(temporary_directory) / "certs/ca.crt"),
                )

    def test_frozen_mode_uses_executable_directory(self) -> None:
        executable = Path("C:/portable-chat/ChatClient.exe")
        with patch.object(sys, "frozen", True, create=True):
            with patch.object(sys, "executable", str(executable)):
                self.assertEqual(application_directory(), executable.parent)

    def test_absolute_path_is_not_changed(self) -> None:
        absolute_path = Path.cwd() / "custom-ca.crt"
        self.assertEqual(runtime_path(str(absolute_path)), str(absolute_path))


if __name__ == "__main__":
    unittest.main()
