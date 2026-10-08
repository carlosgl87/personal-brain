# Consultas naturales con autoridad SQL

Implementado localmente el 8 de octubre de 2026. No se hizo push, deploy ni conexión a Railway/DB productiva. No se cambió `.env`, el seed, el schema PostgreSQL ni ninguna migración.

## Flujo final

Mensaje natural → Source → Message Interpreter / ActionPlan V2 → QueryPlan con necesidades de datos y presentación → backend recupera fuentes permitidas → renderer exacto o synthesis → nuevo ReasoningRun con respuesta y Tasks mostradas.

Los mensajes posteriores vuelven al Message Interpreter y ActionPlan V2. No se añadió router por keywords, regex semántico, classifier adicional ni resolver paralelo de follow-ups. Los comandos existentes conservan su fallback.

Versiones nuevas: `memory-planner-v4-sql-authority` y `message-interpreter-v2-query-authority-v1`. El schema de escritura ActionPlan sigue siendo v2. QueryPlans anteriores siguen siendo legibles con defaults; respuestas históricas cacheadas no se reescriben.

## QueryPlan

Se conservaron los flags include_* y semantic_queries y se añadieron tres campos:

- `data_authority: structured | contextual`: entidades exactas o exploración contextual.
- `presentation: list | analysis`: listado determinístico o análisis sobre evidencia recuperada.
- `catalog_entity: projects | companies | areas | null`: tabla de catálogo solicitada.

Claude interpreta estas opciones semánticamente en la llamada existente; no se inspecciona el texto del usuario para decidir el camino.

## Estado exacto

Para «Dame mis pendientes», Claude pide structured/list y Tasks abiertas. El backend usa `task_statement`: open/in_progress, completed_at null, current_derived y el scope/ventana indicados. Aunque un plan structured pidiera accidentalmente memoria, Sources, Updates o semantic_queries, el backend no los recupera.

Decisions exactas se recuperan de decisions; catálogo de projects/companies/areas. Una Task completed no aparece ni puede agregarse desde una nota antigua o open_items de memoria, porque esas fuentes no entran en la respuesta exacta.

SQL devuelve filas y total mediante `count(...) OVER` en la misma consulta: no hay un count anterior que pueda desincronizarse del listado. Se recuperan hasta 1.000 filas por categoría y se aplica el presupuesto de caracteres. `coverage` conserva total, shown, complete y razón de recorte. Las 35 Tasks del test se muestran completas; un límite de 30 para 47 informa «Hay 47 tareas abiertas. Te muestro las primeras 30.»

La presentación agrupa por proyecto, no muestra UUIDs ni owner/fecha ausentes. Owner y vencimiento se muestran cuando existen. Owner null no se modifica en DB ni se asigna implícitamente a Carlos.

## Presentación y llamadas

Un listado structured/list usa renderer determinístico: normalmente una sola llamada de Message Interpreter, SQL y respuesta. Sin embeddings ni segunda llamada de synthesis. El fallback /ask usa una llamada de planner y el mismo backend.

Una priorización o análisis puede usar structured/analysis: Claude recibe exclusivamente el conjunto SQL exacto, con su cobertura. No se incorporan posibles tareas desde historia. La synthesis informa qué IDs de Tasks ha listado; la aplicación comprueba que esos IDs estén en la evidencia y guarda su orden para follow-ups.

## Exploración

«Qué podría estar olvidando» o «Hay compromisos en mis notas que nunca convertí en tareas» usa contextual. Se recuperan únicamente las fuentes habilitadas por el plan: Tasks, Decisions, memoria, deltas, Updates, Sources y/o búsqueda semántica.

También se recupera estado SQL actual de Tasks del scope, incluidos cierres, con límite de 1.000 y presupuesto. Ese estado tiene precedencia sobre memorias y notas antiguas. Si el registro recuperado es parcial, el prompt prohíbe afirmar que un compromiso está ausente de la DB.

La synthesis debe separar «Tareas registradas» de «Posibles compromisos no registrados», etiquetar inferencias y no convertir hipótesis en Tasks oficiales. Las Decisions registradas prevalecen sobre resúmenes. Project Memory conserva su propósito compacto y derivado.

## Follow-up

«Dame mis pendientes» → respuesta con Tasks SQL → nuevo ReasoningRun.retrieved_context.shown_tasks guarda task_id, title, project_id, project_name, status, ordinal global y ordinal dentro del proyecto.

