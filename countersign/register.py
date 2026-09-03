# audited on 20260903
"""The evidence register: append only, hash chained, plain files.

Every check Countersign runs, every finding it produces and every claim
verdict is written here as one line of JSON. Each line carries the hash of
the line before it, so any later edit to any earlier line breaks the chain
and ``verify_chain`` says so. This is what makes a receipt tamper evident:
not a claim on a website, but arithmetic anyone can redo.

Adapted from Gaigentic Verify's register (2026), which ran this exact design
through a file-by-file production review.

Deliberately a file, not a database. It has to run inside any repository on
any machine on day one, and a file is something an auditor can copy, diff
and keep.

Appends take an exclusive lock on a sibling ``.lock`` file for the read-head,
write-entry pair. Without it, two runs started at the same moment (two CI
jobs on one checkout, a human and a hook) could both chain onto the same
head and leave a register that is broken forever, indistinguishable from
tampering.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Windows has no fcntl; msvcrt provides byte-range locks
    fcntl = None
    import msvcrt

GENESIS = "0" * 64


class RegisterDamaged(ValueError):
    """The file cannot be extended without breaking the chain it carries."""


def _canonical(payload: dict[str, Any]) -> str:
    """Stable JSON so the same entry always hashes to the same value."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def entry_hash(previous_hash: str, body: dict[str, Any]) -> str:
    return hashlib.sha256((previous_hash + _canonical(body)).encode("utf-8")).hexdigest()


@contextlib.contextmanager
def _exclusive(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``lock_path`` for the block."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@dataclass
class Register:
    """Append-only log of everything a verification run did."""

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    @property
    def lock_path(self) -> Path:
        return self.path.parent / (self.path.name + ".lock")

    # ---- writing ----

    def append(self, kind: str, body: dict[str, Any], *, at: datetime | None = None) -> dict[str, Any]:
        """Add one entry and return it, including its position and hash.

        The entry is on the disk before this returns. Evidence that a caller
        has been told was written, and that a power cut then removes, would
        be worse than evidence never written at all: the register would be
        short an entry and nobody would know which.
        """
        with _exclusive(self.lock_path):
            previous = self.head()
            previous_hash = previous["hash"] if previous else GENESIS
            index = (previous["index"] + 1) if previous else 0
            recorded_at = (at or datetime.now(timezone.utc)).isoformat()

            core = {"index": index, "kind": kind, "recorded_at": recorded_at, "body": body}
            entry = {**core, "previous_hash": previous_hash, "hash": entry_hash(previous_hash, core)}

            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return entry

    # ---- reading ----

    def entries(self) -> Iterator[dict[str, Any]]:
        """Every entry, in order, without verifying the chain. Use
        ``verify_chain`` first when the file may have been touched."""
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def _last_line(self) -> str | None:
        """The final line, read by seeking rather than by reading everything.

        Appending needs only the entry before it. Reading the whole file to
        find that entry makes each append cost more than the last, so a
        register that runs for years gets slower every month it is used.
        """
        if not self.path.exists():
            return None
        with self.path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            collected = b""
            while position > 0:
                step = min(4096, position)
                position -= step
                handle.seek(position)
                collected = handle.read(step) + collected
                stripped = collected.rstrip(b"\n")
                if b"\n" in stripped:
                    return stripped.rsplit(b"\n", 1)[1].decode("utf-8", errors="replace")
            final = collected.strip()
            return final.decode("utf-8", errors="replace") if final else None

    def head(self) -> dict[str, Any] | None:
        line = self._last_line()
        if line is None:
            return None
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or not isinstance(entry.get("index"), int) or not isinstance(entry.get("hash"), str):
                raise ValueError("the line is not a register entry")
        except ValueError as exc:  # json.JSONDecodeError is a ValueError
            raise RegisterDamaged(
                f"the last line of {self.path.name} cannot be read as an entry, so nothing can "
                "be added after it without breaking the chain. Keep the file and investigate "
                f"what wrote it: {exc}"
            ) from None
        return entry

    def verify_chain(self) -> tuple[bool, str]:
        """Recompute every hash. Returns (intact, human readable reason).

        A line that cannot even be parsed is itself a broken chain, not a
        crash: the likeliest way a register gets corrupted is someone opening
        the file in an editor, and the verdict has to survive whatever they
        saved, including bytes that are not text.
        """
        previous_hash = GENESIS
        expected_index = 0
        if not self.path.exists():
            return True, "0 entries, chain intact"
        with self.path.open(encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    index = entry["index"]
                    core = {k: entry[k] for k in ("index", "kind", "recorded_at", "body")}
                    recorded_previous = entry["previous_hash"]
                    recorded_hash = entry["hash"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    return False, (
                        f"line {line_number} cannot be read as an entry; the register has been "
                        "altered since it was written"
                    )
                if index != expected_index:
                    return False, f"entry {index} is out of order, expected {expected_index}"
                if recorded_previous != previous_hash:
                    return False, f"entry {index} does not follow the entry before it"
                if entry_hash(previous_hash, core) != recorded_hash:
                    return False, f"entry {index} has been altered since it was written"
                previous_hash = recorded_hash
                expected_index += 1
        return True, f"{expected_index} entries, chain intact"
