# audited on 20260903
import tempfile
import unittest
from pathlib import Path

from countersign.config import Config, ConfigError, is_test_file


class TestIsTestFile(unittest.TestCase):
    def test_prefixes_suffixes_and_directories(self):
        self.assertTrue(is_test_file(Path("src/test_thing.py")))
        self.assertTrue(is_test_file(Path("src/thing_test.go")))
        self.assertTrue(is_test_file(Path("web/app.spec.tsx")))
        self.assertTrue(is_test_file(Path("tests/helpers.py")))
        self.assertTrue(is_test_file(Path("pkg/__tests__/x.js")))
        self.assertFalse(is_test_file(Path("src/testing_utils.py")))
        self.assertFalse(is_test_file(Path("src/contest.py")))
        self.assertFalse(is_test_file(Path("src/attestation/service.py")))


class TestConfigLoad(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "countersign.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text: str) -> Config:
        self.path.write_text(text, encoding="utf-8")
        return Config.load(self.path)

    def test_missing_file_uses_defaults(self):
        config = Config.load(self.path)
        self.assertEqual(config.paths, ["."])
        self.assertEqual(config.claims_file, "claims.toml")
        self.assertTrue(config.exclude_tests)
        self.assertEqual(config.timeout_s, 300)

    def test_malformed_toml_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            self._write("[scan\npaths = [")

    def test_wrong_types_are_config_errors(self):
        with self.assertRaises(ConfigError):
            self._write('[scan]\npaths = "src"\n')
        with self.assertRaises(ConfigError):
            self._write("[run]\ntimeout_s = 0\n")
        with self.assertRaises(ConfigError):
            self._write("[run]\ntimeout_s = true\n")
        with self.assertRaises(ConfigError):
            self._write('[scan]\nexclude_tests = "yes"\n')
        with self.assertRaises(ConfigError):
            self._write('[scan]\nexempt_marker = ""\n')

    def test_empty_claims_file_disables_claims(self):
        config = self._write('[claims]\nfile = ""\n')
        self.assertIsNone(config.claims_file)
        self.assertIsNone(config.claims_path())

    def test_scan_path_outside_root_is_a_config_error(self):
        config = self._write('[scan]\npaths = [".."]\n')
        with self.assertRaises(ConfigError):
            config.collect_files()

    def test_missing_scan_path_is_a_config_error_not_a_silent_pass(self):
        config = self._write('[scan]\npaths = ["src"]\n')
        with self.assertRaises(ConfigError):
            config.collect_files()

    def test_collect_files_applies_every_filter(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "node_modules").mkdir()
        (self.root / "src" / "keep.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "src" / "notes.md").write_text("# prose\n", encoding="utf-8")
        (self.root / "src" / "test_keep.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "src" / "node_modules" / "dep.js").write_text("x = 1\n", encoding="utf-8")
        config = self._write('[scan]\npaths = ["src"]\n')
        root = self.root.resolve()  # collect_files works on the resolved root (macOS /var is a symlink)
        names = [str(p.relative_to(root)) for p in config.collect_files()]
        self.assertEqual(names, ["src/keep.py"])
        config.exclude_tests = False
        names = [str(p.relative_to(root)) for p in config.collect_files()]
        self.assertEqual(names, ["src/keep.py", "src/test_keep.py"])


if __name__ == "__main__":
    unittest.main()
