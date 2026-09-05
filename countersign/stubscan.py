# audited on 20260903
"""The marker scan: unfinished work that looks like finished work.

This engine is generalized from a gate that ran daily on a production tree
of 546+ source files, reviewed file by file, and was tuned to zero false
positives: patterns are deliberately narrow. A check that cries wolf gets
ignored, so every rule here earned its place by catching a real defect class
and never firing on honest code.

Two layers:

1. Marker rules (all languages in scope): regex over each line, ported from
   the proven gate.
2. Structural rules: functions whose body does nothing and says nothing
   about why. A bare ``pass``, ``...`` or ``{}`` is unfinished work; the
   same body with a docstring or a comment is a documented decision (an
   Alembic downgrade that cannot be undone, a ``close()`` with nothing to
   close, a click group) and is not reported. Whatever the explanation
   admits is caught by the marker rules. Python is checked through the ast
   module (overloads, abstract methods and Protocol methods excluded);
   TypeScript and JavaScript by ``jsscan``.

Exemptions are in the file itself: append the exemption marker to a line
that is a genuine false positive (a UI label, a vendor capability note).
Exemptions are counted and reported on every receipt, so they stay visible
instead of rotting in a config file. Only a marker that suppressed a finding
counts as used; a marker on a line no rule would have flagged is inert, and
inert markers are reported separately so a stale one cannot hide.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .jsscan import empty_functions as _js_empty_functions

# Decorators under which an empty body is the whole point.
EMPTY_BODY_EXEMPT_DECORATORS = frozenset({"overload", "abstractmethod"})
# Base classes whose methods are declarations, not implementations.
EMPTY_BODY_EXEMPT_BASES = frozenset({"Protocol"})

JS_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})

# Python's tokenizer ends a line only at \r\n, \r or \n. str.splitlines()
# also splits on form feeds and several Unicode separators, which would put
# every line number after a form feed off by one.
_LINE_BREAK = re.compile(r"\r\n|\r|\n")


@dataclass(frozen=True)
class Rule:
    rule_id: str
    pattern: "re.Pattern[str]"
    why: str
    plain: str  # the same finding, in words for someone who did not write the rule


def _rule(rule_id: str, source: str, flags: int, why: str, plain: str) -> Rule:
    return Rule(rule_id, re.compile(source, flags), why, plain)


RULES: list[Rule] = [
    _rule("unfinished-marker", r"\b(TODO|FIXME|XXX|HACK)\b", 0, "unfinished-work marker; finish the work or record the question where the team keeps them", "a note left for later instead of finished work"),  # countersign: exempt
    _rule("not-implemented", r"not (yet implemented|implemented yet)", re.IGNORECASE, "the code declares itself unimplemented", "code that declares itself unfinished"),
    _rule("not-implemented-error", r"\b(raise|throw)\s+(new\s+)?NotImplemented(Error|Exception)\b", 0, "raises instead of doing the work", "code that raises an error instead of doing the work"),
    _rule("deferred-implementation", r"implemented (later|in a future)", re.IGNORECASE, "defers the implementation", "code that says the work will be done another time"),
    _rule("stub-word", r"\bstub(bed)?\b", re.IGNORECASE, "unfinished stand-in code", "code marked as a stand-in"),  # countersign: exempt
    _rule("fabricated-data", r"(fake|dummy|mock|sample|placeholder|hardcoded|hard-coded) (data|value|values|response|result|results|payload)", re.IGNORECASE, "fabricated values standing in for a real query or API call", "made-up data standing in for a real result"),
    _rule("simplified-implementation", r"(simplified|simplistic) (implementation|version|approach)", re.IGNORECASE, "a deliberately incomplete implementation", "a deliberately incomplete version"),
    _rule("real-implementation-deferred", r"in (a )?real (implementation|system|world|deployment)", re.IGNORECASE, "describes what production would do instead of doing it", "a description of what the real thing would do, instead of doing it"),
    _rule("would-be-done", r"would be (implemented|replaced|fetched|queried)", re.IGNORECASE, "describes work not done", "a description of work not done"),
    _rule("coming-soon", r"coming soon", re.IGNORECASE, "a feature advertised as absent", "text announcing a feature that is not there"),  # countersign: exempt
    _rule("empty-return-standin", r"return (\[\]|\{\}|None|null)\s*(#|//)\s*(TODO|placeholder|stub|for now)", re.IGNORECASE, "returns empty as a stand-in for a real result", "an empty result returned as a stand-in"),  # countersign: exempt
]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule_id: str
    why: str
    evidence: str


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name):
            names.add(decorator.id)
        elif isinstance(decorator, ast.Attribute):
            names.add(decorator.attr)
    return names


def _base_name(base: ast.expr) -> str | None:
    """The class name a base expression refers to: ``Protocol``,
    ``typing.Protocol`` and ``Protocol[T]`` all answer ``Protocol``."""
    if isinstance(base, ast.Subscript):
        base = base.value
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _inside_protocol(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.ClassDef):
            return any(_base_name(base) in EMPTY_BODY_EXEMPT_BASES for base in parent.bases)
        parent = parents.get(parent)
    return False


def _explained(lines: list[str], def_line: int, end_line: int, exempt_marker: str) -> bool:
    """True when a comment with words in it sits anywhere from the ``def``
    line to the end of the body. Only called for bodies that are nothing
    but ``pass`` or ``...``, so a ``#`` in that range is a comment (or, on
    the def line, a ``#`` inside a default value, which errs towards not
    reporting). A comment that is only the exemption marker explains
    nothing; it is counted as an exemption instead.
    """
    for index in range(max(def_line, 1) - 1, min(end_line, len(lines))):
        line = lines[index]
        if "#" not in line:
            continue
        comment = line[line.index("#"):].replace(exempt_marker, "")
        if comment.strip("# \t"):
            return True
    return False


def _python_empty_functions(source: str, exempt_marker: str = "") -> list[tuple[int, str]]:
    """Line numbers of functions whose body does nothing and explains nothing.

    A docstring or a comment inside the body makes it a documented no-op,
    which is a decision, not unfinished work. The exemption marker alone is
    not an explanation. Overloads, abstract methods and Protocol methods are
    the legitimate empty body and are not reported.
    """
    tree = ast.parse(source)
    lines = _LINE_BREAK.split(source)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    reported: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _decorator_names(node) & EMPTY_BODY_EXEMPT_DECORATORS:
            continue
        if _inside_protocol(node, parents):
            continue
        body = list(node.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            continue  # a docstring is the author's explanation of the empty body
        if not body:
            reported.append((node.lineno, node.name))
            continue
        if _explained(lines, node.lineno, node.end_lineno or body[-1].lineno, exempt_marker):
            continue
        does_something = False
        for statement in body:
            if isinstance(statement, ast.Pass):
                continue
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and statement.value.value is Ellipsis:
                continue
            does_something = True
            break
        if not does_something:
            reported.append((node.lineno, node.name))
    return reported


def _is_structural_js_target(path: Path) -> bool:
    """Declaration files carry no bodies; minified files carry polyfill noise."""
    name = path.name
    return not name.endswith(".d.ts") and ".min." not in name


def scan_file(config: Config, relative: str, absolute: Path) -> tuple[list[Finding], int, int]:
    """Scan one file. Returns (findings, exemptions_used, inert_markers)."""
    try:
        # utf-8-sig drops a byte order mark when present and is a plain
        # utf-8 read otherwise; a BOM is not a defect in the file.
        text = absolute.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return [], 0, 0

    findings: list[Finding] = []
    exempt_lines: set[int] = set()
    used_exemptions: set[int] = set()
    lines = _LINE_BREAK.split(text)

    for number, line in enumerate(lines, start=1):
        exempt = config.exempt_marker in line
        if exempt:
            exempt_lines.add(number)
        for rule in RULES:
            if rule.pattern.search(line):
                if exempt:
                    used_exemptions.add(number)
                else:
                    findings.append(Finding(relative, number, rule.rule_id, rule.why, line.strip()[:240]))

    empty: list[tuple[int, str]] = []
    if absolute.suffix == ".py":
        try:
            empty = _python_empty_functions(text, config.exempt_marker)
        except (SyntaxError, ValueError):
            # SyntaxError covers broken code; ValueError is what Python 3.11
            # raises for a null byte in the source, which 3.12+ reports as a
            # SyntaxError. Either way the file is not honest Python.
            findings.append(Finding(relative, 1, "unparseable", "the file cannot be parsed as Python; an agent may have left it broken", ""))
    elif absolute.suffix in JS_SUFFIXES and _is_structural_js_target(absolute):
        empty = _js_empty_functions(text)
    for lineno, name in empty:
        if lineno in exempt_lines:
            used_exemptions.add(lineno)
            continue
        marker_line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        findings.append(
            Finding(relative, lineno, "empty-body", f"function '{name}' has a body that does nothing", marker_line.strip()[:240])
        )

    return findings, len(used_exemptions), len(exempt_lines - used_exemptions)


def scan_tree(config: Config, files: list[Path] | None = None) -> tuple[list[Finding], int, int, int]:
    """Scan every collected file.

    Returns (findings, exemptions_used, inert_markers, files_scanned).
    ``files`` lets a caller that already collected the file list (to record
    it before scanning) hand it over instead of collecting twice.
    """
    findings: list[Finding] = []
    exemptions = 0
    inert = 0
    if files is None:
        files = config.collect_files()
    root = Path(config.root).resolve()
    for absolute in files:
        relative = str(absolute.relative_to(root))
        file_findings, file_exemptions, file_inert = scan_file(config, relative, absolute)
        findings.extend(file_findings)
        exemptions += file_exemptions
        inert += file_inert
    findings.sort(key=lambda f: (f.path, f.line, f.rule_id))
    return findings, exemptions, inert, len(files)
