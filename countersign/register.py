"""The evidence register: append only, hash chained, plain files.

Every check Countersign runs, every finding it produces and every claim
verdict is written here as one line of JSON. Each line carries the hash of
the line before it, so any later edit to any earlier line breaks the chain
and ``verify_chain`` says so. This is what makes a receipt tamper evident:
not a claim on a website, but arithmetic anyone can redo.

Adapted from Gaigentic Verify's register (2026), which ran this exact design
through a file-by-file production certification.

Deliberately a file, not a database. It has to run inside any repository on
any machine on day one, and a file is something an auditor can copy, diff
and keep.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GENESIS = "0" * 64


class RegisterDamaged(ValueError):
    """The file cannot be extended without breaking the chain it carries."""


def _canonical(payload: dict[str, Any]) -> str:
    """Stable JSON so the same entry always hashes to the same value."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def entry_hash(previous_hash: str, body: dict[str, Any]) -> str:
    return hashlib.sha256((previous_hash + _canonical(body)).encode("utf-8")).hexdigest()


@dataclass
class Register:
    """Append-only log of everything a verification run did."""

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---- writing ----

    def append(self, kind: str, body: dict[str, Any], *, at: datetime | None = None) -> dict[str, Any]:
        """Add one entry and return it, including its position and hash.

        The entry is on the disk before this returns. Evidence that a caller
        has been told was written, and that a power cut then removes, would
        be worse than evidence never written at all: the register would be
        short an entry and nobody would know which.
        """
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
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
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
                    return stripped.rsplit(b"\n", 1)[1].decode("utf-8")
            final = collected.strip()
            return final.decode("utf-8") if final else None

    def head(self) -> dict[str, Any] | None:
        line = self._last_line()
        if line is None:
            return None
        try:
            entry: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
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
        saved.
        """
        previous_hash = GENESIS
        expected_index = 0
        if not self.path.exists():
            return True, "0 entries, chain intact"
        with self.path.open(encoding="utf-8") as handle:
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