Para «Ya completé la de Catu», el contexto del ActionPlan V2 incorpora la última respuesta con Tasks de la misma conversación privada y usuario, anterior a la Source actual y con antigüedad máxima de seis horas. Se envían como máximo 100 Tasks y 16.000 caracteres; si falta parte se indica complete=false. Este contexto se copia, nunca modifica el JSON previo.

Los proyectos del listado se añaden como candidatos locales de retrieval, sin decidir el proyecto. Las Tasks se vuelven a consultar por proyecto y cada referencia anterior indica si su ID sigue siendo candidato abierto. El listado anterior no autoriza un cierre: siguen siendo obligatorios UUID permitido, proyecto inequívoco/activo, lista exhaustiva, evidencia actual, confidence, estado performed y revalidación transaccional.

Si «ya hice esa» tiene alternativas, Claude las propone y la ejecución existente no completa automáticamente; el recibo pide aclaración. No hay reglas de lenguaje para resolver el pronombre.

## Autoridad

Estado SQL actual de Tasks → deltas → Project Memory → Updates/Sources/Chunks históricos. Las Decisions SQL tienen precedencia sobre resúmenes antiguos. Esta regla figura tanto en la política compartida de planning/interpretation como en synthesis. En estado exacto, se refuerza por exclusión física de las fuentes históricas, no solo por prompt.

## Búsqueda semántica y warnings

Nunca se usa para structured, incluida presentación analítica. Solo se intenta cuando una exploración contextual pide semantic_queries.

La cobertura se representa con flags/status explícitos, no buscando palabras en warnings. Embeddings no disponibles, búsqueda fallida, notas del scope/ventana sin vectores del modelo activo, selección limitada o reducción por presupuesto pueden marcar una limitación material. La comprobación de indexación respeta el mismo scope y ventana y no cambia ni indexa datos.

Una búsqueda vacía por sí sola no demuestra indexación incompleta. Si la cobertura es adecuada, no produce warning. La respuesta exacta no muestra avisos de embeddings/indexación. Cuando corresponde, el aviso dice que la exploración por significado tiene cobertura limitada y puede omitir temas, sin detalles de configuración.

Los límites de filas/presupuesto se comunican con total y cobertura, independientemente de warnings semánticos.

## Validación

- `python -B -m unittest discover -s tests -v`: **432 total, 404 PASS, 28 SKIP, 0 FAIL**.
- 26 tests nuevos de autoridad, rendering, cobertura, límites, contexto y follow-up.
- Dos tests nuevos en la suite PostgreSQL: exclusión de completed/historia con JSON de Tasks mostradas y aislamiento/expiración de conversación.
- `python -m pip check`: correcto.
- `git diff --check`: correcto.

El workflow PostgreSQL existente se conserva y descubrirá las nuevas pruebas. No se ejecutó CI para estos cambios porque esta solicitud prohíbe push; no hay PostgreSQL/Docker local configurado. Por ello las 27 pruebas de integración y la prueba de upgrade están omitidas y **su ejecución real de esta revisión sigue pendiente**. No se debe confundir el CI exitoso del commit anterior con validación del código local nuevo.

Pruebas omitidas:

