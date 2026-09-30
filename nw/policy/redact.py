"""Tokenise sensitive fields before anything is indexed or sent to a model.

The rule is simple: an identifier that could name a person or an account is
replaced by a stable token before it reaches an index, a prompt, or a log.
Stable means the same value maps to the same token within a document, so the
model can still say "the customer at ACCOUNT_1 asked twice" without ever
seeing the identifier.

The patterns cover what a regular expression can find reliably: email, account and
invoice ids, cards, IBANs, phone numbers in international and national formats
(German and North American), IP addresses and API keys. Names and street addresses
need a detector: `NW_REDACT_DETECTOR=heuristic` turns on a small rule based one that
ships here (honorifics, greetings and sign-offs, street and postcode shapes), and
`presidio` uses Microsoft Presidio when it is installed. Neither is on by default:
a detector costs recall on nothing when the text has no names, and precision always.

How good is it? `python -m nw.policy.redact --eval data/pii/messages.jsonl` scores the
redactor against labelled spans, recall and precision per kind. Recall is the number
that matters for privacy: a missed email is a leak, a false positive is a token.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    # An IBAN before anything numeric: its digit groups would otherwise read as a card.
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,3})?\b")),
    # `#NW-88214` and `invoice NW-88214` are invoices, not accounts: an invoice id names no one.
    # A pattern with a group named `v` redacts only that group; the rest is context.
    (
        "INVOICE",
        re.compile(
            r"(?:#|\b[Ii]nvoice(?: number| no\.?| id)?(?: is)?:? |\bRechnung(?:snummer)?:? )"
            r"(?P<v>NW-\d{4,})\b"
        ),
    ),
    ("ACCOUNT", re.compile(r"(?<![#\w-])NW-\d{5,}\b")),
    ("INVOICE", re.compile(r"\bINV-?\d{4,}\b")),
    # A digit run after a decimal point is a float's fraction (a cost in a JSON line), not a card.
    ("CARD", re.compile(r"(?<![\d.])\b(?:\d[ -]?){13,19}\b")),
    # International: a country code prefix. National: a leading 0 area code (Germany, the UK)
    # or the North American (555) 123-4567 and 555-123-4567 shapes. A bare digit pattern
    # would also match ISO dates, so every branch needs its anchor.
    ("PHONE", re.compile(r"\+\d[\d ()/-]{8,}\d")),
    (
        "PHONE",
        re.compile(r"(?<![\w.+/-])(?:\(0\d{2,5}\)|0\d{2,5})[ /-]?\d{3,4}(?:[ -]?\d{2,5}){0,2}\b"),
    ),
    ("PHONE", re.compile(r"(?<![\w.])(?:\(\d{3}\)[ ]?|\b\d{3}-)\d{3}-\d{4}\b")),
    # A local North American number (555-0123): seven digits, one dash, nothing either side.
    ("PHONE", re.compile(r"(?<![\w.-])\d{3}-\d{4}\b(?!-)")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # IPv6, full or compressed with `::`; a time like 10:30 has neither shape.
    (
        "IP",
        re.compile(
            r"(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
            r"|(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,5})?)(?![\w:])"
        ),
    ),
    ("KEY", re.compile(r"\b(?:sk|pk|rk|nw|key)[_-](?:(?:live|test)[_-])?[A-Za-z0-9]{16,}\b")),
]

KINDS: tuple[str, ...] = tuple(dict.fromkeys(k for k, _ in PATTERNS)) + ("NAME", "ADDRESS")


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end


Detector = Callable[[str], list[Span]]


@dataclass
class Redaction:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.mapping)


# ----- optional detectors for names and addresses --------------------------------------

_CAP = r"[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?"
_NAME_AFTER = re.compile(
    rf"(?:\b(?:Mr|Mrs|Ms|Dr|Prof|Herr|Frau)\.?[ ]+|\b(?:[Mm]y name is|[Ii]ch bin|[Tt]his is)[ ]+)"
    rf"(?P<name>{_CAP}(?:[ ]+{_CAP}){{0,2}})"
)
_SIGNOFF = re.compile(
    rf"(?:Regards|Best|Thanks|Cheers|Grüße|Gruß|Viele Grüße|Mit freundlichen Grüßen),?\s*\n\s*"
    rf"(?P<name>{_CAP}\s+{_CAP})"
)
_STREET_EN = re.compile(
    rf"\b\d{{1,5}}[ ]+{_CAP}(?:[ ]+{_CAP})?[ ]+"
    rf"(?:Street|St\.|Avenue|Ave\.|Road|Rd\.|Lane|Boulevard|Blvd\.|Drive|Dr\.|Way)"
)
_STREET_DE = re.compile(
    r"\b[A-ZÄÖÜ][a-zäöüß-]*(?:straße|strasse|str\.|weg|platz|allee|gasse|ring|damm)"
    r"[ ]+\d{1,4}[a-z]?\b"
)
_POSTCODE_DE = re.compile(rf"\b\d{{5}}[ ]+{_CAP}\b")


# A gazetteer of common given names (English, German, Nordic, Romance, South Asian, Arabic,
# East African). A given name followed by a capitalised word is read as a person.
FIRST_NAMES = frozenset(
    """Aaron Adam Adrian Ahmed Aisha Alex Alexander Ali Alice Amara Amelia Amir Ana Andrea
    Andreas Anja Anke Anna Anne Arjun Astrid Ben Benjamin Bernd Birgit Birte Bjorn Caleb Carla
    Carlos Caroline Charlotte Chloe Christian Christina Claire Clara Daniel David Dev Diana
    Dieter Elena Elias Elif Elin Ella Emil Emily Emma Eric Erik Eva Fatima Felix Finn Florian
    Frank Frida Gabriel George Grace Greta Hana Hannah Hans Harry Heike Helen Henrik Henry Ian
    Imani Ines Isabel Isak Ivan Jack Jacob Jakob James Jan Jana Jens Joanna Johan Johanna John
    Jonah Jonas Jorge Jose Julia Julian Jurgen Kai Karen Karin Karl Kate Katrin Kevin Kirsten
    Klaus Lara Laura Lea Leah Lena Leon Liam Lina Linnea Lisa Lotte Louis Lucas Lucia Luis Lukas
    Maja Mara Marco Maria Marie Mark Marcus Markus Martin Mateo Matthias Max Maya Mehmet Mia
    Michael Michelle Mika Mohammed Monika Nadia Nils Nina Noah Nora Olaf Oliver Olivia Omar Oscar
    Owen Paul Peter Petra Philipp Pia Priya Rahul Rana Raphael Rebecca Robert Rohan Rosa Ruth
    Sabine Sam Samuel Sara Sarah Sebastian Simon Sofia Sophie Stefan Steffen Susanne Sven Tanja
    Tariq Theo Thomas Tim Tobias Tom Ulrike Uwe Vera Victor Wei Wolfgang Yara Yusuf Zara""".split()
)
_NOT_SURNAME = frozenset(
    "Team Support Portal Admin Console Plan Cloud Update Release API SDK Group Office".split()
)
# Roles that follow a sign-off where a name would: "Regards, Support Engineer".
_ROLES = frozenset(
    """Engineer Manager Lead Team Support Operations Product Analyst Developer Director Officer
    Specialist Notifications Admin Administrator Datenanalystin Entwickler Entwicklerin
    Support-Ingenieur Leiter Leiterin Team-Lead""".split()
)
_GIVEN = re.compile(r"\b([A-ZÄÖÜ][a-zäöüß]+)\s+([A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?)\b")


def heuristic_detector(text: str) -> list[Span]:
    """Names after an honorific, a self-introduction or a sign-off, a common given name
    followed by a capitalised word, and street addresses in English and German shapes with a
    German postcode and town. Rules and a gazetteer, not a model: good enough to measure and
    to teach, not a substitute for a trained recogniser."""
    spans: list[Span] = []
    for m in _GIVEN.finditer(text):
        if m.group(1) in FIRST_NAMES and m.group(2) not in _NOT_SURNAME:
            spans.append(Span(m.start(), m.end(), "NAME"))
    for pattern in (_NAME_AFTER, _SIGNOFF):
        for m in pattern.finditer(text):
            if not set(m.group("name").split()) & _ROLES:
                spans.append(Span(m.start("name"), m.end("name"), "NAME"))
    for pattern in (_STREET_EN, _STREET_DE, _POSTCODE_DE):
        for m in pattern.finditer(text):
            spans.append(Span(m.start(), m.end(), "ADDRESS"))
    return spans


def presidio_detector() -> Detector:
    """Microsoft Presidio's analyzer for PERSON and LOCATION entities. Optional: install
    `presidio-analyzer` and a spaCy model; nothing in the course requires it."""
    from presidio_analyzer import AnalyzerEngine  # type: ignore[import-not-found]

    engine = AnalyzerEngine()
    kinds = {"PERSON": "NAME", "LOCATION": "ADDRESS"}

    def detect(text: str) -> list[Span]:
        found = engine.analyze(text=text, entities=list(kinds), language="en")
        return [Span(r.start, r.end, kinds[r.entity_type]) for r in found]

    return detect


_detector_cache: dict[str, Detector | None] = {}


def detector_from_env(env: dict[str, str] | None = None) -> Detector | None:
    """`NW_REDACT_DETECTOR`: unset or `none` for the patterns only, `heuristic`, `presidio`."""
    name = ((env if env is not None else os.environ).get("NW_REDACT_DETECTOR") or "none").lower()
    if name not in _detector_cache:
        if name == "heuristic":
            _detector_cache[name] = heuristic_detector
        elif name == "presidio":
            _detector_cache[name] = presidio_detector()
        elif name in {"none", "off", ""}:
            _detector_cache[name] = None
        else:
            raise ValueError(f"NW_REDACT_DETECTOR={name!r}: expected none, heuristic or presidio")
    return _detector_cache[name]


# ----- detection and redaction ---------------------------------------------------------


def detect(
    text: str, *, keep: Collection[str] = (), detector: Detector | None | bool = True
) -> list[Span]:
    """Every sensitive span, first pattern wins on overlap, left to right. `keep` names kinds
    that stay in the text (the agent keeps ACCOUNT: its tools are bound to it). `detector`
    True means the one from the environment, None or False means patterns only."""
    if detector is True:
        detector = detector_from_env()
    found: list[Span] = []
    for kind, pattern in PATTERNS:
        grouped = "v" in pattern.groupindex
        for m in pattern.finditer(text):
            span = (
                Span(m.start("v"), m.end("v"), kind) if grouped else Span(m.start(), m.end(), kind)
            )
            if not any(span.overlaps(s) for s in found):
                found.append(span)
    if detector:
        for span in detector(text):
            if not any(span.overlaps(s) for s in found):
                found.append(span)
    return sorted((s for s in found if s.kind not in keep), key=lambda s: s.start)


def redact(
    text: str, *, keep: Collection[str] = (), detector: Detector | None | bool = True
) -> Redaction:
    """Replace every sensitive match with a stable token like `[EMAIL_1]`."""
    # SOLUTION BEGIN
    mapping: dict[str, str] = {}
    counters: dict[str, int] = {}

    def token_for(kind: str, value: str) -> str:
        if value not in mapping:
            counters[kind] = counters.get(kind, 0) + 1
            mapping[value] = f"[{kind}_{counters[kind]}]"
        return mapping[value]

    parts: list[str] = []
    cursor = 0
    for span in detect(text, keep=keep, detector=detector):
        parts.append(text[cursor : span.start])
        parts.append(token_for(span.kind, text[span.start : span.end]))
        cursor = span.end
    parts.append(text[cursor:])
    return Redaction(text="".join(parts), mapping=mapping)
    # STUB: return Redaction(text=text)  # the redaction step: nothing is redacted yet
    # SOLUTION END


AGENT_KEEP = ("ACCOUNT", "INVOICE")


def redact_for_agent(text: str) -> str:
    """The agent path's redaction: everything personal goes, the account and invoice ids stay,
    because the tools are bound to the run's account and an invoice id names no one."""
    return redact(text, keep=AGENT_KEEP).text


