from pydantic import Field
from app.schemas.extraction import Extraction, ExtractedTask, ExtractedDecision


class ConsolidatedTask(ExtractedTask):
    candidate_ids: list[str] = Field(min_length=1, max_length=100)


class ConsolidatedDecision(ExtractedDecision):
    candidate_ids: list[str] = Field(min_length=1, max_length=100)


class ConsolidatedExtraction(Extraction):
    tasks: list[ConsolidatedTask]
    decisions: list[ConsolidatedDecision]
