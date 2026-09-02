"""github._eol - line-ending helpers shared by the write paths.

Single home for EOL normalization so the remote PR path (``_writes``:
propose_change / update_pr), the local-git path (``_gitops``:
apply_merge_resolutions) and the rehearsal path (``server/ci_runner``,
via ``github._writes`` re-exports) can never desync. Pure functions, no
package imports - safe to import from anywhere without cycles.

Canonical policy: LF (enforced repo-wide by ``.gitattributes`` and
``[tool.ruff.format] line-ending``). The target detector preserves a
base file's CRLF only to avoid whole-file churn on pre-renormalize
bases; post-renormalize every base is pure LF, so it always answers LF.
"""

from __future__ import annotations


def _normalize_eol(text: str, target: str) -> str:
    """Normalize *text* to *target* EOL ("\\n" or "\\r\\n"). Binary-safe."""
    if "\0" in text:
        return text
    # Collapse CRLF and lone CR to LF, then re-expand to target if CRLF.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if target == "\r\n":
        return normalized.replace("\n", "\r\n")
    return normalized


def _target_eol_for_text(base_text: str | None) -> str:
    """Pick EOL for a file: its majority ending, ties and empties to LF.

    Pure-CRLF bases (pre-renormalize) stay CRLF so payloads don't churn
    them; pure-LF bases and new files are LF (canonical). Mixed bases
    follow whichever ending wins; a tie falls back to LF.
    """
    if not base_text or "\0" in base_text:
        return "\n"
    crlf = base_text.count("\r\n")
    lf = base_text.count("\n") - crlf
    if crlf > lf:
        return "\r\n"
    return "\n"
