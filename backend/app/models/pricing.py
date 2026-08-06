"""Token cost computation (design.md §4, invariant 3).

Two rules this module exists to enforce.

**Tokens are four fields.** ``input + output + cache_read + cache_write``.
Summing only ``input_tokens`` makes the dashboard wrong by roughly 100x in a
long session, because 90%+ of the tokens are cache reads.

**Cost is computed at ingest**, with the price in effect at that moment, and
stored on the row. Never recomputed in a query. Editing ``pricing.yaml``
therefore affects future events only — cost history must not shift retroactively
when a vendor changes prices.

Everything here is pure except :func:`load_price_table`, which reads the YAML
once at a boundary.

Cache-write tiering: the API charges cache *writes* by TTL — roughly 1.25x the
input price for the 5-minute tier and 2.0x for the 1-hour tier. Claude Code
2.1.222 defaults to the 1-hour tier, so treating all cache writes as 5-minute
under-reports cost by up to 1.6x. :class:`TokenCounts` keeps the four fields as
the contract and carries the tier split alongside, for pricing only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PricingError(f"bad price entry for {model_id!r}: {exc}") from exc

        return cls(
            version=int(raw.get("version", 0)),
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
        else:
            write_5m = 0
            write_1h = counts.cache_write_tokens

        return (
            counts.input_tokens * per_token_in
            + counts.output_tokens * per_token_out
            + counts.cache_read_tokens * per_token_in * self.cache_read_multiplier
            + write_5m * per_token_in * self.cache_write_multiplier_5m
            + write_1h * per_token_in * self.cache_write_multiplier_1h
        )


def load_price_table(path: Path) -> PriceTable:
    """Read ``pricing.yaml``. The only I/O in this module."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PricingError(f"cannot read price table at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PricingError(f"malformed price table at {path}: {exc}") from exc
    return PriceTable.from_mapping(raw)
