"""Artifact readers, chosen by file type (B11).

- ``.pdf`` (or a ``%PDF-`` header): the text comes from an external extractor
  run as a subprocess (default ``pdftotext``). The raw bytes are never scanned.
  With no extractor installed, or when it fails, the file is skipped with a
  ``skipped_pdf`` note and a reason.
- ``.ipynb``: the cell sources, joined. Outputs are not scanned. Unparseable
  JSON falls back to reading the raw text, so a broken notebook is never
  scanned less than a text file.
- Any other file containing a NUL byte is binary: it is hashed, not scanned,
  and gets a ``skipped_binary`` note.
- Everything else is text: UTF-8 with replacement characters, universal
  newlines (the same reading the legacy scanner did).

Standard library only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

# {path} is replaced by the artifact path. The extractor writes text to stdout.
DEFAULT_PDF_COMMAND: tuple[str, ...] = ("pdftotext", "-q", "{path}", "-")
PDF_TIMEOUT_S = 60.0


@dataclasses.dataclass(frozen=True)
class Artifact:
    kind: str                 # text | notebook | pdf | binary
    text: str | None          # what the scanner reads; None when skipped
    note: str | None = None   # skipped_pdf | skipped_binary
    reason: str | None = None
    sha256: str | None = None


def _text_from(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _notebook_text(data: bytes) -> str | None:
    try:
        nb = json.loads(data.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    cells = nb.get("cells") if isinstance(nb, dict) else None
    if not isinstance(cells, list):
        return None
    parts: list[str] = []
    for cell in cells:
        src = cell.get("source", "") if isinstance(cell, dict) else ""
        if isinstance(src, list):
            parts.append("".join(str(piece) for piece in src))
        else:
            parts.append(str(src))
    return "\n".join(parts) + ("\n" if parts else "")


def _pdf(path: Path, pdf_command: Sequence[str] | None) -> Artifact:
    template = tuple(pdf_command) if pdf_command else DEFAULT_PDF_COMMAND
    exe = template[0]
    if shutil.which(exe) is None:
        return Artifact("pdf", None, "skipped_pdf",
                        f"PDF extractor {exe!r} is not installed; PDF not scanned")
    argv = [arg.replace("{path}", str(path)) for arg in template]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=PDF_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return Artifact("pdf", None, "skipped_pdf",
                        f"PDF extractor failed to run: {exc}")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[:1]
        detail = err[0] if err else f"exit status {proc.returncode}"
        return Artifact("pdf", None, "skipped_pdf",
                        f"PDF extractor failed: {detail}")
    return Artifact("pdf", _text_from(proc.stdout))


def read_artifact(path, pdf_command: Sequence[str] | None = None) -> Artifact:
    """Read one file as the scanner should see it, chosen by file type."""
    path = Path(path)
    data = path.read_bytes()
    if path.suffix.lower() == ".pdf" or data.startswith(b"%PDF-"):
        return _pdf(path, pdf_command)
    if path.suffix.lower() == ".ipynb":
        text = _notebook_text(data)
        if text is not None:
            return Artifact("notebook", text)
    if b"\x00" in data:
        return Artifact("binary", None, "skipped_binary",
                        "contains NUL bytes: not text; hashed, not scanned",
                        hashlib.sha256(data).hexdigest())
    return Artifact("text", _text_from(data))
