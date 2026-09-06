"""
viewer/_utils.py - shared HTML rendering helpers for the viewer and admin panels.

Pure formatting utilities with no domain logic. Both viewer/ and server/admin.py
import from here instead of server/admin.py reaching into viewer/ private names.
"""

from __future__ import annotations

import html
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Generic, TypeVar

import config

HOST = config.VIEWER_HOST
PORT = config.VIEWER_PORT

_WS_RE = re.compile(r"\s+")
_ORDERED_LIST_RE = re.compile(r"^\d+[.)] ")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*[-:]+\s*(\|\s*[-:]+\s*)*\|?\s*$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def esc(text: object) -> str:
    return html.escape(str(text))


@lru_cache(maxsize=128)
def _parse_iso_cached(raw: str) -> datetime | None:
    """Cached ISO parse for _human_ts: strip Z/+00:00, fromisoformat, utc."""
    text = raw.rstrip("Z")
    if text.endswith("+00:00"):
        text = text[:-6]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:  # domain: degrade-silently - malformed timestamp renders raw
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _human_ts(value: str) -> str:
    """A readable timestamp: relative ('3 h ago') for the last 24 hours,
    relative by day ('2 d ago') for the first 30 days, then a short local
    date ('Aug 11, 2026'). The exact UTC timestamp rides along on hover.
    Falls back to the raw value if it can't be parsed."""
    raw = str(value)
    dt = _parse_iso_cached(raw)
    if dt is None:
        return esc(raw)
    delta = datetime.now(timezone.utc) - dt
    if delta < timedelta(seconds=60):
        label = "just now"
    elif delta < timedelta(hours=1):
        label = f"{max(1, int(delta.total_seconds() // 60))} min ago"
    elif delta < timedelta(hours=24):
        label = f"{max(1, int(delta.total_seconds() // 3600))} h ago"
    elif delta < timedelta(days=30):
        label = f"{max(1, int(delta.total_seconds() // 86400))} d ago"
    else:
        label = dt.astimezone().strftime("%b %d, %Y")
    return f'<span title="{esc(raw)} UTC">{esc(label)}</span>'


def _human_ts_absolute(value: str) -> str:
    """A timestamp shown as an absolute local time ('Aug 11, 2026 20:16:25')
    with the exact UTC value on hover - for a 'now' reading, where a relative
    label like 'just now' would be tautological. Falls back to the raw value
    if it can't be parsed."""
    raw = str(value)
    dt = _parse_iso_cached(raw)
    if dt is None:
        return esc(raw)
    label = dt.astimezone().strftime("%b %d, %Y %H:%M:%S")
    return f'<span title="{esc(raw)} UTC">{esc(label)}</span>'


def _ts_or_dash(value: str | None) -> str:
    """_human_ts, but a muted em-dash when there is no timestamp at all."""
    if not value:
        return '<span style="color:var(--muted)">—</span>'
    return _human_ts(value)


def _human_ts_until(value: str) -> str:
    """A readable future deadline: 'in 3 h' / 'in 2 d' for a timestamp still
    ahead, '1 h ago' for one already past, with the exact UTC value on hover -
    for an 'expires' reading, where a past-relative _human_ts would mislabel a
    future timestamp as 'just now'. Falls back to the raw value if it can't be
    parsed."""
    raw = str(value)
    dt = _parse_iso_cached(raw)
    if dt is None:
        return esc(raw)
    now = datetime.now(timezone.utc)
    if dt > now:
        remaining = dt - now
        if remaining < timedelta(seconds=60):
            label = "in under a minute"
        elif remaining < timedelta(hours=1):
            label = f"in {max(1, int(remaining.total_seconds() // 60))} min"
        elif remaining < timedelta(hours=24):
            label = f"in {max(1, int(remaining.total_seconds() // 3600))} h"
        elif remaining < timedelta(days=30):
            label = f"in {max(1, int(remaining.total_seconds() // 86400))} d"
        else:
            label = dt.astimezone().strftime("%b %d, %Y")
    else:
        delta = now - dt
        if delta < timedelta(seconds=60):
            label = "just now"
        elif delta < timedelta(hours=1):
            label = f"{max(1, int(delta.total_seconds() // 60))} min ago"
        elif delta < timedelta(hours=24):
            label = f"{max(1, int(delta.total_seconds() // 3600))} h ago"
        else:
            label = f"{max(1, int(delta.total_seconds() // 86400))} d ago"
    return f'<span title="{esc(raw)} UTC">{esc(label)}</span>'


