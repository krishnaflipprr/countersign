# audited on 20260903
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from countersign.cli import main
from countersign.config import Config
from countersign.claims import load_claims
from countersign.starter import detect_starter_claims


class TestInit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config_path = self.root / "countersign.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def _init(self, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["init", "--config", str(self.config_path), *extra])
        return code, out.getvalue()

    def test_node_repository_gets_test_lint_and_typecheck_claims(self):
        (self.root / "package.json").write_text(json.dumps({
            "scripts": {"test": "vitest run", "lint": "eslint ."},
            "devDependencies": {"typescript": "^5"},
        }), encoding="utf-8")
        (self.root / "tsconfig.json").write_text("{}", encoding="utf-8")
        (self.root / "pnpm-lock.yaml").write_text("", encoding="utf-8")
        code, output = self._init()
        self.assertEqual(code, 0, output)
        config = Config.load(self.config_path)
        self.assertEqual(config.required_claims, ["tests-pass"])
        self.assertTrue(config.fail_on_weakened)
        claims = {c.claim_id: c.command for c in load_claims(self.root / "claims.toml")}
        self.assertEqual(claims, {"tests-pass": "pnpm test", "lint-clean": "pnpm run lint", "types-check": "npx tsc --noEmit"})

    def test_python_repository_with_pytest(self):
        (self.root / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['tests']\n[tool.ruff]\nline-length = 100\n", encoding="utf-8")
        code, _output = self._init()
        self.assertEqual(code, 0)
        claims = {c.claim_id: c.command for c in load_claims(self.root / "claims.toml")}
        self.assertEqual(claims, {"tests-pass": "python3 -m pytest -q", "lint-clean": "ruff check ."})

    def test_python_repository_without_pytest_uses_unittest(self):
        (self.root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        claims = {c.claim_id: c.command for c in detect_starter_claims(self.root)}
        self.assertEqual(claims, {"tests-pass": "python3 -m unittest discover -s tests -t ."})

    def test_go_and_rust(self):
        (self.root / "go.mod").write_text("module x\n", encoding="utf-8")
        claims = {c.claim_id: c.command for c in detect_starter_claims(self.root)}
        self.assertEqual(claims, {"tests-pass": "go test ./...", "vet-clean": "go vet ./..."})
        (self.root / "go.mod").unlink()
        (self.root / "Cargo.toml").write_text("[package]\nname = 'x'\n", encoding="utf-8")
        claims = {c.claim_id: c.command for c in detect_starter_claims(self.root)}
        self.assertEqual(claims, {"tests-pass": "cargo test"})

    def test_unrecognised_repository_gets_a_commented_example_and_no_required_claims(self):
        code, output = self._init()
        self.assertEqual(code, 0)
        self.assertIn("no build files recognised", output)
        config = Config.load(self.config_path)
        self.assertEqual(config.required_claims, [])
        self.assertIsNone(load_claims(self.root / "claims.toml") or None)
        self.assertIn("[[claim]]", (self.root / "claims.toml").read_text(encoding="utf-8"))

    def test_existing_claims_file_is_kept_and_existing_config_needs_force(self):
        (self.root / "claims.toml").write_text('[[claim]]\nid = "mine"\nstatement = "M"\ncommand = "true"\n', encoding="utf-8")
        code, output = self._init()
        self.assertEqual(code, 0)
        self.assertIn("kept existing", output)
        self.assertEqual([c.claim_id for c in load_claims(self.root / "claims.toml")], ["mine"])
        code, _output = self._init()
        self.assertEqual(code, 2)
        code, _output = self._init("--force")
        self.assertEqual(code, 0)

    def test_generated_config_verifies_a_clean_tree(self):
        (self.root / "ok.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
        self._init()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["verify", "--config", str(self.config_path), "--no-color", "--no-pack"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("COUNTERSIGNED", out.getvalue())


if __name__ == "__main__":
    unittest.main()
