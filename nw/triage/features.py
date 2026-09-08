"""The feature pipeline for ticket triage.

Reproducibility is the whole point of this module: the same ticket must produce
the same feature vector today, after a restart, and inside the container. So
every transform is a fitted scikit-learn object that is serialised with the
model, and nothing here reads global state.
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
from scipy.sparse import csr_matrix, hstack
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion, Pipeline

PRIORITIES = ["P0", "P1", "P2", "P3"]

_LOG_LINE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}T|\[?(ERROR|WARN|WARNING|INFO|Traceback)|at \w+\.)", re.M
)
_ERROR_WORDS = re.compile(
    r"\b(error|exception|failed|failure|timeout|refused|denied|crash|500|502|503|429)\b", re.I
)
_URGENT_WORDS = re.compile(
    r"\b(urgent|critical|asap|immediately|outage|down|breach|data loss|all users|entire"
    r"|production)\b",
    re.I,
)


def ticket_text(subject: str, body: str) -> str:
    """One string per ticket. The subject is repeated so short subjects still weigh."""
    subject = (subject or "").strip()
    body = (body or "").strip()
    return f"{subject}\n{subject}\n{body}"


class TextSelector(BaseEstimator, TransformerMixin):
    """Pick the text column out of a list of dicts."""

    def fit(self, X: list[dict[str, Any]], y: Any = None) -> TextSelector:
        return self

    def transform(self, X: list[dict[str, Any]]) -> list[str]:
        return [ticket_text(t.get("subject", ""), t.get("body", "")) for t in X]


class StructuralFeatures(BaseEstimator, TransformerMixin):
    """Hand-made signals a bag of words misses: length, shouting, pasted logs, error and
    urgency vocabulary density. All scaled to roughly [0, 1] so they sit next to TF-IDF."""

    names = [
        "log_chars",
        "log_words",
        "caps_ratio",
        "log_lines",
        "error_density",
        "urgent_density",
        "subject_missing",
        "exclamations",
    ]

    def fit(self, X: list[dict[str, Any]], y: Any = None) -> StructuralFeatures:
        return self

    def transform(self, X: list[dict[str, Any]]) -> csr_matrix:
        rows = []
        for t in X:
            subject = (t.get("subject") or "").strip()
            body = t.get("body") or ""
            words = max(1, len(body.split()))
            letters = [c for c in body if c.isalpha()]
            caps = sum(1 for c in letters if c.isupper()) / max(1, len(letters))
            rows.append(
                [
                    min(1.0, np.log1p(len(body)) / 8.0),
                    min(1.0, np.log1p(words) / 6.0),
                    caps,
                    min(1.0, len(_LOG_LINE.findall(body)) / 5.0),
                    min(1.0, len(_ERROR_WORDS.findall(body)) / words * 10),
                    min(1.0, len(_URGENT_WORDS.findall(subject + " " + body)) / words * 10),
                    1.0 if len(subject.split()) <= 1 else 0.0,
                    min(1.0, body.count("!") / 5.0),
                ]
            )
        return csr_matrix(np.asarray(rows, dtype=np.float32))

    def get_feature_names_out(self, input_features: Any = None) -> np.ndarray:
        return np.asarray(self.names)


def build_features() -> FeatureUnion:
    """Word and character TF-IDF over the text, plus structural signals."""
    text = Pipeline(
        [
            ("select", TextSelector()),
            (
                "union",
                FeatureUnion(
                    [
                        (
                            "word",
                            TfidfVectorizer(
                                ngram_range=(1, 2), min_df=2, max_features=60000, sublinear_tf=True
                            ),
                        ),
                        (
                            "char",
                            TfidfVectorizer(
                                analyzer="char_wb",
                                ngram_range=(3, 5),
                                min_df=3,
                                max_features=60000,
                                sublinear_tf=True,
                            ),
                        ),
                    ]
                ),
            ),
        ]
    )
    return FeatureUnion([("text", text), ("structural", StructuralFeatures())])


def stack(*blocks: csr_matrix) -> csr_matrix:
    return hstack(blocks).tocsr()
