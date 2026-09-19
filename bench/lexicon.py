"""Answering the question without a model at all.

Everything in Fusion's semantic path is already deterministic except one step:
turning a sentence into a choice of measure, dimension and filter. This module
does that step with a lexicon instead of a language model.

It is worth having for two reasons beyond curiosity. It is the control the
benchmark otherwise lacks — an arm that scores *below* it is a model earning
nothing over string matching. And it is the configuration an on-premise
deployment can actually certify: no GPU, no weights, no sampling, the same
answer every time, and an audit trail that is a list of matched words rather
than a probability.

It shares the constrained arm's schema, so it is under exactly the same
restriction: it can only name measures, dimensions and values that exist. The
difference is how the choice is made, which is the variable under test.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

#: Turkish is written with characters the data does not always use — the
#: fixture stores ``basarili`` while a question says ``başarılı`` — so every
#: comparison happens on a folded form. ``ı`` and ``İ`` do not fold the way
#: ``str.lower`` and NFKD expect, hence the explicit pairs.
_FOLD = str.maketrans(
    {
        "ı": "i",
        "İ": "i",
        "ş": "s",
        "Ş": "s",
        "ğ": "g",
        "Ğ": "g",
        "ü": "u",
        "Ü": "u",
        "ö": "o",
        "Ö": "o",
        "ç": "c",
        "Ç": "c",
    }
)


def fold(text: str) -> str:
    """A comparable form: lower case, no diacritics, Turkish pairs handled."""
    folded = text.translate(_FOLD).lower()
    return "".join(c for c in unicodedata.normalize("NFKD", folded) if not unicodedata.combining(c))


#: Words that name an aggregation, most specific first — ``en yuksek`` has to
#: beat ``yuksek``, and ``kac farkli`` has to beat ``kac``.
AGGREGATIONS: tuple[tuple[str, str], ...] = (
    ("count_distinct", "kac farkli"),
    ("count_distinct", "farkli"),
    ("count_distinct", "distinct"),
    ("count_distinct", "unique"),
    ("count_distinct", "benzersiz"),
    ("max", "en yuksek"),
    ("max", "en buyuk"),
    ("max", "en fazla"),
    ("max", "highest"),
    ("max", "largest"),
    ("max", "maximum"),
    ("max", "max"),
    ("min", "en dusuk"),
    ("min", "en kucuk"),
    ("min", "en az"),
    ("min", "lowest"),
    ("min", "smallest"),
    ("min", "minimum"),
    ("min", "min"),
    ("median", "medyan"),
    ("median", "median"),
    ("median", "ortanca"),
    ("avg", "ortalama"),
    ("avg", "average"),
    ("avg", "mean"),
    ("avg", "avg"),
    ("sum", "toplam tutar"),
    ("sum", "total amount"),
    ("sum", "toplami"),
    ("sum", "toplam"),
    ("sum", "total"),
    ("sum", "sum"),
    ("count", "kac adet"),
    ("count", "kac tane"),
    ("count", "sayisi"),
    ("count", "sayi"),
    ("count", "adedi"),
    ("count", "kac"),
    ("count", "how many"),
    ("count", "number of"),
    ("count", "count"),
)

#: Words that name a transform.
TRANSFORMS: tuple[tuple[str, str], ...] = (
    ("change_pct", "yuzde degisim"),
    ("change_pct", "yuzdesel degisim"),
    ("change_pct", "percentage change"),
    ("change_pct", "percent change"),
    ("change_pct", "month-over-month"),
    ("change_pct", "onceki aya gore"),
    ("cumsum", "kumulatif"),
    ("cumsum", "cumulative"),
    ("cumsum", "running total"),
    ("cumsum", "birikimli"),
    ("time_shift", "gecen yil ayni"),
    ("time_shift", "same period last"),
    ("rank", "siralama"),
    ("rank", "ranking"),
)

#: Words that name a time grain.
GRAINS: tuple[tuple[str, str], ...] = (
    ("month", "aylik"),
    ("month", "ay bazinda"),
    ("month", "monthly"),
    ("month", "per month"),
    ("month", "month"),
    ("month", "aya gore"),
    ("year", "yillik"),
    ("year", "yearly"),
    ("year", "annual"),
    ("year", "per year"),
    ("year", "year"),
    ("quarter", "ceyrek"),
    ("quarter", "quarterly"),
    ("quarter", "quarter"),
    ("week", "haftalik"),
    ("week", "weekly"),
    ("week", "week"),
    ("day", "gunluk"),
    ("day", "daily"),
    ("day", "gun bazinda"),
    ("day", "per day"),
)

#: Phrasings that introduce a breakdown. What follows one of these is a
#: dimension; ``X bazinda`` puts the dimension *before* the marker, which is
#: why both sides get searched.
BREAKDOWN = ("bazinda", "gore", "basina", "per ", "by ", "for each", "her ")

#: Business words for columns a bank's schema spells differently. This is
#: operator configuration, not inference: deciding that "ciro" means `tutar`
#: is the same decision as defining a measure, and belongs to whoever owns
#: the data rather than to whatever is reading the question.
SYNONYMS: Mapping[str, tuple[str, ...]] = {
    "tutar": ("ciro", "hacim", "amount", "revenue", "volume", "value", "miktar"),
    "islem": ("transaction", "islemler", "transactions"),
    "musteri": ("customer", "musteriler", "customers", "client"),
    "kanal": ("channel", "kanallar"),
    "durum": ("status", "state"),
    "segment": ("segmentler", "tier"),
    "sube_kodu": ("sube", "branch", "subeler", "branches"),
    "islem_turu": ("tur", "type", "turu", "islem tipi"),
    "para_birimi": ("currency", "doviz", "para"),
    "islem_tarihi": ("tarih", "date", "zaman", "time"),
    "acilis_tarihi": ("acilis", "opening", "opened"),
}


#: Words for the values themselves. Turkish questions match the stored value
#: after folding — "başarılı" is ``basarili`` — but an English question asking
#: about successful transactions has no word in common with the data at all.
#: Like the column synonyms, this is the operator saying what their codes mean.
VALUE_SYNONYMS: Mapping[str, tuple[str, ...]] = {
    "basarili": ("successful", "success", "completed"),
    "iptal": ("cancelled", "canceled", "cancellation"),
    "havale": ("wire", "remittance"),
    "odeme": ("payment", "paid"),
    "transfer": ("transfers",),
    "mobil": ("mobile",),
    "web": (),
    "sube": ("branch office",),
    "premium": (),
    "standart": ("standard",),
    "temel": ("basic",),
}


def _first_match(text: str, table: Sequence[tuple[str, str]]) -> str:
    """The value whose phrase appears in the text, longest phrase first."""
    for value, phrase in sorted(table, key=lambda pair: -len(pair[1])):
        if phrase in text:
            return value
    return ""


def _names_for(name: str, extra: Sequence[str] | None = None) -> tuple[str, ...]:
    """A column's own name, its parts, and the words an operator mapped to it."""
    folded = fold(name)
    parts = [part for part in folded.split("_") if len(part) > 2]
    return (folded, *parts, *(fold(w) for w in (extra or ())))


