"""Token cost computation (design.md §4, invariant 3).

Two rules this module exists to enforce.

**Tokens are four fields.** ``input + output + cache_read + cache_write``.
Summing only ``input_tokens`` makes the dashboard wrong by roughly 100x in a
long session, because 90%+ of the tokens are cache reads.

**Cost is computed at ingest**, with the price in effect at that moment, and
stored on the row. Never recomputed in a query. Editing ``pricing.yaml``
therefore affects future events only — cost history must not shift retroactively
when a vendor changes prices.

That second rule only holds if a superseded price can still be *found*. Replay
re-ingests, so a rebuild that reaches for the current table reprices every past
run at today's prices — silently, and invisibly in any diff. ``pricing.yaml``
therefore keeps its retired tables (`design.md` §4), :class:`PriceHistory`
exposes them by version, and :meth:`PriceHistory.table` raises
:class:`PriceTableNotFound` rather than falling back to the current one. Refusing
is recoverable; repricing history is not.

Everything here is pure except :func:`load_price_table` and
:func:`load_price_history`, which read the YAML once at a boundary.

Cache-write tiering: the API charges cache *writes* by TTL — roughly 1.25x the
input price for the 5-minute tier and 2.0x for the 1-hour tier. Claude Code
2.1.222 defaults to the 1-hour tier, so treating all cache writes as 5-minute
under-reports cost by up to 1.6x. :class:`TokenCounts` keeps the four fields as
the contract and carries the tier split alongside, for pricing only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

import yaml

# ``claude-haiku-4-5-20251001`` and ``claude-haiku-4-5`` are the same model.
# Claude Code emits both spellings in a single run — the dated one on
# ``assistant`` lines, the undated one on ``system/init`` — and keys
# ``result.modelUsage`` by both at once. Without normalization, one of the two
# misses the price table and silently yields a null cost.
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def normalize_model_id(raw: str) -> str:
    """Strip a trailing ``-YYYYMMDD`` release date from a model id."""
    return _DATE_SUFFIX.sub("", raw.strip())


class PricingError(Exception):
    """The price table itself is malformed. A missing *model* is not an error."""


class PriceTableNotFound(PricingError):
    """A pinned ``price_table_version`` is not in ``pricing.yaml`` any more.

    Raised by :meth:`PriceHistory.table`, and the reason replay refuses instead
    of continuing: the alternative is repricing a historical run at today's
    prices, which invariant 3 exists to prevent and which no diff would show.
    """

    def __init__(self, version: int, known: tuple[int, ...], source: Path | None):
        self.version = version
        self.known = known
        where = "the price history" if source is None else str(source)
        listed = ", ".join(str(v) for v in known) or "none"
        super().__init__(
            f"price table version {version} is not in {where} "
            f"(known versions: {listed}). Restore it under `superseded:` — "
            "pricing a past run with a different table rewrites cost history."
        )


@dataclass(frozen=True, slots=True)
class TokenCounts:
    """The four fields of invariant 3, plus the cache-write tier split.

    ``cache_write_tokens`` is the total. ``cache_write_5m_tokens`` and
    ``cache_write_1h_tokens`` refine it for pricing and must sum to it when
    either is set.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cache_write_5m_tokens",
            "cache_write_1h_tokens",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")

        tiered = self.cache_write_5m_tokens + self.cache_write_1h_tokens
        if tiered and tiered != self.cache_write_tokens:
            raise ValueError(
                "cache_write tier split "
                f"({self.cache_write_5m_tokens} + {self.cache_write_1h_tokens}) "
                f"does not sum to cache_write_tokens ({self.cache_write_tokens})"
            )

    @property
    def total(self) -> int:
        """All four fields. Not a billing quantity — a volume."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def __add__(self, other: TokenCounts) -> TokenCounts:
        return TokenCounts(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cache_write_5m_tokens=(
                self.cache_write_5m_tokens + other.cache_write_5m_tokens
            ),
            cache_write_1h_tokens=(
                self.cache_write_1h_tokens + other.cache_write_1h_tokens
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per 1M tokens."""

    input: float
    output: float
    # Provider-specific cache economics. Claude's TTL-aware events use the
    # global 5m/1h multipliers below; models without a TTL split may override
    # the conservative global fallback here (for example OpenAI at 1.25x).
    cache_read_multiplier: float | None = None
    cache_write_multiplier: float | None = None


