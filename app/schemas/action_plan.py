from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from app.schemas.extraction import Extraction, StrictModel, ExtractedUpdate, ExtractedTask, ExtractedDecision
from app.schemas.reasoning import QueryPlan


class CompletedTask(StrictModel):
    task_id: UUID
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    evidence: str = Field(min_length=1, max_length=3000)
    state: Literal["performed", "future", "negated", "pending", "mentioned"]
    alternatives: list[UUID] = Field(default_factory=list, max_length=100)


class InterpretedQuery(StrictModel):
    question: str = Field(min_length=1, max_length=3000)
    retrieval: QueryPlan


class ActionPlan(Extraction):
    """Flat tasks/decisions preserve downstream and historical result conventions."""
    schema_version: Literal["action-plan-v1"]
    interaction: Literal["update", "query", "mixed"]
    scope_confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    updates: list[ExtractedUpdate] = Field(default_factory=list, max_length=100)
    completed_tasks: list[CompletedTask] = Field(default_factory=list, max_length=100)
    query: InterpretedQuery | None = None
    ambiguities: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def consistent_interaction(self):
        actions = bool(self.tasks or self.decisions or self.updates or self.completed_tasks)
        if self.interaction == "query" and (actions or self.query is None):
            raise ValueError("A pure query cannot mutate project state.")
        if self.interaction == "update" and self.query is not None:
            raise ValueError("An update cannot contain a query.")
        if self.interaction == "mixed" and (not actions or self.query is None):
            raise ValueError("A mixed message needs facts/actions and a query.")
        return self


class ItemAmbiguity(StrictModel):
    type: Literal["project", "task", "owner", "date", "query_scope", "other"]
    message: str = Field(min_length=1, max_length=1000)


class ItemScope(StrictModel):
    project_id: UUID | None
    scope_confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    ambiguities: list[ItemAmbiguity] = Field(default_factory=list, max_length=20)


class ScopedTask(ExtractedTask, ItemScope):
    pass


class ScopedDecision(ExtractedDecision, ItemScope):
    pass


class ScopedUpdate(ExtractedUpdate, ItemScope):
    pass


class ScopedCompletion(CompletedTask, ItemScope):
    pass


class ScopedQuery(InterpretedQuery):
    ambiguities: list[ItemAmbiguity] = Field(default_factory=list, max_length=20)


class ActionPlanV2(StrictModel):
    schema_version: Literal["action-plan-v2"]
    primary_project_id: UUID | None = None
    interaction: Literal["update", "query", "mixed"]
    summary: str
    tasks: list[ScopedTask] = Field(default_factory=list, max_length=100)
    decisions: list[ScopedDecision] = Field(default_factory=list, max_length=100)
    updates: list[ScopedUpdate] = Field(default_factory=list, max_length=100)
    completed_tasks: list[ScopedCompletion] = Field(default_factory=list, max_length=100)
    query: ScopedQuery | None = None
    people: list[str] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    ambiguities: list[ItemAmbiguity] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def consistent_interaction(self):
        return ActionPlan.consistent_interaction(self)


def read_action_plan(value):
    schema = ActionPlanV2 if value.get("schema_version") == "action-plan-v2" else ActionPlan
    owned = {"execution", "extraction_mode", "chunk_version", "partial_prompt_version", "part_ids", "generation_id", "candidate_dispositions"}
    return schema.model_validate({key: item for key, item in value.items() if key not in owned})
