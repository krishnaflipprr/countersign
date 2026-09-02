import tempfile
import unittest
from pathlib import Path

from countersign.config import Config
from countersign.stubscan import scan_tree


SERVICE_PY = '''def real_function(x):
    total = x + 1
    return total


def empty_implementation(y):
    ...


# TODO: connect the feed
def get_feed():
    return {"items": []}  # fake data until the feed lands


def exempted_line():
    return None  # TODO: deliberate exemption, this label is honest  # countersign: exempt
'''

BANNER_TS = '''export function betaBanner(): string {
  return "Coming soon";
}
'''

OVERLOAD_PY = '''from typing import overload


@overload
def parse(x: int) -> int: ...
@overload
def parse(x: str) -> str: ...
def parse(x):
    return int(x) if isinstance(x, int) else x
'''

TEST_FILE_PY = """# TODO: tests may carry markers, the scan excludes them by policy
def test_thing():
    assert True
"""


class TestStubScan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "service.py").write_text(SERVICE_PY, encoding="utf-8")
        (self.root / "src" / "banner.ts").write_text(BANNER_TS, encoding="utf-8")
        (self.root / "src" / "shapes.py").write_text(OVERLOAD_PY, encoding="utf-8")
        (self.root / "tests" / "test_api.py").write_text(TEST_FILE_PY, encoding="utf-8")
        self.config = Config(root=self.root, config_path=self.root / "countersign.toml", paths=["."])

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_marker_fabricated_and_empty_body(self):
        findings, _exemptions, files = scan_tree(self.config)
        rule_ids = {f.rule_id for f in findings}
        self.assertIn("unfinished-marker", rule_ids)
        self.assertIn("fabricated-data", rule_ids)
        self.assertIn("empty-body", rule_ids)
        locations = {(f.path, f.line) for f in findings}
        self.assertIn(("src/service.py", 6), locations)  # empty-body reported at the def line
        self.assertGreaterEqual(files, 3)

    def test_finds_coming_soon_in_typescript(self):
        findings, _exemptions, _files = scan_tree(self.config)
        self.assertTrue(any(f.rule_id == "coming-soon" and f.path == "src/banner.ts" for f in findings))

    def test_overloads_are_not_reported(self):
        findings, _exemptions, _files = scan_tree(self.config)
        self.assertFalse(any(f.path == "src/shapes.py" for f in findings))

    def test_exempted_line_produces_no_finding_but_counts(self):
        findings, exemptions, _files = scan_tree(self.config)
        self.assertFalse(any("deliberate exemption" in f.evidence for f in findings))
        self.assertGreaterEqual(exemptions, 1)

    def test_test_files_excluded_by_policy(self):
        findings, _exemptions, _files = scan_tree(self.config)
        self.assertFalse(any(f.path.startswith("tests/") for f in findings))

    def test_test_files_scanned_when_policy_turned_off(self):
        self.config.exclude_tests = False
        findings, _exemptions, _files = scan_tree(self.config)
        self.assertTrue(any(f.path == "tests/test_api.py" for f in findings))


if __name__ == "__main__":
    unittest.main()
