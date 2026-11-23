"""Bounded, lossless text spans for reviewing a proposed workspace replacement."""

from bisect import bisect_left
from difflib import SequenceMatcher
from itertools import accumulate
import json


MAX_HUNKS = 16
MAX_HUNK_BYTES = 6000  # Includes JSON escaping and offsets for all displayed hunks.


def _trim_edges(old, new):
    """Return the common prefix length and exclusive ends of the differing substrings."""
    start = 0
    end_old, end_new = len(old), len(new)
    while start < min(end_old, end_new) and old[start] == new[start]:
        start += 1
    while min(end_old, end_new) > start and old[end_old - 1] == new[end_new - 1]:
        end_old -= 1
        end_new -= 1
    return start, end_old, end_new


def _matching_lines(old, new):
    # Detailed alignment is useful on modest edits. Large, repetitive logs can
    # make SequenceMatcher quadratic; use ordered equal-line anchors there.
    if len(old) * len(new) <= 250_000:
        yield from SequenceMatcher(None, old, new, autojunk=False).get_matching_blocks()
        return
    positions = {}
    for i, line in enumerate(old):
        positions.setdefault(line, []).append(i)
    next_old = 0
    for j, line in enumerate(new):
        candidates = positions.get(line, ())
        index = bisect_left(candidates, next_old)
        if index < len(candidates):
            i = candidates[index]
            yield i, j, 1
            next_old = i + 1
    yield len(old), len(new), 0


def _changed_spans(old, new):
    prefix, end_old, end_new = _trim_edges(old, new)
    before = old[prefix:end_old].splitlines(keepends=True)
    after = new[prefix:end_new].splitlines(keepends=True)
    offsets_old = list(accumulate(map(len, before), initial=prefix))
    offsets_new = list(accumulate(map(len, after), initial=prefix))
    oi = ni = 0
    for i, j, count in _matching_lines(before, after):
        if oi != i or ni != j:
            removed = old[offsets_old[oi]:offsets_old[i]]
            added = new[offsets_new[ni]:offsets_new[j]]
            # Even a long single-line paragraph can have a one-word edit.
            start, stop_old, stop_new = _trim_edges(removed, added)
            yield {"old_offset": offsets_old[oi] + start, "new_offset": offsets_new[ni] + start,
                   "removed": removed[start:stop_old], "added": added[start:stop_new]}
        oi, ni = i + count, j + count


def edit_summary(old, new):
    if old is None:
        return {"status": "create", "complete": True, "added_from": "arguments.text"}
    if old == new:
        return {"status": "unchanged", "complete": True, "hunks": []}

    hunks = []
    budget = MAX_HUNK_BYTES
    omitted = 0
    for hunk in _changed_spans(old, new):
        size = len(json.dumps(hunk, ensure_ascii=True)) + 2
        if size > budget or len(hunks) >= MAX_HUNKS:
            omitted += 1
        else:
            hunks.append(hunk)
            budget -= size
    result = {"status": "replace", "complete": omitted == 0, "hunks": hunks}
    if omitted:
        result.update(omitted_hunks=omitted, reason="Edit exceeds the review excerpt limit. See the full proposal in arguments.text.")
    return result
