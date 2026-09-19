"""Writing the lexicon with a model, so that answering needs none.

The deterministic planner's one real weakness is that its vocabulary — that
"ciro" means ``tutar``, that "successful" means ``basarili`` — is written by
hand, per schema and per language. A new database does not get one for free.

So move the model. Instead of spending a completion on every question, spend
one on the *schema*, once, and keep the result. What comes back is a data
file: reviewable before it is used, diffable in version control, correctable
by whoever owns the data, and completely absent at query time. Answering stays
deterministic, auditable and free.

This is the same shape as Warp's LLM catalog — a draft an operator approves,
not an answer a model gives — and the reason it is safe here is the same:
generation is constrained to the names that already exist, so a generated
lexicon can attach words to a column but can never invent one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bench.constrain import dimension_values
from bench.lexicon import fold
from bench.models import extract_json

#: Asked for per name. Enough to cover how a question is actually phrased,
#: few enough that the model stays specific instead of listing the thesaurus.
WORDS_PER_NAME = 6


def _vocabulary(semantic: Any, tables: Sequence[str]) -> tuple[list[str], dict[str, list[Any]]]:
    """Every column name, and the values worth naming, across the tables."""
    columns: list[str] = []
    values: dict[str, list[Any]] = {}
    for table in tables:
        model = semantic.model_for(table)
        columns.append(table.split(".")[-1])
        for measure in model.measures:
            if not measure.is_row_count:
                columns.append(measure.name)
        for dimension in model.dimensions:
            columns.append(dimension.as_dict()["name"])
            for value in dimension_values(semantic, table, dimension):
                values.setdefault(fold(str(value)), []).append(value)
    return list(dict.fromkeys(columns)), values


def lexicon_schema(semantic: Any, tables: Sequence[str]) -> dict[str, Any]:
    """A schema admitting words for these names and no others.

    One property per real name, and ``additionalProperties`` closed: the model
    chooses what a column is *called*, never what columns there are.
    """
    columns, values = _vocabulary(semantic, tables)
    # `minItems: 0` and nothing required, deliberately. Demanding a word for
    # every name is what produced the first generation's `tutar: ["tutar",
    # " tutar"]` — with no way to say "nothing to add", the model filled the
    # array with inflections of the name, which `lexicon._clean` then threw
    # away. Letting it decline makes the output mean something.
    words = {
        "type": "array",
        "minItems": 0,
        "maxItems": WORDS_PER_NAME,
        "items": {"type": "string"},
    }
    return {
        "type": "object",
        "properties": {
            "columns": {
                "type": "object",
                "properties": {name: dict(words) for name in columns},
                "additionalProperties": False,
            },
            "values": {
                "type": "object",
                "properties": {name: dict(words) for name in values},
                "additionalProperties": False,
            },
        },
        "required": ["columns", "values"],
        "additionalProperties": False,
    }


def generation_prompt(semantic: Any, tables: Sequence[str]) -> str:
    """What the model is told about the schema it is writing a lexicon for."""
    columns, values = _vocabulary(semantic, tables)
    blocks = []
    for table in tables:
        model = semantic.model_for(table)
        described = ", ".join(
            sorted(
                {m.name for m in model.measures if not m.is_row_count}
                | {d.as_dict()["name"] for d in model.dimensions}
            )
        )
        blocks.append(f"Table {table.split('.')[-1]}: {described}")
    return (
        "You are writing a lookup table for a banking database, so that "
        "questions can be answered by matching words instead of by a model.\n\n"
        "For each name, list the OTHER words a question would use for it — the "
        "business word, the everyday word, and the English one.\n\n"
        "An inflection of the name is not wanted and will be discarded: the "
        "matcher already handles endings, so 'tutari' adds nothing to "
        "'tutar'. Only different words count.\n\n"
        "Example, for a transaction amount column named tutar:\n"
        '  good: ["ciro", "hacim", "amount", "revenue", "value"]\n'
        '  bad:  ["tutar", "tutari", "tutarlar"]   (the name again)\n\n'
        "Example, for a stored status value basarili:\n"
        '  good: ["successful", "success", "completed", "gerceklesen"]\n'
        '  bad:  ["basarili", "basarili islem"]\n\n'
        "Write Turkish without accents ('basarili', not 'başarılı'), lower "
        "case, no leading spaces. Be specific: a word that fits every column "
        "is useless.\n\n"
        + "\n".join(blocks)
        + "\n\nStored values to describe: "
        + ", ".join(values)
        + f"\n\nColumn and table names to describe: {', '.join(columns)}\n"
    )


def generate(model: Any, semantic: Any, tables: Sequence[str]) -> dict[str, list[str]]:
    """Ask a model for the lexicon. Returns ``{"columns": …, "values": …}``."""
    reply = model.complete(generation_prompt(semantic, tables), lexicon_schema(semantic, tables))
    # Through the same scanner every other reply goes through: a model wraps
    # its answer in a fence or a sentence often enough that a bare json.loads
    # here would throw away a generation that had actually succeeded.
    parsed = json.loads(extract_json(reply))
    return {
        "columns": {k: list(v) for k, v in (parsed.get("columns") or {}).items()},
        "values": {k: list(v) for k, v in (parsed.get("values") or {}).items()},
    }


def save(lexicon: Mapping[str, Any], path: str | Path) -> Path:
    """Store a generated lexicon where a human can read and correct it."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(lexicon, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def load(path: str | Path) -> dict[str, dict[str, list[str]]]:
    """Read a stored lexicon back."""
    parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        "columns": parsed.get("columns") or {},
        "values": parsed.get("values") or {},
    }