#: How much of a word has to agree before it counts as the same word.
#: Turkish inflects in both directions around a schema name — a question says
#: ``tutarı`` where the column is ``tutar``, and ``müşteri`` where the table is
#: ``musteriler`` — so neither string reliably contains the other and matching
#: happens on a shared stem.
STEM = 5


def _stem(word: str) -> str:
    return word[:STEM] if len(word) > STEM else word


def _agreement(candidate: str, text: str) -> int:
    """How many characters the text and this name actually agree on.

    The stem is there to survive inflection, not to stand in for the whole
    name. Scoring a stem hit by the *candidate's* length was the bug: with
    ``STEM = 5`` the bare word "islem" matched ``islem_turu`` and ``islem_tarihi``
    and was credited 10 and 12, so "Toplam islem tutari" ranked two columns it
    never mentioned above ``tutar``, which it did. Measuring the shared prefix
    instead gives all three 5, which is the truth: one word was recognised.
    """
    best = 0
    for found in re.finditer(rf"\b{re.escape(_stem(candidate))}\w*", text):
        word = found.group(0)
        shared = 0
        for mine, theirs in zip(candidate, word, strict=False):
            if mine != theirs:
                break
            shared += 1
        best = max(best, shared)
    return best


def _mentions(text: str, name: str, extra: Sequence[str] | None = None) -> int:
    """How strongly the text names this column or table."""
    best = 0
    for candidate in _names_for(name, extra):
        if len(candidate) < 3:
            continue
        best = max(best, _agreement(candidate, text))
    return best


