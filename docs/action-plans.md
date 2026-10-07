# Action Plans — refactor local

Implementado para nuevas entradas. No se ejecutaron push, merge, deploy, seed, migraciones online ni conexiones a Railway. `.env`, secretos y migraciones anteriores permanecen sin cambios. No hubo backfill, reprocesamiento histórico ni modificaciones de registros existentes.

## Arquitectura

`mensaje → Source confirmada → contexto acotado → Claude → ActionPlan → validación → transacción → PostgreSQL → dirty/debounce de Project Memory`

Los comandos explícitos conservan prioridad. El texto natural y `/nota` pasan por el intérprete único; el router de intención y la completion independiente ya no participan en esa ruta. Nuevas Sources llevan `processing_schema=action-plan-v1` desde su creación, conservando el texto y el envelope recibidos. No se modifica metadata histórica para activar el flujo. Los duplicados nuevos reutilizan el ProcessingRun; los duplicados legacy conservan el comportamiento/resultado histórico sin activar un reprocesamiento.

El ActionPlan registra simultáneamente updates, tareas nuevas, completions, decisiones, preguntas y ambigüedades. Se conserva el formato plano `tasks`/`decisions` compatible con consumidores actuales; las completions viven en `completed_tasks`. El JSON completo y el resultado de ejecución quedan en `processing_runs.result`.

El catálogo contiene exclusivamente proyectos activos. Claude propone scope semántico; el backend valida pertenencia y revalida bajo lock que el proyecto siga activo. La caption explícita de un documento conserva autoridad. Scope de baja confianza queda sin resolver. Contexto separado del prompt: mensaje, fecha original, catálogo, hasta 100 tareas abiertas y 10 updates recientes. Si se omiten candidatos por presupuesto, se deshabilitan completions; no se asume que los candidatos restantes son exhaustivos.

Las completions requieren candidatos actuales, estado `performed`, confianza al menos 0.95 y proyecto resuelto con confianza al menos 0.90. Alternativas/ambigüedad material impiden completar. Se bloquean los orígenes y tareas, se comparan sus valores con el contexto y se vuelve a comprobar el conjunto de pendientes. El lock del proyecto protege también frente a inserciones concurrentes con FK mientras se ejecuta. Cada cambio crea TaskChange `natural_completion`, before/after y evento de memoria independiente. La restricción `(command_source_id, task_id)` impide repetir el mismo cambio desde una Source.

Toda la persistencia final de un plan comparte transacción: ProcessingRun, updates, tareas, decisiones, evidence, TaskChanges y eventos. La Source ya existía antes de llamar a Claude; un fallo conserva el original y revierte acciones. Retries usan el resultado confirmado. Un fallo de proveedor antes del commit puede requerir otra llamada.

## Consultas, audio y documentos

Una consulta natural usa el retrieval del mismo ActionPlan, sin clasificador ni planner adicional. En mensajes mixed primero se confirma la transacción y después se responde con el estado actualizado. `ReasoningRun` conserva plan, trazas y respuesta; un retry reutiliza la respuesta. Una query pura no crea acciones ni se incorpora como evidencia de proyecto; el filtro se basa en el JSON del run, sin cambiar su Source original.

Reasoning recupera ProjectUpdates vigentes, source_id, fecha y un excerpt del original. El update se etiqueta como derivado. Los prompts distinguen hechos derivados de citas literales y mantienen Sources/chunks para detalle. Updates participan del presupuesto y las trazas guardan hashes del contenido. La consulta recupera como máximo 10/20/30 updates según profundidad, compartiendo los límites estructurados existentes.

Audio conserva el archivo original y crea una transcription Source; las transcripciones nuevas llevan el marcador y utilizan el mismo intérprete, sin exact matching previo. Se mantiene la transcripción de OpenRouter. `--process` sigue controlando el procesamiento de audio; el texto natural ahora se interpreta dentro de su recepción.

Los documentos largos mantienen chunks, parciales persistidos, generaciones, retries y consolidación. Parciales nuevos incluyen updates; el final devuelve ConsolidatedActionPlan. Todos los candidatos necesitan dispositions y referencias de su categoría. Cada update final conserva evidence, offsets, chunk, processing_run_part y generation_part cuando corresponde. El final se confirma atómicamente con las demás acciones. Los presupuestos fallan conservando parciales, sin truncar la fuente.

Se preservan `/pendientes`, `/decisiones`, `/resumen`, `/estado`, `/completar`, `/reabrir`, `/fecha`, `/responsable`, `/proyecto`, `/refrescar`, `/procesamiento`, `/nota`, `/ask` y `/pregunta`. `/ask` y `/pregunta` mantienen su planner determinístico como fallback explícito. El procesamiento legacy permanece disponible para fuentes sin marcador y reprocesamientos explícitos existentes; ninguna fuente anterior se convierte automáticamente al esquema nuevo.

