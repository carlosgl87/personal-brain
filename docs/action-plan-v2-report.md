# Action Plan v2 — reporte de hardening

Implementado localmente el 7 de octubre de 2026. No se hizo push, merge, deploy ni conexión a Railway o a una DB productiva. `.env` y seed_data no se modificaron.

## Flujo y versionado

Texto / audio → transcript / documento → Source guardada → shortlist local → Claude interpreta → ActionPlan v2 → Pydantic y validaciones de catálogo/evidencia/candidatos → ejecución transaccional → PostgreSQL → dirty por proyecto → Project Memory compacta. Una mixed query se responde después del commit, con su alcance independiente.

Fuentes nuevas usan `action-plan-v2`, `message-interpreter-v2`; documentos largos usan `action-plan-part-v2` y `action-plan-consolidation-v2`. Los parciales conservan evidencia literal; la consolidación asigna proyectos por item y conserva candidate dispositions y offsets. Una nota corta común necesita una sola llamada de interpretación; no se añaden clasificadores LLM.

Los planes v1 y extracciones antiguas siguen siendo legibles. El resultado ya procesado se devuelve sin llamar al modelo. La opción explícita `--reprocess --action-plan` aplica v2 sin reescribir metadata original ni JSON de runs anteriores. No hay reprocess automático ni backfill.

## Schema real

`app/schemas/action_plan.py`, clase `ActionPlanV2`:

- `schema_version: action-plan-v2`, `primary_project_id: UUID|null`, `interaction: update|query|mixed`, `summary`.
- Listas planas `tasks`, `completed_tasks`, `decisions`, `updates` (máximo 100 por categoría).
- Cada item tiene `project_id: UUID|null`, `scope_confidence: 0..1` y `ambiguities: [{type, message}]`.
- Task nueva: `title`, `description`, `owner_text`, `due_at`, `evidence`.
- Completion: `task_id`, `confidence`, `state`, `alternatives`, `evidence`.
- Decision: `decision_text`, `decided_at`, `evidence`. Update: `update_text`, `event_at`, `evidence`.
- `query: {question, retrieval: QueryPlan, ambiguities}|null`, independiente de los proyectos mutados.
- `people`, `dates`, `follow_ups`, `tags`; ambigüedades generales informativas.
- Tipos de ambigüedad: project, task, owner, date, query_scope, other.
- `interaction=mixed` significa acciones + pregunta; hablar de dos proyectos sin pregunta sigue siendo update.