class LexiconPlanner:
    """Turns a question into a semantic request by matching words.

    Reads its vocabulary off the same schema the constrained arm decodes
    against, so the two are restricted identically and only the choosing
    differs. Nothing here can name a measure, dimension or value that the
    semantic model does not have.
    """

    def __init__(
        self,
        semantic: Any,
        tables: Sequence[str],
        synonyms: Mapping[str, Sequence[str]] | None = None,
        value_synonyms: Mapping[str, Sequence[str]] | None = None,
        values: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
    ) -> None:
        """
        Args:
            synonyms: Business words per column name. Defaults to the
                hand-written table; a generated lexicon is passed in here.
            value_synonyms: The same, per stored value.
            values: ``table -> column -> values``, as a request schema already
                carries. Passed in, discovery is skipped — each one is a full
                DSL round trip and the answer is the same either way.
        """
        from bench.constrain import dimension_values

        # Normalised on the way in, because a lexicon is a file a model wrote
        # and a person may edit: entries arrive with leading spaces, accents,
        # or as the name again.
        self._synonyms = _clean(SYNONYMS if synonyms is None else synonyms)
        self._value_synonyms = _clean(VALUE_SYNONYMS if value_synonyms is None else value_synonyms)
        self._tables = list(tables)
        self._models = {table: semantic.model_for(table) for table in tables}
        self._filter_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        if values is not None:
            self._values = {
                table: {column: list(found) for column, found in values.get(table, {}).items()}
                for table in self._tables
            }
        else:
            self._values = {
                table: {
                    d.as_dict()["name"]: found
                    for d in model.dimensions
                    if (found := dimension_values(semantic, table, d))
                }
                for table, model in self._models.items()
            }

    def _mentions(self, text: str, name: str) -> int:
        """``_mentions`` with this planner's lexicon applied."""
        return _mentions(text, name, self._synonyms.get(name))

    # -- the parts of a question -------------------------------------------

    def _filters_for(self, table: str, text: str) -> list[dict[str, Any]]:
        """Conditions read straight out of the question's own words.

        The enumerated values are what makes this work without a model: the
        question says "başarılı", the column stores ``basarili``, and folding
        both makes that the same word. A value that is also a common word is
        skipped — matching every sentence containing "web" would be worse
        than matching none.
        """
        found = []
        for column, values in self._values[table].items():
            for value in values:
                folded = fold(str(value))
                if len(folded) < 4:
                    continue
                words = (folded, *(fold(w) for w in self._value_synonyms.get(folded, ())))
                if any(_value_asked_for(text, word) for word in words):
                    found.append({"column": column, "op": "eq", "value": value})
                    break
        return found

    def _conditions(self, table: str, text: str) -> list[dict[str, Any]]:
        """`_filters_for`, memoised for the question being planned.

        `plan` reaches it four times for the same pair — twice through
        `_score`, once choosing a breakdown, once building the request — and
        each pass re-scans every enumerated value with a regex. The answer
        depends only on the table and the text, and both are fixed.
        """
        cached = self._filter_cache.get((table, text))
        if cached is None:
            cached = self._filters_for(table, text)
            self._filter_cache[(table, text)] = cached
        return cached

    def _score(self, table: str, text: str) -> int:
        """How much of the question this table can account for.

        The table's own name counts: "kaç müşteri var" names no column at all,
        and without this the question would be answered against whichever
        table happened to be first.
        """
        model = self._models[table]
        columns = {m.name for m in model.measures} | {d.as_dict()["name"] for d in model.dimensions}
        score = sum(self._mentions(text, name) for name in columns)
        score += 3 * self._mentions(text, table.split(".")[-1])
        # A matched value is strong evidence: only one table stores it.
        return score + 10 * len(self._conditions(table, text))

    def _measure_and_agg(self, table: str, text: str) -> tuple[str, str]:
        model = self._models[table]
        agg = _first_match(text, AGGREGATIONS)
        named = [
            (self._mentions(text, m.name), m)
            for m in model.measures
            if not m.is_row_count and self._mentions(text, m.name)
        ]
        numeric = [(score, m) for score, m in named if m.numeric and not _looks_like_a_key(m.name)]
        if numeric and agg not in ("count", "count_distinct", ""):
            return max(numeric, key=lambda pair: pair[0])[1].name, agg
        if agg in ("", "count"):
            # "how many X" counts rows; nothing else needs naming.
            return "*", "count"
        if numeric:
            return max(numeric, key=lambda pair: pair[0])[1].name, agg
        if named and agg == "count_distinct":
            return max(named, key=lambda pair: pair[0])[1].name, agg
        return "*", "count"

    def _dimensions_for(self, table: str, text: str, measure: str) -> list[dict[str, Any]]:
        """What to break the measure down by.

        A dimension is only taken when the question asks for a breakdown —
        "kanal bazında", "per segment" — or names a time grain. Without that
        test every mention of a column would become a GROUP BY, and "toplam
        tutar" would answer a question nobody asked.
        """
        model = self._models[table]
        grain = _first_match(text, GRAINS)
        chosen: list[dict[str, Any]] = []
        temporal = next((d for d in model.dimensions if d.as_dict().get("temporal")), None)
        if grain and temporal is not None:
            chosen.append({"column": temporal.as_dict()["name"], "grain": grain})
        if not any(marker in text for marker in BREAKDOWN):
            return chosen
        filtered = {f["column"] for f in self._conditions(table, text)}
        scored = [
            (self._mentions(text, d.as_dict()["name"]), d.as_dict()["name"])
            for d in model.dimensions
            if not d.as_dict().get("temporal")
        ]
        for score, name in sorted(scored, reverse=True):
            # Not the measure itself, and not something a filter has already
            # pinned to a single value. A name that reads like a key is only
            # rejected when nothing suggests it is a category: `sube_kodu` is
            # a branch code with three values and groups perfectly well, while
            # `islem_id` has one value per row and groups into nothing. The
            # enumeration already drew that line, so it is reused rather than
            # guessed at again.
            if not score or name == measure or name in filtered:
                continue
            if _looks_like_a_key(name) and name not in self._values[table]:
                continue
            chosen.append({"column": name})
            break
        return chosen

    # -- the whole request --------------------------------------------------

    def plan(self, question: str) -> dict[str, Any]:
        """The request this question asks for, in the constrained arm's shape."""
        from fusion.domain.metric_dsl import parse_metric

        text = fold(question)
        table = max(self._tables, key=lambda name: self._score(name, text))
        measure, agg = self._measure_and_agg(table, text)
        transform = _first_match(text, TRANSFORMS)
        metric: dict[str, Any] = {"measure": measure, "aggregation": agg}
        if transform:
            metric["transform"] = transform
        dimensions = self._dimensions_for(table, text, measure)
        request: dict[str, Any] = {
            "table": table,
            "metrics": [metric],
            "dimensions": dimensions,
            "filters": list(self._conditions(table, text)),
        }
        limit = _limit_in(text)
        if limit:
            request["limit"] = limit
            # "the 5 highest" is an ordering as much as a limit, and the name
            # to order by is whatever the domain calls the chosen metric.
            request["order_by"] = parse_metric(f"{measure}:{agg}").output_name()
            request["descending"] = agg != "min"
        return request


