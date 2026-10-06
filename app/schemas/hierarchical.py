from typing import Literal
from pydantic import Field, model_validator
from app.schemas.extraction import StrictModel
from app.schemas.extraction import Extraction, ExtractedTask, ExtractedDecision


class ConsolidatedTask(ExtractedTask):
    candidate_ids: list[str] = Field(min_length=1, max_length=100)


class ConsolidatedDecision(ExtractedDecision):
    candidate_ids: list[str] = Field(min_length=1, max_length=100)


class CandidateDisposition(StrictModel):
    candidate_id: str
    status: Literal["kept", "merged", "rejected"]
    final_index: int | None = Field(default=None, ge=0, strict=True)
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def valid_target(self):
        if self.status == "rejected":
            if self.final_index is not None or not (self.reason or "").strip():
                raise ValueError("Rejected candidates require a reason and no final index.")
        elif self.final_index is None:
            raise ValueError("Kept/merged candidates require a final index.")
        return self


class ConsolidatedExtraction(Extraction):
    dispositions: list[CandidateDisposition] = Field(max_length=1000)
    tasks: list[ConsolidatedTask]
    decisions: list[ConsolidatedDecision]