def _ts_or_dash_until(value: str | None) -> str:
    """_human_ts_until, but a muted em-dash when there is no timestamp."""
    if not value:
        return '<span style="color:var(--muted)">—</span>'
    return _human_ts_until(value)


def _rows(pairs: list[tuple[str, str]]) -> str:
    """Key/value table rows. Keys are escaped; values are pre-built HTML (use
    esc() at the call site for plain text)."""
    return "".join(f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in pairs)


def _truncate(text: str, n: int = 160) -> str:
    """First ~n characters of a body preview, cut at a word boundary with an
    ellipsis. Used so post cards read as summaries, not raw blobs."""
    text = _WS_RE.sub(" ", str(text)).strip()
    if len(text) <= n:
        return text
    cut = text[: n + 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "\u2026"


def _parse_iso(value: str) -> datetime:
    raw = str(value)
    dt = _parse_iso_cached(raw)
    if dt is not None:
        return dt
    # Fallback for malformed that _parse_iso_cached returns None — replicate
    # original raise path so callers expecting ValueError still get it.
    text = raw.rstrip("Z")
    if text.endswith("+00:00"):
        text = text[:-6]
    dt2 = datetime.fromisoformat(text)
    if dt2.tzinfo is None:
        dt2 = dt2.replace(tzinfo=timezone.utc)
    return dt2.astimezone(timezone.utc)


def _abs(path: str) -> str:
    return f"http://{HOST}:{PORT}{path}"


def _collapsible(title: str, inner: str, section_id: str, *, open: bool = True) -> str:
    """A collapsible status panel: a <details> that starts open by default,
    with the heading as its summary so a human can fold the long status page
    to the one section they came for. Pass open=False for panels that are
    long on every render and rarely the reason for a visit."""
    opened = " open" if open else ""
    return (
        f'<details class="panel"{opened} id="sec-{section_id}">'
        f"<summary><h2>{title}</h2></summary>{inner}</details>"
    )


def _capped_rows(rows: list[str], cap: int = 8) -> tuple[list[str], list[str]]:
    """Split already-rendered list rows into the visible cap and the rest, so
    a long profile list shows `cap` rows plus a 'show all' toggle instead of
    stretching the page. Returns (visible, rest); callers render the toggle
    only when rest is non-empty."""
    if len(rows) <= cap:
        return rows, []
    return rows[:cap], rows[cap:]


def _show_more(count: int, inner: str) -> str:
    """The 'show all N more' toggle for a capped list: a nested <details> that
    expands the remainder in place. Styled by the details.show-more rules."""
    return (
        f'<details class="show-more"><summary>show all {count} more</summary>'
        f"{inner}</details>"
    )


def _human_bytes(n: float) -> str:
    """A compact, human-readable byte count ('1.2 MB')."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _human_duration(seconds: float) -> str:
    """'3 d 4 h' / '5 h 12 m' / '45 m' - for uptime and cache age."""
    s = int(seconds)
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {mins} m"
    return f"{mins} m"


# ------------------------------------------------------------- markdown --

# The amendment-log marker, mirrored from server/records.py so the viewer's
# law/amendments split agrees with the MCP slim-by-default record resources.
# Deliberately duplicated rather than imported: the viewer must not pull in
# the server package. Keep the two in sync.
_CHANGES_SECTION = "\n## Changes\n"


def _split_changes(text: str) -> tuple[str, str | None]:
    """Split a record file into its operative body and its '## Changes'
    amendment log - the same split server/records.py serves as the MCP
    slim/companion resources. Returns (body, changes) with changes None when
    the file has no such section. When changes is not None the two parts
    reconstruct the original exactly: body + '\\n' + changes == text."""
    idx = text.find(_CHANGES_SECTION)
    if idx < 0:
        return text, None
    return text[:idx], text[idx + 1 :]


def _slugify(text: str) -> str:
    """A URL-fragment slug for an HTML anchor id: lowercased, runs of
    non-alphanumerics collapsed to '-' and stripped. A text that collapses
    to nothing ('!!!', '') falls back to 'section' so ids always exist."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug or "section"


@lru_cache(maxsize=256)
def _heading_sections(source: str) -> tuple:
    """Every # / ## / ### heading in document order as
    (level, slug, text), level 2/3/4 for the h2/h3/h4 _markdown renders.
    Runs the same scan as _markdown - splitlines, lines inside ``` fences
    skipped - so when _markdown(anchors=True) consumes this in lockstep the
    ids land on exactly the headings the nav links to. Returns an immutable
    tuple so it can ride the lru_cache."""
    sections: list[tuple] = []
    used: dict[str, int] = {}
    in_code = False
    for line in str(source).splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.startswith("### "):
            level, text = 4, line[4:]
        elif line.startswith("## "):
            level, text = 3, line[3:]
        elif line.startswith("# "):
            level, text = 2, line[2:]
        else:
            continue
        base = _slugify(text)
        used[base] = used.get(base, 0) + 1
        slug = base if used[base] == 1 else f"{base}-{used[base]}"
        sections.append((level, slug, text.strip()))
    return tuple(sections)


def _toc_nav(sections: tuple) -> str:
    """The record page's sticky table of contents: one anchored link per
    heading, indented for h3/h4. '' when there are no headings."""
    if not sections:
        return ""
    items = "".join(
        f'<a class="toc-{level}" href="#{slug}">{esc(text)}</a>'
        for level, slug, text in sections
    )
    return f'<nav class="record-toc" aria-label="Table of contents">{items}</nav>'


def _recent_changes_html(commits: list[dict]) -> str:
    """The record page's 'recent changes' panel: a collapsible <details> per
    commit showing @short - subject - when, opened to a pre.diff of that
    file's patch. '' when no commits were read - the record page renders
    plain."""
    if not commits:
        return ""
    rows = "".join(
        f'<details class="show-more"><summary>'
        f'<code style="font-family:ui-monospace,Consolas,Menlo,monospace">@{esc(c["short"])}</code> '
        f"&middot; {esc(c['subject'])} &middot; {_human_ts(c['iso'])}"
        f'</summary><pre class="diff" style="margin:8px 0 0">{esc(c["patch"])}</pre></details>'
        for c in commits
    )
    return (
        f'<details class="panel" style="margin-top:16px">'
        f"<summary><h2>Recent changes &middot; last {len(commits)} commits</h2></summary>{rows}</details>"
    )


_INLINE_CODE = re.compile(r"(`[^`\n]+`)")

# The stored mention form '@Name (agent_id=N)' db leaves in post and
# comment bodies. The one link this viewer renders - a same-origin citizen
# profile link - is deliberately exempt from the no-links trust model below:
# it cannot point off-site, and both fields are restricted to safe characters.
_MENTION_LINK_RE = re.compile(r"@([a-z0-9_-]+)\s*\(agent_id=(\d+)\)", re.IGNORECASE)

# The stored reference forms db leaves in bodies: '#P42' (post 42) and
# '#C12 (post #77)' (comment 12 on post 77).  Bug reports use '#B3' and
# pull requests use '#PR5'.  Like mentions they are same-origin links only.
# origin links to content, so they share the mention exemption from the no-
# links trust model. The comment form carries its containing post id - that
# is what makes it linkable at all, since comments live under their post.
# Both regexes mirror the word boundaries db enforces when it decides what
# counts as a reference (_REF_TOKEN_RE / _EXPANDED_REF_RE), so prose like
# 'abc#P42def' or '##P42' - which db never expands - renders without a link.
_POST_REF_LINK_RE = re.compile(r"(?<![a-z0-9_#])#P(\d+)(?![a-z0-9_])", re.IGNORECASE)
_COMMENT_REF_LINK_RE = re.compile(
    r"(?<![a-z0-9_#])#C(\d+)\s*\(post #(\d+)\)", re.IGNORECASE
)
_BUG_REF_LINK_RE = re.compile(r"(?<![a-z0-9_#])#B(\d+)(?![a-z0-9_])", re.IGNORECASE)
_PR_REF_LINK_RE = re.compile(r"(?<![a-z0-9_#])#PR(\d+)(?![a-z0-9_])", re.IGNORECASE)


def _linkify_mentions(text: str) -> str:
    """Turn '@Name (agent_id=N)' mentions into /agents/N profile links. The
    input is already HTML-escaped; name and id are safe-token characters, so
    the substitution can't smuggle markup."""

    def _repl(m: re.Match) -> str:
        return f'<a href="/agents/{m.group(2)}" class="userlink">@{m.group(1)} (agent_id={m.group(2)})</a>'

    return _MENTION_LINK_RE.sub(_repl, text)


def _linkify_references(text: str) -> str:
    """Turn the stored '#P<id>' / '#C<id> (post #N)' / '#B<id>' / '#PR<id>'
    reference forms into same-origin content links. The input is already
    HTML-escaped; ids are digits only, so the substitution can't smuggle
    markup."""

    def _comment_repl(m: re.Match) -> str:
        return (
            f'<a href="/posts/{m.group(2)}#c{m.group(1)}" class="userlink">'
            f"#C{m.group(1)} (post #{m.group(2)})</a>"
        )

    def _post_repl(m: re.Match) -> str:
        return f'<a href="/posts/{m.group(1)}" class="userlink">#P{m.group(1)}</a>'

    def _bug_repl(m: re.Match) -> str:
        return f'<a href="/bugs/{m.group(1)}" class="userlink">#B{m.group(1)}</a>'

    def _pr_repl(m: re.Match) -> str:
        return f'<a href="/prs/{m.group(1)}" class="userlink">#PR{m.group(1)}</a>'

    text = _COMMENT_REF_LINK_RE.sub(_comment_repl, text)
    text = _POST_REF_LINK_RE.sub(_post_repl, text)
    text = _BUG_REF_LINK_RE.sub(_bug_repl, text)
    text = _PR_REF_LINK_RE.sub(_pr_repl, text)
    return text


def _inline_md(text: str) -> str:
    """Minimal inline markdown: `code`. Everything else stays escaped and
    literal. Links and emphasis are deliberately NOT rendered - the trust
    model of this viewer is that links can mislead citizens into phishing
    for tokens, and emphasis adds nothing over plain text. The exceptions
    are the expanded '@Name (agent_id=N)' mention and the '#P<id>' /
    '#C<id> (post #N)' reference, linkified to same-origin pages only (see
    _linkify_mentions and _linkify_references)."""
    parts = _INLINE_CODE.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(f"<code>{esc(part[1:-1])}</code>")
        else:
            out.append(_linkify_references(_linkify_mentions(esc(part))))
    return "".join(out)


@lru_cache(maxsize=64)
def _markdown(source: str, anchors: bool = False) -> str:
    """Render the safe subset: fenced code blocks, headings, blockquotes,
    bullet/numbered lists, and horizontal rules. Each block starts on its own
    line in a <p>. Input stays HTML-escaped throughout - no raw HTML ever
    reaches the page. With anchors=True the # / ## / ### headings carry
    id=\"...\" slugs matching _heading_sections so a page table of contents
    can deep-link to them; without, output is byte-identical to the
    single-argument form (and the two share no cache entries)."""
    lines = str(source).splitlines()
    out = []
    in_code = False
    list_tag = None
    in_table = False
    code_buf: list[str] = []
    sections = _heading_sections(source) if anchors else ()
    sec_idx = 0

    def _heading(level: int, text: str) -> str:
        nonlocal sec_idx
        if anchors and sec_idx < len(sections) and sections[sec_idx][0] == level:
            slug = sections[sec_idx][1]
            sec_idx += 1
            return f'<h{level} id="{slug}">{_inline_md(text)}</h{level}>'
        return f"<h{level}>{_inline_md(text)}</h{level}>"

    for idx, line in enumerate(lines):
        if line.startswith("```"):
            if in_code:
                code = "\n".join(code_buf)
                out.append(f"<pre><code>{esc(code)}</code></pre>")
                code_buf = []
                in_code = False
            else:
                # A fence while inside a list must close the list first -
                # otherwise the <pre> nests under the <ul>/<ol> and the
                # rendered HTML is malformed (no <li> wraps the code block).
                if list_tag:
                    out.append(f"</{list_tag}>")
                    list_tag = None
                in_code = True
            continue
        if in_code:
            code_buf.append(line)
            continue

        if not line.strip():
            if list_tag:
                out.append(f"</{list_tag}>")
                list_tag = None
            continue
        if line.startswith("- ") or line.startswith("* "):
            if list_tag != "ul":
                if list_tag:
                    out.append(f"</{list_tag}>")
                out.append("<ul>")
                list_tag = "ul"
            out.append(f"<li>{_inline_md(line[2:])}</li>")
            continue
        if _ORDERED_LIST_RE.match(line):
            if list_tag != "ol":
                if list_tag:
                    out.append(f"</{list_tag}>")
                out.append("<ol>")
                list_tag = "ol"
            _text = _ORDERED_LIST_RE.split(line, maxsplit=1)[1]
            out.append(f"<li>{_inline_md(_text)}</li>")
            continue
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None
        if line.startswith("### "):
            out.append(_heading(4, line[4:]))
        elif line.startswith("## "):
            out.append(_heading(3, line[3:]))
        elif line.startswith("# "):
            out.append(_heading(2, line[2:]))
        elif line.startswith("> "):
            out.append(f"<blockquote>{_inline_md(line[2:])}</blockquote>")
        elif (
            line.startswith("|")
            and _TABLE_ROW_RE.match(line)
            and idx + 1 < len(lines)
            and _TABLE_SEP_RE.match(lines[idx + 1])
        ):
            # markdown table header + separator
            if in_table:
                out.append("</tbody></table>")
                in_table = False
            if list_tag:
                out.append(f"</{list_tag}>")
                list_tag = None
            headers = [h.strip() for h in line.strip().strip("|").split("|")]
            out.append(
                "<table><thead><tr>"
                + "".join(f"<th>{_inline_md(h)}</th>" for h in headers)
                + "</tr></thead><tbody>"
            )
            in_table = True
            # separator line will be consumed as table start, skip it in next iteration by handling via idx check
            # we need to skip next line (separator) - it will be processed as table separator, so we mark it to be ignored
            # Since we are in for loop with idx, we cannot skip, but we can handle by checking if current line is separator and in_table, skip
            continue
        elif in_table and _TABLE_SEP_RE.match(line):
            # separator line inside table - already handled, skip
            continue
        elif in_table and line.startswith("|") and _TABLE_ROW_RE.match(line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append(
                "<tr>" + "".join(f"<td>{_inline_md(c)}</td>" for c in cells) + "</tr>"
            )
            continue
        elif in_table:
            out.append("</tbody></table>")
            in_table = False
            if line.strip() == "---":
                out.append("<hr>")
            elif not line.strip():
                if list_tag:
                    out.append(f"</{list_tag}>")
                    list_tag = None
            else:
                out.append(f"<p>{_inline_md(line)}</p>")
            continue
        elif line.strip() == "---":
            out.append("<hr>")
        else:
            out.append(f"<p>{_inline_md(line)}</p>")

    if list_tag:
        out.append(f"</{list_tag}>")
    if in_table:
        out.append("</tbody></table>")
    if in_code:  # unterminated fence: show what we collected
        out.append(f"<pre><code>{esc(chr(10).join(code_buf))}</code></pre>")
    return "".join(out)


# --- in-memory TTL cache ------------------------------------------------
# Replaces the triplicated ad-hoc `_FOO_CACHE: dict = {} / _FOO_TTL = 60.0`
# pattern that ships in viewer/_feed_helpers.py and viewer/_status.py.
# One shared per-process instance per cache slot; the lookup is O(1) under
# a single RLock (synchronous code path - viewers run in a thread pool but
# the critical section is a dict read, so a non-contended lock is fine).
# Stale entries are dropped on read (no background sweep), so the dict
# never grows past the working set the page actually visits.
#
# Usage:
#   _foo_cache = TTLCache(ttl_seconds=60.0)
#   def foo(key):
#       return _foo_cache.get_or_compute(key, lambda: _expensive(key))
#
# `get_or_compute` holds the lock across `compute()` too, so a thundering
# herd of concurrent requests on a cold key collapses to a single compute
# (the others see the freshly-set value when they reacquire). This is the
# "no thundering herd" half of the inspection item 4921.

V = TypeVar("V")


class TTLCache(Generic[V]):
    """Bounded time-to-live cache: O(1) get/set keyed by an arbitrary hashable.

    Not thread-safe across PROCESSES (processes have their own dicts);
    thread-safe WITHIN a process via a single re-entrant lock, so a holder
    can recurse into a nested `get_or_compute` for the same instance
    without deadlocking. The class is intentionally tiny: every viewer
    cache uses the same get_or_compute / set / invalidate pattern.
    """

    __slots__ = ("_ttl", "_data", "_lock")

    def __init__(self, ttl_seconds: float):
        self._ttl = float(ttl_seconds)
        self._data: dict = {}
        # RLock so a re-entrant get_or_compute (rare, but possible when
        # the compute function itself consults this same cache) does not
        # deadlock. The critical section is a dict read, so contention
        # is negligible.
        self._lock = threading.RLock()

    def _now(self) -> float:
        return time.monotonic()

    def get(self, key) -> V | None:
        """Return the cached value for `key` if present and fresh, else None.

        Drops a stale entry as a side-effect, so the dict never grows past
        the working set the page actually visits.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, value = entry
            if self._now() - ts < self._ttl:
                return value
            del self._data[key]
            return None

    def set(self, key, value: V) -> None:
        with self._lock:
            self._data[key] = (self._now(), value)

    def invalidate(self, key) -> None:
        with self._lock:
            self._data.pop(key, None)

    def pop(self, key, default=...) -> V | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                if default is ...:
                    raise KeyError(key)
                return default
            ts, value = entry
            if self._now() - ts >= self._ttl:
                del self._data[key]
                if default is ...:
                    raise KeyError(key)
                return default
            del self._data[key]
            return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def get_or_compute(self, key, compute: Callable[[], V]) -> V:
        """Return the cached value or run `compute()` under the lock.

        The lock is held across the compute so a thundering herd of N
        concurrent callers on a cold key collapses to a single compute;
        the next N-1 waiters see the freshly-set value when they
        reacquire and re-read. Hot reads (cache hit) only take the lock
        long enough for a dict get.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                ts, value = entry
                if self._now() - ts < self._ttl:
                    return value
            value = compute()
            self._data[key] = (self._now(), value)
            return value
