# audited on 20260903
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

export function sendInvoice(orderId: string): void {}
'''

DECLARATIONS_D_TS = "export declare function sendInvoice(orderId: string): void;\nexport function noBody(): void {}\n"

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

FORM_FEED_PY = "def first():\n    return 1\n\x0c\n# TODO: after a form feed\ndef later():\n    pass\n"

GENERIC_PROTOCOL_PY = (
    "from typing import Protocol, TypeVar\n"
    "T = TypeVar('T')\n"
    "class Reader(Protocol[T]):\n"
    "    def read(self) -> T: ...\n"
)

ABSTRACT_PY = (
    "from abc import ABC, abstractmethod\n"
    "class Base(ABC):\n"
    "    @abstractmethod\n"
    "    def run(self) -> None: ...\n"
)

BOM_PY = "﻿def fine():\n    return 1\n"

EXEMPT_DEF_PY = "def hook():  # countersign: exempt\n    pass\n"

STALE_EXEMPT_PY = "def fine():\n    return 1  # countersign: exempt\n"

EXPLAINED_PY = (
    "def close(self):\n"
    '    """Nothing to tear down; the client is stateless."""\n'
    "\n\n"
    "def downgrade():\n"
    "    # Postgres has no DROP VALUE; done by hand if ever needed.\n"
    "    pass\n"
    "\n\n"
    "def noop():\n"
    "    pass  # intentionally does nothing\n"
    "\n\n"
    "def bare():\n"
    "    pass\n"
    "\n\n"
    "def bare_ellipsis():\n"
    "    ...\n"
)

EXCEPT_PY = (
    "try:\n"
    "    loop.add_signal_handler(sig, stop.set)\n"
    "except NotImplementedError:\n"
    "    pass\n"
    "\n\n"
    "def send():\n"
    "    raise NotImplementedError\n"
)


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

    def _scan_single(self, name: str, content: str | bytes):
        target = self.root / "single" / name
        target.parent.mkdir(exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        config = Config(root=self.root, config_path=self.root / "countersign.toml", paths=[f"single/{name}"])
        return scan_tree(config)

    def test_finds_marker_fabricated_and_empty_body(self):
        findings, _exemptions, _inert, files = scan_tree(self.config)
        rule_ids = {f.rule_id for f in findings}
        self.assertIn("unfinished-marker", rule_ids)
        self.assertIn("fabricated-data", rule_ids)
        self.assertIn("empty-body", rule_ids)
        locations = {(f.path, f.line) for f in findings}
        self.assertIn(("src/service.py", 6), locations)  # empty-body reported at the def line
        self.assertGreaterEqual(files, 3)

    def test_finds_coming_soon_in_typescript(self):
        findings, _exemptions, _inert, _files = scan_tree(self.config)
        self.assertTrue(any(f.rule_id == "coming-soon" and f.path == "src/banner.ts" for f in findings))

    def test_finds_empty_body_in_typescript(self):
        findings, _exemptions, _inert, _files = scan_tree(self.config)
        hits = [(f.line, f.evidence) for f in findings if f.rule_id == "empty-body" and f.path == "src/banner.ts"]
        self.assertEqual(hits, [(5, "export function sendInvoice(orderId: string): void {}")])

    def test_typescript_empty_body_honours_exemption(self):
        findings, exemptions, _inert, _files = self._scan_single("hook.ts", "export function onIdle(): void {}  // countersign: exempt\n")
        self.assertEqual(findings, [])
        self.assertEqual(exemptions, 1)

    def test_declaration_files_are_not_structurally_checked(self):
        findings, _exemptions, _inert, _files = self._scan_single("types.d.ts", DECLARATIONS_D_TS)
        self.assertEqual(findings, [])

    def test_overloads_are_not_reported(self):
        findings, _exemptions, _inert, _files = scan_tree(self.config)
        self.assertFalse(any(f.path == "src/shapes.py" for f in findings))

    def test_exempted_line_produces_no_finding_but_counts(self):
        findings, exemptions, inert, _files = scan_tree(self.config)
        self.assertFalse(any("deliberate exemption" in f.evidence for f in findings))
        self.assertGreaterEqual(exemptions, 1)

    def test_test_files_excluded_by_policy(self):
        findings, _exemptions, _inert, _files = scan_tree(self.config)
        self.assertFalse(any(f.path.startswith("tests/") for f in findings))

    def test_test_files_scanned_when_policy_turned_off(self):
        self.config.exclude_tests = False
        findings, _exemptions, _inert, _files = scan_tree(self.config)
        self.assertTrue(any(f.path == "tests/test_api.py" for f in findings))

    def test_form_feed_keeps_line_numbers(self):
        findings, _exemptions, _inert, _files = self._scan_single("pages.py", FORM_FEED_PY)
        located = {(f.rule_id, f.line) for f in findings}
        self.assertEqual(located, {("unfinished-marker", 4), ("empty-body", 5)})

    def test_generic_protocol_methods_are_not_reported(self):
        findings, _exemptions, _inert, _files = self._scan_single("reader.py", GENERIC_PROTOCOL_PY)
        self.assertEqual(findings, [])

    def test_abstract_methods_are_not_reported(self):
        findings, _exemptions, _inert, _files = self._scan_single("base.py", ABSTRACT_PY)
        self.assertEqual(findings, [])

    def test_byte_order_mark_is_not_a_finding(self):
        findings, _exemptions, _inert, _files = self._scan_single("bom.py", BOM_PY.encode("utf-8"))
        self.assertEqual(findings, [])

    def test_null_byte_is_reported_as_unparseable(self):
        findings, _exemptions, _inert, _files = self._scan_single("broken.py", b"def fine():\n    return 1\n\x00")
        self.assertEqual([f.rule_id for f in findings], ["unparseable"])

    def test_exempted_empty_body_counts_once(self):
        findings, exemptions, inert, _files = self._scan_single("hook.py", EXEMPT_DEF_PY)
        self.assertEqual(findings, [])
        self.assertEqual(exemptions, 1)
        self.assertEqual(inert, 0)

    def test_explained_empty_bodies_are_decisions_not_stubs(self):
        findings, _exemptions, _inert, _files = self._scan_single("explained.py", EXPLAINED_PY)
        self.assertEqual([(f.line, f.rule_id) for f in findings], [(14, "empty-body"), (18, "empty-body")])

    def test_catching_not_implemented_error_is_not_raising_it(self):
        findings, _exemptions, _inert, _files = self._scan_single("signals.py", EXCEPT_PY)
        self.assertEqual([(f.line, f.rule_id) for f in findings], [(8, "not-implemented-error")])

    def test_marker_that_suppresses_nothing_is_reported_as_inert_not_used(self):
        findings, exemptions, inert, _files = self._scan_single("stale.py", STALE_EXEMPT_PY)
        self.assertEqual(findings, [])
        self.assertEqual(exemptions, 0)
        self.assertEqual(inert, 1)


if __name__ == "__main__":
    unittest.main()
