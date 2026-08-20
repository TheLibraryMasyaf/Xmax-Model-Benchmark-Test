"""Case number allocation.

Numbers follow ``feedXXX_promptYYY`` with an optional ``_NN`` repeat suffix.
A single repeat with no history keeps the bare number; repeat groups allocate
``_01..._NN`` at plan freeze time; later batches continue from the maximum
existing suffix read from the remote/repository snapshot.
"""

from __future__ import annotations

import re

CASE_PATTERN = re.compile(r"^feed\d+_prompt\d+(?:_\d{2,})?$")
NUMBER_PATTERN = re.compile(r"^feed\d+|^prompt\d+")


class CaseNumberAllocator:
    def __init__(self, repository: object | None = None) -> None:
        self._repository = repository

    def feed_number(self, index: int) -> str:
        return f"feed{index:03d}"

    def prompt_number(self, index: int) -> str:
        return f"prompt{index:03d}"

    def allocate(
        self,
        prefix: str,
        *,
        repeat_index: int,
        repeat_count: int,
        existing_max_suffix: int = 0,
    ) -> str:
        """Allocate one case number for a repeat.

        ``existing_max_suffix`` is the largest suffix already used for this
        prefix across history; new plans continue from there.
        """

        if repeat_count == 1 and existing_max_suffix == 0:
            return prefix
        actual = existing_max_suffix + repeat_index
        return f"{prefix}_{actual:02d}"

    def existing_max_suffix(self, prefix: str) -> int:
        if self._repository is None:
            return 0
        maximum = 0
        try:
            maximum = self._repository.max_case_suffix(prefix)
        except AttributeError:
            maximum = 0
        return maximum

    @staticmethod
    def prefix_for(feed_number: str, prompt_number: str) -> str:
        return f"{feed_number}_{prompt_number}"

    @staticmethod
    def is_valid(case_number: str) -> bool:
        return CASE_PATTERN.fullmatch(case_number) is not None
