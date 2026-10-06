from uuid import UUID
from pydantic import Field, model_validator
from app.schemas.extraction import StrictModel


class MemorySection(StrictModel):
    text: str = Field(default="", max_length=3000)
    source_ids: list[UUID] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def grounded(self):
        if self.text.strip() and not self.source_ids:
            raise ValueError("Nonempty memory sections require original source references.")
        return self


class ProjectMemoryContent(StrictModel):
    executive_summary: MemorySection = Field(default_factory=MemorySection)
    objective: MemorySection = Field(default_factory=MemorySection)
    current_state: MemorySection = Field(default_factory=MemorySection)
    recent_progress: MemorySection = Field(default_factory=MemorySection)
    open_items: MemorySection = Field(default_factory=MemorySection)
    risks_and_blockers: MemorySection = Field(default_factory=MemorySection)
    key_decisions: MemorySection = Field(default_factory=MemorySection)
    recently_completed: MemorySection = Field(default_factory=MemorySection)
    key_people: MemorySection = Field(default_factory=MemorySection)
    important_facts: MemorySection = Field(default_factory=MemorySection)
    open_questions: MemorySection = Field(default_factory=MemorySection)
    recent_changes: MemorySection = Field(default_factory=MemorySection)