Si en el futuro se solicita expresamente reinterpretar una fuente legacy con el sistema nuevo, existe `python -m app.process --source-id UUID --reprocess --action-plan`, o `POST /sources/UUID/process?force=true&action_plan=true`. Exige una fuente concreta y reprocesamiento explícito; no cambia `raw_content`, `raw_metadata` ni runs previos. Para documentos usa un snapshot temporal con la selección del intérprete. Se mantienen los bloqueos frente a tareas ya editadas. **Esta opción no se ejecutó sobre datos reales durante este trabajo.**

## Project Memory

Project Memory sigue siendo estado actual compacto y versionado, no historia acumulada. El snapshot incorpora como máximo 40 updates relevantes, además de Sources, tareas, decisiones y eventos. Incrementales con fuentes identificadas acotan los updates a esas fuentes; reconciliación usa una selección reciente. El prompt ya existente integra novedades, elimina vigencia de hechos obsoletos y respeta el límite de salida. Los updates históricos siguen en PostgreSQL.

Nuevos updates generan evento `project_update`; tareas/decisiones invalidan memoria a través del evento del run y cada completion genera su propio `task_change`. `origin_key` conserva idempotencia. El delta de memoria expone múltiples TaskChanges de una misma Source durante el debounce. No se añadió cron obligatorio ni regeneración automática de versiones anteriores.

## DB y versiones

Nueva revisión: `0012_action_plans`, después de `0011_task_completion_attempts`.

- `project_updates`: UUID, created_at, project_id nullable, source_id, processing_run_id, update_text y event_at nullable; FKs e índices.
- `update_evidence`: reutiliza EvidenceFields, con offsets válidos, FKs y unicidad de update/chunk/offsets.
- Triggers de inmutabilidad para las dos tablas nuevas.
- Sustituye únicamente `uq_task_changes_command_source_id` por `uq_task_changes_command_source_id_task_id`.
- Downgrade destructivo deshabilitado. No contiene DML histórico ni backfill; el UPDATE de `alembic_version` es el cursor estándar de Alembic.

Schema `action-plan-v1`; prompt corto `message-interpreter-v1`; parciales `action-plan-part-v1`; final largo `action-plan-consolidation-v1`. Runs y JSON antiguos siguen legibles sin transformación. `task_completion_attempts` permanece intacta y no recibe registros del flujo nuevo.

Texto corto: una llamada principal de interpretación. Una query/mixed con evidencia añade una llamada de síntesis; sin evidencia no la añade. Memoria e indexación siguen sus mecanismos/configuración existentes y pueden generar llamadas posteriores independientes. Audio agrega la transcripción. Documentos largos usan una llamada por parcial nuevo y una consolidación final, reutilizando parciales confirmados.

## Archivos

Nuevos: `app/models/project_update.py`, `app/schemas/action_plan.py`, `app/services/action_context.py`, `app/services/action_execution.py`, `app/services/message_interpreter.py`, `app/services/message_handling.py`, `migrations/versions/0012_action_plans.py`, `tests/test_action_plans.py` y este reporte.

Modificados, agrupados por responsabilidad:

- Entradas/UX: `app/api/routes/telegram.py`, `app/api/routes/processing.py`, `app/telegram.py`, `app/source_processing_worker.py`, `app/services/telegram_ingestion.py`, `audio.py`, `documents.py`, `file_ingestion.py`, `transcription.py`.
- Persistencia/esquemas: `app/models/__init__.py`, `task_change.py`, `app/schemas/extraction.py`, `hierarchical.py`, `reasoning.py`.
- Procesamiento: `app/process.py`, `app/services/processing.py`, `hierarchical.py`, `hierarchical_llm.py`, `task_management.py`, `task_completion.py`.
- Retrieval/memoria: `app/services/queries.py`, `reasoning.py`, `reasoning_llm.py`, `retrieval_budget.py`, `project_memory.py`, `project_memory_llm.py`.
- Regresión: `tests/test_audio.py`, `test_database_integration.py`, `test_documents.py`, `test_final_evolution.py`, `test_hierarchical.py`, `test_intent_router.py`, `test_memory.py`, `test_migration_upgrade.py`, `test_phase1.py`, `test_project_memory.py`, `test_reasoning.py`, `test_task_completion.py`, `test_telegram.py`; `README.md` apunta a esta arquitectura vigente.

Legacy conservado: `intent_router.py`, `task_completion.py`, extractor corto de `claude.py`, prompts históricos de consolidación y tabla `task_completion_attempts`. Se reutilizan auditoría, consultas, chunks, generaciones, dirty/debounce y versionado. No se eliminan las pruebas unitarias de esos módulos históricos.

## Validación local

Ejecutada con `.venv\Scripts\python.exe`, equivalente a activar el entorno y usar `python`:

