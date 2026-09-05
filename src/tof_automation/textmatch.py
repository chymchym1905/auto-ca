"""Fuzzy text matching over OCR results.

OCR of game UI is imperfect (``chymchyml 905``, ``ritiC91 Abyss``), so all text
lookups are fuzzy and operate at the token/phrase level rather than on the whole
frame. Two jobs:

* classify a screen by signature phrases (:func:`find_phrase` + a threshold);
* locate a button/row label to click, returning a bounding box center.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .ocr import OcrResult, Word

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def normalize(s: str) -> str:
    return _NON_ALNUM.sub("", s.lower())


@dataclass(frozen=True)
class TextMatch:
    text: str
    score: float
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


def _merge_box(words: tuple[Word, ...]) -> tuple[int, int, int, int]:
    left = min(w.x for w in words)
    top = min(w.y for w in words)
    right = max(w.x + w.w for w in words)
    bottom = max(w.y + w.h for w in words)
    return left, top, right - left, bottom - top


def find_phrase(ocr: OcrResult, query: str, threshold: float = 0.7) -> TextMatch | None:
    """Return the best fuzzy match for ``query`` among consecutive words.

    Searches windows of adjacent words within each OCR line and scores the
    concatenated, normalized candidate against the normalized query. Returns the
    best match at or above ``threshold``, else ``None``.
    """
    nq = normalize(query)
    if not nq:
        return None
    # Allow the candidate to span a couple more words than the query has tokens.
    max_span = max(1, len(query.split()) + 1)

    best: tuple[Word, ...] | None = None
    best_score = 0.0
    best_text = ""
    for line in ocr.lines:
        ws = line.words
        for i in range(len(ws)):
            for j in range(i + 1, min(i + max_span, len(ws)) + 1):
                window = ws[i:j]
                cand = "".join(normalize(w.text) for w in window)
                if not cand:
                    continue
                score = SequenceMatcher(None, nq, cand).ratio()
                if score > best_score:
                    best_score = score
                    best = window
                    best_text = " ".join(w.text for w in window)

    if best is None or best_score < threshold:
        return None
    x, y, w, h = _merge_box(best)
    return TextMatch(text=best_text, score=best_score, x=x, y=y, w=w, h=h)


def has_phrase(ocr: OcrResult, query: str, threshold: float = 0.7) -> bool:
    return find_phrase(ocr, query, threshold) is not None


def _overlaps(a: TextMatch, b: TextMatch) -> bool:
    return not (a.x + a.w <= b.x or b.x + b.w <= a.x or
                a.y + a.h <= b.y or b.y + b.h <= a.y)


def find_all(ocr: OcrResult, query: str, threshold: float = 0.7) -> list[TextMatch]:
    """Return all non-overlapping fuzzy matches for ``query``, best score first.

    Used to find every instance of a repeated button label (e.g. each ``Request``
    / ``Approve`` row) so the caller can pick the one on a given name's row.
    """
    nq = normalize(query)
    if not nq:
        return []
    max_span = max(1, len(query.split()) + 1)

    candidates: list[TextMatch] = []
    for line in ocr.lines:
        ws = line.words
        for i in range(len(ws)):
            for j in range(i + 1, min(i + max_span, len(ws)) + 1):
                window = ws[i:j]
                cand = "".join(normalize(w.text) for w in window)
                if not cand:
                    continue
                score = SequenceMatcher(None, nq, cand).ratio()
                if score >= threshold:
                    x, y, w, h = _merge_box(window)
                    candidates.append(
                        TextMatch(" ".join(w.text for w in window), score, x, y, w, h)
                    )

    candidates.sort(key=lambda m: m.score, reverse=True)
    kept: list[TextMatch] = []
    for c in candidates:
        if not any(_overlaps(c, k) for k in kept):
            kept.append(c)
    return kept
