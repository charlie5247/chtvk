"""Conservative, deterministic FAQ search without generated answers."""

import re
import sqlite3
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum

from faq_repository import FAQRepository


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass(frozen=True, slots=True)
class SearchSettings:
    high_threshold: float = 0.82
    medium_threshold: float = 0.58
    ambiguity_delta: float = 0.07
    max_alternatives: int = 3


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    faq: dict
    score: float
    match_type: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    faq: dict | None
    score: float
    confidence: Confidence
    match_type: str
    alternatives: list[SearchCandidate] = field(default_factory=list)


def normalize_text(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


class FAQSearch:
    def __init__(self, connection: sqlite3.Connection, settings: SearchSettings | None = None):
        self.repository = FAQRepository(connection)
        self.settings = settings or SearchSettings()

    @staticmethod
    def _candidate(query: str, faq: dict) -> SearchCandidate:
        question = normalize_text(faq["question"])
        aliases = [normalize_text(alias) for alias in faq["aliases"]]
        if query == question:
            return SearchCandidate(faq, 1.0, "exact_question")
        if query in aliases:
            return SearchCandidate(faq, 0.97, "exact_alias")

        variants = [question, *aliases]
        sequence_score = max(SequenceMatcher(None, query, value).ratio() for value in variants)
        query_tokens = set(query.split())
        token_scores = [len(query_tokens & set(value.split())) / max(len(query_tokens | set(value.split())), 1) for value in variants]
        close_score = 0.65 * sequence_score + 0.35 * max(token_scores)

        keywords = [normalize_text(value) for value in faq["keywords"]]
        matched = sum(1 for keyword in keywords if keyword in query or all(part in query_tokens for part in keyword.split()))
        keyword_score = 0.0
        if matched:
            keyword_score = min(0.76, 0.48 + 0.10 * matched)
        if close_score >= keyword_score:
            return SearchCandidate(faq, round(close_score, 4), "very_close" if close_score >= 0.58 else "uncertain")
        return SearchCandidate(faq, round(keyword_score, 4), "keywords")

    def search(self, text: str) -> SearchResult:
        query = normalize_text(text)
        if not query:
            return SearchResult(None, 0.0, Confidence.LOW, "not_found")
        candidates = sorted((self._candidate(query, faq) for faq in self.repository.active()), key=lambda item: (-item.score, -item.faq["priority"], item.faq["id"]))
        if not candidates:
            return SearchResult(None, 0.0, Confidence.LOW, "not_found")
        best = candidates[0]
        confidence = Confidence.HIGH if best.score >= self.settings.high_threshold else Confidence.MEDIUM if best.score >= self.settings.medium_threshold else Confidence.LOW
        close = [item for item in candidates[1:] if item.score >= self.settings.medium_threshold and best.score - item.score <= self.settings.ambiguity_delta]
        alternatives = ([best, *close][: self.settings.max_alternatives] if confidence is Confidence.MEDIUM or close else [])
        if alternatives:
            confidence = Confidence.MEDIUM
        return SearchResult(best.faq if confidence is not Confidence.LOW else None, best.score, confidence, best.match_type, alternatives)
