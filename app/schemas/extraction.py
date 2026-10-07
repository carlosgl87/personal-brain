from uuid import UUID
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ExtractedTask(StrictModel):
    title: str = Field(description="Una unidad de trabajo con estado independiente; no agrupar acciones separables ni dividir objetos de una misma acción.")
    description: str | None = Field(description="Contexto y condiciones/dependencias explícitas de esta acción; sin inventar pasos adicionales.")
    owner_text: str | None = Field(description="Responsable explícito de esta acción; no copiar responsables de otras acciones. null si no consta.")
    due_at: AwareDatetime | None = Field(description="Fecha límite de esta acción, con zona horaria; compartirla solo si el plazo abarca explícitamente todas las acciones. null si no consta.")
    evidence: str

    @field_validator("title")
    @classmethod
    def title_length(cls, value):
        if not value.strip() or len(value) > 500:
            raise ValueError("Título de tarea no válido.")
        return value

    @field_validator("owner_text")
    @classmethod
    def owner_length(cls, value):
        if value is not None and len(value) > 250:
            raise ValueError("Responsable demasiado largo.")
        return value


class ExtractedDecision(StrictModel):
    decision_text: str
    decided_at: AwareDatetime | None
    evidence: str

    @field_validator("decision_text")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("Decisión vacía.")
        return value


class Extraction(StrictModel):
    project_id: UUID | None
    summary: str
    tasks: list[ExtractedTask]
    decisions: list[ExtractedDecision]
    people: list[str]
    dates: list[str]
    follow_ups: list[str]
    tags: list[str]
