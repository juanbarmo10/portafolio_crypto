"""Base class and shared retry/backoff for ingest modules (CLAUDE.md sections 7, 9).

Every concrete ingester subclasses :class:`Ingester` and implements ``fetch()``,
returning a DataFrame with the loader's column contract
``[source, series_id, ts, ts_release, value]``. The ``retry`` helper centralizes
exponential backoff so individual modules do not reimplement it (rate limits and
transient failures, section 9).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Callable, TypeVar

import pandas as pd

from core.logging_setup import get_logger
from db.loader import OBSERVATION_COLUMNS

log = get_logger(__name__)

T = TypeVar("T")


def retry(
    func: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay_s: float = 1.0,
    max_delay_s: float = 30.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    """Call ``func`` with exponential backoff.

    Args:
        func: Zero-argument callable to execute.
        attempts: Maximum number of attempts before re-raising.
        base_delay_s: Initial backoff delay; doubles each retry.
        max_delay_s: Upper bound on the delay between attempts.
        exceptions: Exception types that trigger a retry. Others propagate.

    Returns:
        Whatever ``func`` returns on the first successful attempt.

    Raises:
        The last exception if all attempts fail.
    """
    delay = base_delay_s
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except exceptions as exc:
            if attempt == attempts:
                log.error("All %d attempts failed: %s", attempts, exc)
                raise
            log.warning(
                "Attempt %d/%d failed (%s); retrying in %.1fs.",
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, max_delay_s)
    # Unreachable: the loop either returns or raises.
    raise RuntimeError("retry: exhausted attempts without returning")


class Ingester(ABC):
    """Abstract data-source ingester.

    Subclasses set :attr:`source` and implement :meth:`fetch`. Use
    :meth:`validate` to assert the output honors the loader column contract.
    """

    #: Source label written to observations.source (e.g. 'coingecko').
    source: str = ""

    @abstractmethod
    def fetch(self) -> pd.DataFrame:
        """Fetch data and return a DataFrame with :data:`OBSERVATION_COLUMNS`."""
        raise NotImplementedError

    @staticmethod
    def validate(df: pd.DataFrame) -> pd.DataFrame:
        """Assert the DataFrame has the required columns; fail loudly otherwise.

        Args:
            df: Candidate observations DataFrame.

        Returns:
            The same DataFrame (for chaining) if valid.

        Raises:
            ValueError: If any required column is missing (section 10: no silent
                failures in parsers).
        """
        missing = [c for c in OBSERVATION_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(
                f"fetch() output missing required columns: {missing}. "
                f"Expected {OBSERVATION_COLUMNS}."
            )
        return df