def redact_value(value: Any, *, keep: Collection[str] = ()) -> Any:
    """Redact every string inside a JSON-like value (dicts, lists, strings), keys untouched."""
    if isinstance(value, str):
        return redact(value, keep=keep).text if value else value
    if isinstance(value, dict):
        return {k: redact_value(v, keep=keep) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(v, keep=keep) for v in value]
    return value


def redact_fields(record: dict[str, Any], *names: str) -> dict[str, Any]:
    """A copy of `record` with the named string fields redacted. Every capture file the
    services write and the feedback log go through this before a line hits disk, so a
    backtest file never holds a raw email, phone number or account id."""
    out = dict(record)
    for name in names:
        value = out.get(name)
        if isinstance(value, str) and value:
            out[name] = redact(value).text
    return out


# ----- measurement ---------------------------------------------------------------------


def load_labelled(path: Path) -> list[dict[str, Any]]:
    """Rows of `{"text": ..., "spans": [{"start", "end", "label"}]}`, one JSON object a line.
    `label` uses the kinds above; `kind` is accepted as a synonym."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _texts(rows: Iterable[dict[str, Any]]) -> Iterable[tuple[str, list[dict[str, Any]]]]:
    """Each labelled text with its spans: `text` with `spans` (only those whose `field` is
    `text` when a field is given), and `subject` with `subject_spans` when present."""
    for row in rows:
        spans = [s for s in row.get("spans", []) if s.get("field", "text") == "text"]
        yield row["text"], spans
        if row.get("subject"):
            yield row["subject"], list(row.get("subject_spans") or [])


def evaluate(
    rows: Iterable[dict[str, Any]], *, detector: Detector | None | bool = True
) -> dict[str, Any]:
    """Span recall and precision, overall and per kind. A gold span counts as found when a
    predicted span of any kind overlaps it (a leak is about the text, not the label); a
    predicted span is correct when it overlaps a gold span."""
    per: dict[str, dict[str, int]] = {}
    tp_gold = n_gold = tp_pred = n_pred = 0

    def bump(kind: str, key: str) -> None:
        per.setdefault(kind, {"gold": 0, "found": 0, "predicted": 0, "correct": 0})[key] += 1

    for text, spans in _texts(rows):
        gold = [
            Span(int(s["start"]), int(s["end"]), str(s.get("label") or s.get("kind")).upper())
            for s in spans
        ]
        pred = detect(text, detector=detector)
        for g in gold:
            n_gold += 1
            bump(g.kind, "gold")
            if any(g.overlaps(p) for p in pred):
                tp_gold += 1
                bump(g.kind, "found")
        for p in pred:
            n_pred += 1
            bump(p.kind, "predicted")
            if any(p.overlaps(g) for g in gold):
                tp_pred += 1
                bump(p.kind, "correct")

    def ratio(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    return {
        "gold_spans": n_gold,
        "predicted_spans": n_pred,
        "recall": ratio(tp_gold, n_gold),
        "precision": ratio(tp_pred, n_pred),
        "by_kind": {
            k: {
                **v,
                "recall": ratio(v["found"], v["gold"]),
                "precision": ratio(v["correct"], v["predicted"]),
            }
            for k, v in sorted(per.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Score the redactor against labelled PII spans.")
    ap.add_argument("--eval", type=Path, required=True, help="JSONL of text and gold spans")
    ap.add_argument(
        "--detector", default=None, help="none, heuristic or presidio (default: environment)"
    )
    args = ap.parse_args(argv)
    det: Detector | None | bool = True
    if args.detector is not None:
        det = detector_from_env({"NW_REDACT_DETECTOR": args.detector})
    print(json.dumps(evaluate(load_labelled(args.eval), detector=det), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