@dataclass(frozen=True, slots=True)
class PriceTable:
    version: int
    models: dict[str, ModelPrice]
    cache_write_multiplier_5m: float
    cache_write_multiplier_1h: float
    cache_read_multiplier: float

    @classmethod
    def from_mapping(cls, raw: Any) -> Self:
        if not isinstance(raw, dict):
            raise PricingError("price table must be a mapping")

        version = raw.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise PricingError("price table `version` must be a positive integer")

        defaults = raw.get("defaults") or {}
        models_raw = raw.get("models")
        if not isinstance(models_raw, dict) or not models_raw:
            raise PricingError("price table has no models")

        models: dict[str, ModelPrice] = {}
        for model_id, prices in models_raw.items():
            try:
                models[normalize_model_id(str(model_id))] = ModelPrice(
                    input=float(prices["input"]),
                    output=float(prices["output"]),
                    cache_read_multiplier=(
                        float(prices["cache_read_multiplier"])
                        if "cache_read_multiplier" in prices
                        else None
                    ),
                    cache_write_multiplier=(
                        float(prices["cache_write_multiplier"])
                        if "cache_write_multiplier" in prices
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PricingError(f"bad price entry for {model_id!r}: {exc}") from exc

        return cls(
            version=version,
            models=models,
            cache_write_multiplier_5m=float(
                defaults.get("cache_write_multiplier_5m", 1.25)
            ),
            cache_write_multiplier_1h=float(
                defaults.get("cache_write_multiplier_1h", 2.0)
            ),
            cache_read_multiplier=float(defaults.get("cache_read_multiplier", 0.1)),
        )

    def cost_usd(self, model: str, counts: TokenCounts) -> float | None:
        """Cost of ``counts`` under ``model``, or ``None`` if the price is unknown.

        ``None`` is deliberate and must survive all the way to the UI. A model
        absent from the table means "we do not know what this cost", and zero is
        a number someone will trust.
        """
        price = self.models.get(normalize_model_id(model))
        if price is None:
            return None

        per_token_in = price.input / 1_000_000
        per_token_out = price.output / 1_000_000

        # An untiered cache_write is priced at the 1-hour rate. Claude Code
        # 2.1.222 defaults to that tier, so guessing the cheap one would
        # under-report; the conservative guess is the correct default here.
        tiered = counts.cache_write_5m_tokens + counts.cache_write_1h_tokens
        if tiered:
            write_5m = counts.cache_write_5m_tokens
            write_1h = counts.cache_write_1h_tokens
            cache_write_cost = (
                write_5m * per_token_in * self.cache_write_multiplier_5m
                + write_1h * per_token_in * self.cache_write_multiplier_1h
            )
        else:
            write_multiplier = (
                price.cache_write_multiplier
                if price.cache_write_multiplier is not None
                else self.cache_write_multiplier_1h
            )
            cache_write_cost = (
                counts.cache_write_tokens * per_token_in * write_multiplier
            )

        read_multiplier = (
            price.cache_read_multiplier
            if price.cache_read_multiplier is not None
            else self.cache_read_multiplier
        )

        return (
            counts.input_tokens * per_token_in
            + counts.output_tokens * per_token_out
            + counts.cache_read_tokens * per_token_in * read_multiplier
            + cache_write_cost
        )


@dataclass(frozen=True, slots=True)
class PriceHistory:
    """The current price table plus every table it superseded.

    One file, not a directory of versions: a reviewer has to be able to see in a
    single diff that raising a price *added* a table and did not edit an old one.
    Each retired entry is a **complete** table rather than a delta — replay
    depends on being able to reconstruct an exact price years later, and a delta
    chain turns that into an interpretation.
    """

    current: PriceTable
    superseded: Mapping[int, PriceTable] = field(default_factory=dict)
    # Where it was loaded from, for error messages only.
    source: Path | None = None

    @property
    def version(self) -> int:
        """The version new ingests price with."""
        return self.current.version

    @property
    def versions(self) -> tuple[int, ...]:
        return tuple(sorted({self.current.version, *self.superseded}))

    def table(self, version: int | None = None) -> PriceTable:
        """The table for ``version``; the current one when ``version`` is None.

        Raises :class:`PriceTableNotFound` for a version this file no longer
        carries. It never falls back to the current table — see the module
        docstring.
        """
        if version is None or version == self.current.version:
            return self.current
        found = self.superseded.get(version)
        if found is None:
            raise PriceTableNotFound(version, self.versions, self.source)
        return found

    @classmethod
    def from_mapping(cls, raw: Any, *, source: Path | None = None) -> Self:
        current = PriceTable.from_mapping(raw)
        retired_raw = raw.get("superseded") or []
        if not isinstance(retired_raw, list):
            raise PricingError("`superseded` must be a list of whole price tables")

        superseded: dict[int, PriceTable] = {}
        for entry in retired_raw:
            table = PriceTable.from_mapping(entry)
            if table.version in superseded or table.version == current.version:
                raise PricingError(
                    f"duplicate price table version {table.version}: a version "
                    "identifies exactly one table forever"
                )
            if table.version > current.version:
                raise PricingError(
                    f"superseded price table version {table.version} is newer "
                    f"than the current one ({current.version}); versions only "
                    "ever go up"
                )
            superseded[table.version] = table
        return cls(current=current, superseded=superseded, source=source)


def load_price_history(path: Path) -> PriceHistory:
    """Read ``pricing.yaml`` with its retired tables. The only I/O here."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PricingError(f"cannot read price table at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PricingError(f"malformed price table at {path}: {exc}") from exc
    return PriceHistory.from_mapping(raw, source=path)


def load_price_table(path: Path, *, version: int | None = None) -> PriceTable:
    """One table out of ``pricing.yaml``; the current one by default.

    Live ingest wants the default. Replay passes the version pinned in the run's
    ``meta.json`` and must get that exact table or an exception.
    """
    return load_price_history(path).table(version)