- `test_action_plan_multiple_changes_and_updates_commit_once (test_database_integration.DatabaseIntegrationTests.test_action_plan_multiple_changes_and_updates_commit_once)`
- `test_chunk_vector_versions_reuse_text_without_repeating_embeddings (test_database_integration.DatabaseIntegrationTests.test_chunk_vector_versions_reuse_text_without_repeating_embeddings)`
- `test_document_asset_source_job_are_atomic_idempotent_and_new_updates_are_new_sources (test_database_integration.DatabaseIntegrationTests.test_document_asset_source_job_are_atomic_idempotent_and_new_updates_are_new_sources)`
- `test_document_job_insert_failure_rolls_back_source_and_asset (test_database_integration.DatabaseIntegrationTests.test_document_job_insert_failure_rolls_back_source_and_asset)`
- `test_document_original_bytes_cannot_be_updated_or_deleted (test_database_integration.DatabaseIntegrationTests.test_document_original_bytes_cannot_be_updated_or_deleted)`
- `test_document_worker_hierarchical_retry_preserves_parts_and_original (test_database_integration.DatabaseIntegrationTests.test_document_worker_hierarchical_retry_preserves_parts_and_original)`
- `test_document_worker_reuses_extraction_and_notification_failure_keeps_completed (test_database_integration.DatabaseIntegrationTests.test_document_worker_reuses_extraction_and_notification_failure_keeps_completed)`
- `test_edit_that_moves_note_invalidates_old_and_new_project (test_database_integration.DatabaseIntegrationTests.test_edit_that_moves_note_invalidates_old_and_new_project)`
- `test_event_duplicate_and_source_change_commit_atomically (test_database_integration.DatabaseIntegrationTests.test_event_duplicate_and_source_change_commit_atomically)`
- `test_exact_sql_query_excludes_completed_and_history_and_preserves_rows (test_database_integration.DatabaseIntegrationTests.test_exact_sql_query_excludes_completed_and_history_and_preserves_rows)`
- `test_hierarchical_processing_reuses_partials_and_preserves_generation_evidence (test_database_integration.DatabaseIntegrationTests.test_hierarchical_processing_reuses_partials_and_preserves_generation_evidence)`
- `test_natural_completion_persists_once_and_flows_into_memory_version (test_database_integration.DatabaseIntegrationTests.test_natural_completion_persists_once_and_flows_into_memory_version)`
- `test_natural_completion_rolls_back_task_audit_and_attempt_when_event_fails (test_database_integration.DatabaseIntegrationTests.test_natural_completion_rolls_back_task_audit_and_attempt_when_event_fails)`
- `test_new_event_during_snapshot_keeps_memory_dirty_and_retrieves_delta (test_database_integration.DatabaseIntegrationTests.test_new_event_during_snapshot_keeps_memory_dirty_and_retrieves_delta)`
- `test_new_telegram_edit_cannot_discard_manually_completed_task (test_database_integration.DatabaseIntegrationTests.test_new_telegram_edit_cannot_discard_manually_completed_task)`
- `test_original_sources_and_memory_history_are_immutable_in_database (test_database_integration.DatabaseIntegrationTests.test_original_sources_and_memory_history_are_immutable_in_database)`
- `test_previous_task_response_is_scoped_and_expired_context_is_ignored (test_database_integration.DatabaseIntegrationTests.test_previous_task_response_is_scoped_and_expired_context_is_ignored)`
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

## Historia y límites prácticos

**No se modificó ni reprocesó historia.** No hubo backfill, reprocesamiento de Sources, modificación de Tasks/Decisions/ProcessingRuns existentes ni regeneración de memorias. El contexto conversacional solo se guarda en nuevos ReasoningRuns y se lee mediante copias acotadas. No hay migración.

La elección entre estado exacto y exploración sigue siendo semántica y puede ser errónea si Claude interpreta mal una pregunta. Las listas exactas quedan protegidas por el renderer y la selección SQL. En exploración, la separación de inferencias y el reconocimiento de equivalencias entre una frase y una Task dependen de Claude; no se añadió un validador semántico paralelo.

El contexto puede expirar o recortarse; en ese caso un referente puede requerir aclaración. Las respuestas antiguas se conservan como snapshots: una consulta nueva genera una respuesta nueva contra SQL actual; un retry de la misma Source conserva su respuesta histórica cacheada.

No se ejecutaron llamadas pagadas de Claude en esta revisión. Los tests simulan sus planes/respuestas y comprueban qué evidencia y efectos admite el backend.

## Prueba manual posterior a un deploy autorizado

1. «Dame todos mis pendientes»: solo Tasks actuales agrupadas, cobertura explícita.
2. «Qué tengo que hacer en Catu»: mismo conjunto que /pendientes Catu, sin historia ni warnings semánticos.
3. «Qué decisiones tenemos de SIMA»: solo Decisions registradas.
4. «Dame mis proyectos de Laureate»: catálogo SQL.
5. «Resúmeme mis pendientes por prioridad»: análisis del conjunto SQL, sin añadir Tasks inferidas.
6. «Qué cosas podría estar olvidando»: inferencias separadas de Tasks oficiales.
7. Tras listar Catu: «Ya completé la de enviar el correo»: cierre inequívoco por ActionPlan V2.
8. Tras listar dos Tasks similares: «Ya hice esa»: aclaración, sin cierre automático.
9. Después del cierre: «Qué tengo pendiente de Catu»: Task cerrada ausente aunque la memoria siga en debounce.
10. «Qué temas sobre costos discutimos en SIMA»: exploración; warning solo si hay limitación material de búsqueda.

