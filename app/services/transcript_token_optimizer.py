import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.services.transcript_source_selector import (
    SourceType,
    TranscriptSourceSelector,
)

DEFAULT_MAX_TRANSCRIPT_CHARS = 120_000


class TranscriptInputTooLargeError(ValueError):
    """Raised when optimized transcript exceeds the maximum allowed character limit."""

    def __init__(
        self,
        message: str = "Transcript exceeds maximum allowed length of 120000 characters.",
        character_count: int = 0,
        max_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS,
    ) -> None:
        self.message = message
        self.character_count = character_count
        self.max_chars = max_chars
        self.status_code = 422
        self.error_code = "INPUT_TOO_LARGE"
        super().__init__(message)


@dataclass(frozen=True)
class TranscriptTokenOptimizationResult:
    optimized_text: str
    character_count: int
    word_count: int
    estimated_token_count: int
    exceeds_limit: bool
    selected_source: SourceType
    max_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS


def estimate_token_count(text: str) -> int:
    """
    Estimates token count from text using a standard heuristic (~4 chars per token for English/multilingual text).
    Note: This is an estimated token count, not an actual model tokenizer count.
    """
    if not text:
        return 0
    return int(len(text) / 4.0) + (1 if len(text) % 4 > 0 else 0)


def optimize_transcript_text(
    text: str | None,
    max_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS,
    raise_on_limit: bool = False,
    selected_source: SourceType = "none",
) -> TranscriptTokenOptimizationResult:
    """
    Deterministically optimizes normalized transcript text for token/context efficiency.

    Optimizations performed:
    - Normalizes newline variations (\\r\\n -> \\n).
    - Removes redundant blank lines between transcript entries.
    - Strips leading/trailing overall whitespace and trailing line whitespace.
    - Preserves 100% of spoken content, Vietnamese/Unicode characters, timestamps, and speaker names.

    Validation:
    - Calculates exact character count, word count, and estimated token count.
    - Flags `exceeds_limit = True` if character count > max_chars.
    - Raises `TranscriptInputTooLargeError` if raise_on_limit=True and character count > max_chars.
    """
    if not text:
        return TranscriptTokenOptimizationResult(
            optimized_text="",
            character_count=0,
            word_count=0,
            estimated_token_count=0,
            exceeds_limit=False,
            selected_source=selected_source,
            max_chars=max_chars,
        )

    # 1. Normalize line endings
    raw_text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 2. Split lines and clean individual lines while removing empty/blank lines
    raw_lines = raw_text.split("\n")
    cleaned_lines: list[str] = []
    for line in raw_lines:
        line_clean = line.rstrip()
        if line_clean.strip():
            cleaned_lines.append(line_clean)

    optimized_text = "\n".join(cleaned_lines)

    char_count = len(optimized_text)
    word_count = len(optimized_text.split()) if optimized_text else 0
    token_est = estimate_token_count(optimized_text)
    exceeds = char_count > max_chars

    if exceeds and raise_on_limit:
        raise TranscriptInputTooLargeError(
            message=f"Transcript character count ({char_count}) exceeds maximum allowed limit of {max_chars}.",
            character_count=char_count,
            max_chars=max_chars,
        )

    return TranscriptTokenOptimizationResult(
        optimized_text=optimized_text,
        character_count=char_count,
        word_count=word_count,
        estimated_token_count=token_est,
        exceeds_limit=exceeds,
        selected_source=selected_source,
        max_chars=max_chars,
    )


class TranscriptTokenOptimizer:
    """Service to orchestrate source selection (Step 3C) and token optimization (Step 3D)."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.selector = TranscriptSourceSelector(session)

    def optimize_by_meeting_id(
        self,
        meeting_id: uuid.UUID,
        max_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS,
        raise_on_limit: bool = False,
    ) -> TranscriptTokenOptimizationResult:
        """
        Retrieves selected transcript from Step 3C for meeting_id,
        and applies deterministic token optimization.
        Read-only operation.
        """
        selection_result = self.selector.select_source_by_meeting_id(meeting_id)
        return optimize_transcript_text(
            text=selection_result.transcript_text,
            max_chars=max_chars,
            raise_on_limit=raise_on_limit,
            selected_source=selection_result.selected_source,
        )
