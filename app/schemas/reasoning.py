from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, Field, StrictBool, field_validator, model_validator
from app.schemas.extraction import StrictModel


class QueryPlan(StrictModel):
    semantic_queries: list[str] = Field(default_factory=list, max_length=3)
    scope_type: Literal["project", "company", "area", "global"] | None = None
    scope_value: str | None = Field(default=None, max_length=250)
    include_project_memory: StrictBool = True
    time_basis: Literal["source_date", "due_date", "decision_date", "created_date", "mixed", "none"] = "source_date"
    include_tasks: StrictBool = True
    include_decisions: StrictBool = True
    include_recent_sources: StrictBool = True
    include_completed_tasks: StrictBool = False
    retrieval_depth: Literal["focused", "normal", "broad"] = "normal"
    recent_sources_limit: int | None = Field(default=None, ge=1, le=15, strict=True)
    date_from: AwareDatetime | None = None
    date_to: AwareDatetime | None = None

    @field_validator("semantic_queries")
    @classmethod
    def validate_queries(cls, values):
        if any(not x.strip() or len(x) > 3000 for x in values):
            raise ValueError("Consulta semántica no válida.")
        return values

    @model_validator(mode="after")
    def consistent(self):
        if self.scope_type in {"project", "company", "area"} and not (self.scope_value or "").strip():
            raise ValueError("El alcance requiere un nombre.")
        if self.scope_type in {None, "global"} and self.scope_value is not None:
            raise ValueError("El alcance global no admite nombre.")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("Intervalo de fechas invertido.")
        return self


class SynthesizedAnswer(StrictModel):
    text: str = Field(min_length=1, max_length=10000)
    source_ids: list[UUID] = Field(max_length=40)