- `python -B -m unittest discover -s tests -v`: **373 pruebas, 349 aprobadas, 24 omitidas, cero fallos**.
- `python -m pip check`: **No broken requirements found**.
- `git diff --check`: aprobado.
- Syntax AST de aplicación, migraciones y tests: aprobado.
- Alembic offline `0011_task_completion_attempts:0012_action_plans`: aprobado; constraints, índices y ausencia de DML histórico comprobados.

Se agregaron pruebas de Dashboard, Catu, enviar vs consolidar, dos completions, futuro/negación/pendiente/mención, decisión vs propuesta, mixed después del commit, scope semántico/ambiguo, UUIDs/evidence inválidos, rollback, candidatos concurrentes, presupuestos, query pura, replay legacy, updates dirty, provenance de generaciones, pipeline largo completo, audio nuevo y memoria compacta con múltiples cambios.

Los proveedores se simularon: estas pruebas verifican contratos y ejecución, no certifican la calidad semántica de Claude real.

No hubo upgrade online: no están configuradas `PERSONAL_BRAIN_TEST_DATABASE_URL` ni `PERSONAL_BRAIN_MIGRATION_TEST_DATABASE_URL`; no hay `psql`, `pg_ctl` ni Docker disponibles. Nunca se utilizó la conexión de Railway como sustituto.

Suites SQL omitidas exactamente:

- `tests/test_database_integration.py`: sus 23 tests requieren PostgreSQL local desechable explícitamente configurada. Incluyen las nuevas pruebas `test_action_plan_multiple_changes_and_updates_commit_once` y `test_updates_and_evidence_foreign_keys_offsets_and_immutability`, además de la suite previa de auditoría, originales inmutables, eventos, versiones, vectors, jerarquía, documentos y cola/concurrencia.
- `tests/test_migration_upgrade.py`: `test_upgrade_preserves_existing_sources_chunks_vectors_and_partials`, requiere otra base local vacía. Ahora construye filas legacy hasta 0011 y compara Sources, Tasks, Decisions, ProcessingRuns, partes, TaskChanges, attempts y memorias antes/después de 0012; comprueba que no haya updates/evidence backfilled.

## Límites y siguientes comprobaciones

La interpretación semántica continúa dependiendo del modelo; no se añadió otro sistema de regex ni un segundo LLM para juzgarla. Casos reales ambiguos, negaciones complejas y coreference necesitan evaluación end-to-end con Claude. El catálogo y las tareas están acotados: con más de 100 candidatos o contexto incompleto se conservan datos, pero se evita completar. Un catálogo mayor de 100 proyectos requiere retrieval adicional. Los hechos quedan sin proyecto cuando no hay scope seguro. El plan tiene un scope principal; no aplica completions entre proyectos diferentes en una sola nota.

La suite SQL y los locks reales siguen pendientes de validación en PostgreSQL local desechable. La migración se preparó y compiló, no se aplicó. Los recibos de Telegram son de mejor esfuerzo; un fallo de notificación no revierte datos confirmados. El uso de memoria tiene cobertura acotada y no representa toda la historia. El modo nuevo no convierte fuentes legacy; cualquier transición de reprocesamiento histórico debe pedirse explícitamente.

## Prueba manual de Telegram, después de un despliegue autorizado

Antes de probar completions, crear/verificar tareas abiertas inequívocas con esos títulos; comparar luego `/pendientes` y `/decisiones`.

1. `Ya tuvimos la reunión con Cobranzas. Los datos cuadraron y las líneas recomendadas les hicieron sentido. Mañana tengo que mandar los accesos.` — updates, presentar dashboard completed y enviar accesos nuevo; sin decision inventada.
2. `Para Catu quiero cambiar la validación. Si el ID no parece un número debe pedir DNI y nombre para saber si es vendedor o supervisor. Esto hay que implementarlo.` — requerimiento, comportamiento definido y task de implementación.
3. `Operating Model / McKinsey: ya envié los correos de validación a los responsables de IT.` — completa enviar; consolidar sigue open.
4. `Operating Model / McKinsey: ya envié los correos y terminé la presentación.` — dos completions auditadas desde una Source.
5. `Operating Model / McKinsey: mañana enviaré los correos.` — ninguna completion.
6. `Operating Model / McKinsey: no pude enviar los correos.` — update posible, ninguna completion.
7. `Catu: evaluamos ambas opciones y decidimos usar Databricks.` — decision; compararlo con `Catu: tal vez deberíamos usar Databricks.` sin decision.
8. `Ya envié los correos de IT. ¿Qué me queda pendiente de Operating Model / McKinsey?` — completion y respuesta posterior al cambio.
9. `McKinsey FrontRunner: revisar la factura.` — scope FrontRunner; compararlo con `Hablé con McKinsey. Hay cambios.` sin adivinar proyecto.
10. `¿Qué pasó en la última reunión de Cobranzas y cuándo validaron los datos?` — respuesta grounded en updates y originales; repetir por audio para verificar convergencia.