def _clean(lexicon: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, ...]]:
    """Make a lexicon usable however it was written.

    A generated one arrives with leading spaces and accents, and with entries
    that are just the name again — ``tutar: ["tutar", " tutari"]``. A leading
    space alone would make an entry dead on arrival, because the match anchors
    on a word boundary. Echoes of the name are dropped rather than kept: the
    stem match already covers every inflection, so an entry that only repeats
    the name adds nothing and hides that nothing was contributed.
    """
    cleaned: dict[str, tuple[str, ...]] = {}
    for name, words in lexicon.items():
        stem = fold(str(name))[:STEM]
        kept = []
        for word in words:
            folded = fold(str(word)).strip()
            if len(folded) > 2 and not folded.startswith(stem) and folded not in kept:
                kept.append(folded)
        if kept:
            cleaned[name] = tuple(kept)
    return cleaned


def _value_asked_for(text: str, word: str) -> bool:
    """Whether this word is being used as a value rather than as a column.

    "şube bazında" asks to group *by* branch; it does not ask for the rows
    whose channel is ``sube``. A word immediately followed by a breakdown
    marker is naming a dimension, so it is not read as a value.
    """
    for found in re.finditer(rf"\b{re.escape(word)}\w*", text):
        rest = text[found.end() :].lstrip()
        if not any(rest.startswith(marker.strip()) for marker in BREAKDOWN):
            return True
    return False


def _looks_like_a_key(name: str) -> bool:
    """Whether a name reads as an identifier rather than something to group by."""
    lowered = fold(name)
    return lowered == "id" or lowered.endswith(("_id", "_key", "_kodu"))


#: Words that turn a number into a row count rather than part of the question.
_SUPERLATIVE = r"(?:en (?:yuksek|dusuk|fazla|az|buyuk|kucuk)|top|ilk|highest|lowest)"

#: The number has to be *next to* the superlative, give or take a qualifier —
#: "en yuksek tutarli 5 islem", "the 5 highest transactions". Taking the first
#: number anywhere in the sentence read "Son 7 gunde en yuksek islemleri" as a
#: limit of 7, where the 7 is a time window and no row cap was asked for.
_LIMIT_PATTERNS = (
    re.compile(rf"{_SUPERLATIVE}\s+(?:\w+\s+){{0,2}}?(\d{{1,3}})\b"),
    # Nothing between the two in this direction: "5 highest" is a row count,
    # "7 gunde en yuksek" is a time window that happens to precede one.
    re.compile(rf"\b(\d{{1,3}})\s+{_SUPERLATIVE}"),
)


def _limit_in(text: str) -> int:
    """The N in "en yüksek 5", "top 3", "the 5 highest" — and nowhere else."""
    for pattern in _LIMIT_PATTERNS:
        found = pattern.search(text)
        if found:
            return int(found.group(1))
    return 0
