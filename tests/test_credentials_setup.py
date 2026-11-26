import stat
import tempfile
import unittest
from pathlib import Path

from stackorion.credentials_setup import save_spaceship_credential


class SpaceshipCredentialSetupTests(unittest.TestCase):
    def test_writes_private_file_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "cloud-ops"
            parent.mkdir(mode=0o700)
            directory = parent / "env"
            directory.mkdir(mode=0o700)
            path = save_spaceship_credential(directory, "test-key", "test-secret")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn("SPACESHIP_API_KEY=test-key", path.read_text())
            with self.assertRaisesRegex(ValueError, "already exists"):
                save_spaceship_credential(directory, "second-key", "second-secret")
            self.assertNotIn("second-key", path.read_text())

    def test_rejects_world_accessible_directory_and_bad_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "cloud-ops"
            parent.mkdir(mode=0o700)
            directory = parent / "env"
            directory.mkdir(mode=0o755)
            with self.assertRaisesRegex(ValueError, "0700"):
                save_spaceship_credential(directory, "test-key", "test-secret")
            directory.chmod(0o700)
            with self.assertRaisesRegex(ValueError, "whitespace"):
                save_spaceship_credential(directory, "test key", "test-secret")
            self.assertFalse((directory / "spaceship.env").exists())


if __name__ == "__main__":
    unittest.main()