Anthropic rechazó la gramática compilada de este esquema por tamaño. V2 envía el esquema JSON en el prompt y valida estrictamente la respuesta con el mismo Pydantic antes de ejecutar. Se acepta una envoltura completa ```json; no se recupera JSON parcial ni se aceptan campos desconocidos. V1 conserva structured output anterior. Una respuesta inválida no aplica acciones.

## Ejemplo real: dos proyectos

Caso evaluado con Claude real, en contexto sintético:

> En Dashboard Cobranzas ya enviamos los accesos. Para Catu quiero incorporar la línea de crédito disponible dentro del agente.

Resultado PASS: update «Se enviaron los accesos» y completion «Enviar accesos» en Dashboard Cobranzas; task nueva «Incorporar línea de crédito disponible dentro del agente» en Catu. Ambas asignaciones y completion tuvieron confidence 1.0; `event_at`/`due_at` null; sin ambigüedades. Los UUID son de fixture. El JSON completo está en `action-plan-v2-eval.json`; no se ejecutó contra DB.

En ejecución, cada fila conserva SU project_id. `Source.primary_project_id` queda null cuando afecta varios proyectos; `execution.project_ids` registra todos los scopes realmente aplicados. Las consultas de Sources y búsqueda semántica reconocen esta pertenencia. Project Memory recibe evidencia filtrada del proyecto para evitar copiar hechos del otro.

## Retrieval y exhaustividad

No se envía el catálogo completo al modelo. Se puntúan localmente nombre/slug/aliases, tokens, empresa/área, proyectos con updates recientes y contexto fijado. La puntuación solo propone candidatos: no asigna project_id final. La DB conserva todo el catálogo, sin límite conceptual de 100 proyectos.

Shortlist por presupuesto: máximo 20.000 caracteres o un tercio de `reasoning_context_max_chars`, el menor. Los empates se incluyen completos: si no caben, no se oculta una alternativa equivalente y se marca `catalog_truncated`. Ante truncamiento no se automatiza scope ni cierre; los hechos pueden guardarse sin proyecto. Hay tests con 500 proyectos y aliases ambiguos.

Por proyecto candidato se recuperan hasta 101 Tasks actuales abiertas/in_progress y sin completed_at: se entregan hasta 100 y `tasks_complete=true` solo si no había más. Si el presupuesto recorta Tasks, ese proyecto queda incompleto explícitamente. Updates suplementarios se retiran primero. Cinco Tasks del proyecto permiten cierre aunque existan cientos en otros proyectos. Más de 100 en ese proyecto bloquean sus cierres.

Para cerrar: proyecto autorizado y activo, scope confidence ≥0.90, candidatos completos de ese proyecto, task_id/alternativas en ese scope, confidence ≥0.95, state=performed, evidencia literal y sin ambigüedad relevante. Se bloquean proyectos en orden estable y Sources/Tasks objetivo; se revalida el conjunto de IDs y el snapshot de cada Task. Cambios de candidatos bloquean ese proyecto; cambios de una Task bloquean esa Task, dejando independientes las demás.

## Ambigüedad, auditoría y UX

Una ambigüedad general no bloquea completions. Project/task ambiguity o alternativas bloquean su completion; owner/date dudosos quedan null en su item sin contaminar otros. Query scope ambiguity pide aclaración de la consulta después de aplicar las mutaciones inequívocas.

`processing_runs.result` conserva el plan íntegro propuesto y `execution.items`: tipo, índice, project_id propuesto/aplicado, record/task ID, estado, razón, alternativas y ajustes de campos desconocidos. Estados applied/completed/not_applied/already_applied. Razones incluyen ambiguous, incomplete_candidates, unresolved_project, low_confidence, not_performed, candidates_changed, task_changed y duplicate_in_plan.

Se genera un evento run por cada proyecto afectado, con origin key diferenciada; TaskChanges mantienen su auditoría compartida y unicidad Source+Task. Los retries procesados devuelven el resultado guardado. Telegram agrupa hechos/tareas/decisiones/completions por proyecto y solo pide aclaraciones relevantes para continuar.

## DB e historia

No se añadió ni modificó migración; head sigue siendo `0012_action_plans`. Se usan las columnas project_id existentes y JSON de ProcessingRun. Alembic offline pasó.

**No se modificó ni reprocesó ningún dato histórico.** No se conectó a una base productiva ni se ejecutó seed/migración/reprocess contra datos reales. No se borraron intentos de completion ni se regeneró memoria histórica.

## PostgreSQL real

No hay psql/pg_ctl/PostgreSQL instalado en la ubicación estándar ni Docker disponible. Por tanto **no se ejecutaron pruebas SQL reales localmente**. Se agregó `.github/workflows/tests.yml`, con PostgreSQL 17 + pgvector y dos bases loopback desechables separadas: integración y migración. Ejecuta toda la suite, pip check, diff check y Alembic offline; no usa secretos ni proveedores de pago.

El workflow aún no se ha ejecutado: no se hizo push. Debe pasar antes de considerar validada la ejecución SQL real.

Las suites preparadas cubren upgrade 0011→0012 con historia de fixture preservada, tablas nuevas vacías, múltiples TaskChanges por Source, unicidad, FKs, triggers, rollback y concurrencia/advisory locks. Se añadieron pruebas SQL v2 de multi-project, Source membership, dirty de ambos scopes, idempotencia y rollback; fixtures de documentos se adaptaron a v2.

## Claude real

`python -m app.eval_action_plan` ejecuta diez casos sintéticos sin abrir DB. `--fixtures-only` permite revisar contextos sin llamadas. Credencial/modelo ausentes producen SKIP explícito. No es parte obligatoria del CI.

Evaluación final: **10 PASS / 0 REVIEW**. Dashboard, Catu, McKinsey completion, dos completions, futuro, negación, decisión, propuesta, mixed, multi-project. Los intentos preliminares detectaron el límite de gramática, la envoltura markdown y la confusión update/mixed, ya corregidos. PASS es una expectativa estructural de humo; no garantiza corrección semántica para todos los mensajes.

## Validación local final

- `python -B -m unittest discover -s tests -v`: 404 pruebas, **378 PASS / 26 SKIP / 0 FAIL**.
- `python -m pip check`: sin incompatibilidades.
- `git diff --check`: limpio.
- Alembic offline head: OK, sin conexiones.
- 29 tests v2 adicionales, más cobertura de canales y de integración existente.

Omitidas por no tener una PostgreSQL desechable configurada (25 integración + 1 upgrade):

- `test_action_plan_multiple_changes_and_updates_commit_once (test_database_integration.DatabaseIntegrationTests.test_action_plan_multiple_changes_and_updates_commit_once)`
- `test_chunk_vector_versions_reuse_text_without_repeating_embeddings (test_database_integration.DatabaseIntegrationTests.test_chunk_vector_versions_reuse_text_without_repeating_embeddings)`
- `test_document_asset_source_job_are_atomic_idempotent_and_new_updates_are_new_sources (test_database_integration.DatabaseIntegrationTests.test_document_asset_source_job_are_atomic_idempotent_and_new_updates_are_new_sources)`
- `test_document_job_insert_failure_rolls_back_source_and_asset (test_database_integration.DatabaseIntegrationTests.test_document_job_insert_failure_rolls_back_source_and_asset)`
- `test_document_original_bytes_cannot_be_updated_or_deleted (test_database_integration.DatabaseIntegrationTests.test_document_original_bytes_cannot_be_updated_or_deleted)`
- `test_document_worker_hierarchical_retry_preserves_parts_and_original (test_database_integration.DatabaseIntegrationTests.test_document_worker_hierarchical_retry_preserves_parts_and_original)`
- `test_document_worker_reuses_extraction_and_notification_failure_keeps_completed (test_database_integration.DatabaseIntegrationTests.test_document_worker_reuses_extraction_and_notification_failure_keeps_completed)`
- `test_edit_that_moves_note_invalidates_old_and_new_project (test_database_integration.DatabaseIntegrationTests.test_edit_that_moves_note_invalidates_old_and_new_project)`
- `test_event_duplicate_and_source_change_commit_atomically (test_database_integration.DatabaseIntegrationTests.test_event_duplicate_and_source_change_commit_atomically)`
- `test_hierarchical_processing_reuses_partials_and_preserves_generation_evidence (test_database_integration.DatabaseIntegrationTests.test_hierarchical_processing_reuses_partials_and_preserves_generation_evidence)`
- `test_natural_completion_persists_once_and_flows_into_memory_version (test_database_integration.DatabaseIntegrationTests.test_natural_completion_persists_once_and_flows_into_memory_version)`
- `test_natural_completion_rolls_back_task_audit_and_attempt_when_event_fails (test_database_integration.DatabaseIntegrationTests.test_natural_completion_rolls_back_task_audit_and_attempt_when_event_fails)`
- `test_new_event_during_snapshot_keeps_memory_dirty_and_retrieves_delta (test_database_integration.DatabaseIntegrationTests.test_new_event_during_snapshot_keeps_memory_dirty_and_retrieves_delta)`
- `test_new_telegram_edit_cannot_discard_manually_completed_task (test_database_integration.DatabaseIntegrationTests.test_new_telegram_edit_cannot_discard_manually_completed_task)`
- `test_original_sources_and_memory_history_are_immutable_in_database (test_database_integration.DatabaseIntegrationTests.test_original_sources_and_memory_history_are_immutable_in_database)`
- `test_processing_is_idempotent_and_current_queries_use_latest_run (test_database_integration.DatabaseIntegrationTests.test_processing_is_idempotent_and_current_queries_use_latest_run)`
- `test_query_ingestion_and_catalog_do_not_create_tasks_or_memory_events (test_database_integration.DatabaseIntegrationTests.test_query_ingestion_and_catalog_do_not_create_tasks_or_memory_events)`
- `test_queue_advisory_lock_excludes_other_backend_on_native_postgres (test_database_integration.DatabaseIntegrationTests.test_queue_advisory_lock_excludes_other_backend_on_native_postgres)`
- `test_queue_claim_completion_retry_and_stale_tokens_use_real_sql (test_database_integration.DatabaseIntegrationTests.test_queue_claim_completion_retry_and_stale_tokens_use_real_sql)`
- `test_queue_workers_do_not_process_same_source_concurrently_on_native_postgres (test_database_integration.DatabaseIntegrationTests.test_queue_workers_do_not_process_same_source_concurrently_on_native_postgres)`
- `test_seed_is_additive_and_idempotent_with_existing_projects (test_database_integration.DatabaseIntegrationTests.test_seed_is_additive_and_idempotent_with_existing_projects)`
- `test_unknown_document_caption_stays_unassigned_after_extraction (test_database_integration.DatabaseIntegrationTests.test_unknown_document_caption_stays_unassigned_after_extraction)`
- `test_updates_and_evidence_foreign_keys_offsets_and_immutability (test_database_integration.DatabaseIntegrationTests.test_updates_and_evidence_foreign_keys_offsets_and_immutability)`
- `test_v2_late_persistence_failure_rolls_back_all_projects (test_database_integration.DatabaseIntegrationTests.test_v2_late_persistence_failure_rolls_back_all_projects)`
- `test_v2_multi_project_membership_memory_and_idempotency (test_database_integration.DatabaseIntegrationTests.test_v2_multi_project_membership_memory_and_idempotency)`
- `test_upgrade_preserves_existing_sources_chunks_vectors_and_partials (test_migration_upgrade.MigrationUpgradeTests.test_upgrade_preserves_existing_sources_chunks_vectors_and_partials)`

## Riesgos pendientes

- El CI PostgreSQL está preparado pero no ejecutado: locks/FKs/triggers y migración aún necesitan ese resultado real.
- Claude puede interpretar mal un compromiso, alias, pronombre o fecha. La aplicación verifica catálogo, evidencia y condiciones de cierre; no prueba por sí sola la verdad semántica de una afirmación.
- V2 depende de JSON validado localmente porque la gramática completa supera el límite del proveedor; una respuesta inválida conserva la Source sin aplicar acciones y requiere retry.
- Shortlist puede no recuperar un proyecto sin pistas léxicas/contextuales. El resultado seguro es scope desconocido, no un UUID inventado. Un catálogo/tareas que exceden el presupuesto exige aclaración o commands.
- Catálogo se carga localmente en memoria para retrieval; solo el shortlist se envía a Claude. Muchos proyectos candidatos implican varias consultas de Tasks (una por candidato), acotadas por presupuesto.
- El smoke real probó texto corto sintético, no STT ni documentos largos reales. Audio/documentos y offsets se cubrieron con fixtures locales; revisar manualmente después del deploy.

## Diez mensajes manuales después del deploy

Preparar Tasks abiertas de prueba donde se espera cierre; los resultados dependen de candidatos reales del proyecto.

1. Ya tuvimos la reunión con Cobranzas. Los datos cuadraron y las líneas recomendadas les hicieron sentido. Mañana tengo que mandar los accesos.
2. Para Catu quiero cambiar la validación. Si el ID no parece un número debe pedir DNI y nombre para saber si es vendedor o supervisor. Esto hay que implementarlo.
3. Operating Model / McKinsey: ya envié los correos de validación a los responsables de IT.
4. Operating Model / McKinsey: ya envié los correos y terminé la presentación.
5. Mañana enviaré los correos.
6. No pude enviar los correos.
7. Evaluamos ambas opciones y decidimos usar Databricks.
8. Tal vez deberíamos usar Databricks.
9. Ya envié los correos de IT. ¿Qué me queda pendiente de Operating Model / McKinsey?
10. En Dashboard Cobranzas ya enviamos los accesos. Para Catu quiero incorporar la línea de crédito disponible dentro del agente.

Comprobar que 5/6 no cierran Tasks, 8 no crea Decision, 9 consulta después del cierre y 10 agrupa dos proyectos. Repetir 10 como audio/documento y consultar `/pendientes` de ambos scopes.

