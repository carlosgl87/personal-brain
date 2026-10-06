# Revisión de producción de Personal Brain

Revisión local del 5 de octubre de 2026. Se revisaron aplicación, modelos, validación, consultas, extracción corta y jerárquica, audio, memoria, tareas, migraciones, pruebas, dependencias y despliegue.

Se encontraron y corrigieron problemas que podían bloquear el despliegue o producir respuestas basadas en estado obsoleto. No se desplegó, no se hizo push, no se modificó la base de Railway y no se hicieron llamadas a Claude, Telegram ni OpenRouter. Los proveedores se simularon y el SQL se ejecutó en bases locales desechables.

## Hallazgos corregidos

| Prioridad | Problema | Corrección |
| --- | --- | --- |
| Crítica | 0009 generaba dos restricciones únicas con el mismo nombre. PostgreSQL rechazaba la migración. | Nombres distintos en modelo y migración nueva; fallo reproducido y migraciones ejecutadas después de corregirlo. 0001–0007 permanecen intactas. |
| Alta | Una memoria vacía podía limpiarse mientras entraba otro evento, perdiendo la actualización pendiente. | Bloqueo y recarga del estado; comparación de revisión y versión. Se conserva el cursor y los eventos no consumidos de batches vacíos. |
| Alta | Un fallo de modelo o contexto podía retrasar una solicitud nueva. Un fallo de unlock podía reportar como fallida una publicación ya confirmada. | Backoff condicionado al estado capturado; conexión invalidada si no libera el lock; una versión confirmada mantiene su resultado exitoso. |
| Alta | La hora inicial se reutilizaba durante llamadas lentas y para todos los proyectos del tick. El reintento podía estar vencido al crearse. | Hora actual por proyecto y al publicar o fallar. Reloj inyectable para pruebas. |
| Alta | Editar una nota para cambiarla de proyecto no invalidaba la memoria del proyecto anterior. | Todos los proyectos afectados se marcan en la misma transacción. Los deltas identifican fuentes reemplazadas y asociación actual sin copiar texto de otro proyecto. |
| Alta | Una nueva edición de Telegram podía ocultar tareas modificadas manualmente; la protección anterior cubría solo el reprocesamiento del mismo UUID. | Se bloquea la sustitución antes de pagar la extracción y durante las etapas jerárquicas. Se conserva la extracción vigente; la corrección debe ser una nota nueva. |
| Alta | El catálogo quedaba expuesto sin autenticación si se publicaba un dominio. | `/projects` y `/projects/{id}` requieren el Bearer token existente del bot, antes de consultar datos. |
| Alta | Un 429, 5xx o fallo de red de Telegram cerraba el receptor y podía agotar los reinicios. | Reintentos con espera acotada y offset conservado. Credenciales inválidas, conflicto de polling y webhook siguen deteniendo el receptor. |
| Media | El healthcheck declaraba listo un esquema sin actualizar. | Compara `alembic_version` con las cabeceras incluidas en esta versión; devuelve 503 ante esquema ausente o antiguo. |
| Media | Una brecha de revisiones podía etiquetar memoria obsoleta como limpia; el delta de tarea completada omitía su título. | Se considera también el cursor. Delta con título y fecha de finalización aunque ya no aparezca en pendientes. |
| Media | Grupos históricos idénticos de chunks podían compartir selección lógica y duplicar resultados. | Selección determinista de un solo grupo, ordenado por índice. Se conservan IDs, texto, offsets y vectores históricos. |
| Media | Embeddings aceptaban índices booleanos y números que desbordaban o desaparecían al guardarse como float32. | Índices enteros estrictos y representación float32 válida antes de insertar; chunks conservados ante fallo. |
| Media | Los esquemas podían acumular demasiadas propiedades opcionales anidadas. | Campos explícitos, incluidos vacíos o nulos; las cotas completas siguen validadas con Pydantic. [Referencia de Claude](https://platform.claude.com/docs/en/build-with-claude/structured-outputs). |
| Media | Un hijo que terminara antes de `terminate()` podía interrumpir el cierre de otros procesos. | Se tolera esa carrera, se detiene primero el receptor y se intenta cerrar cada hijo. Conexión invalidada ante unlock fallido. |
| Media | Rangos abiertos de dependencias podían cambiar comportamiento entre despliegues. | `constraints.txt` fija runtime y dependencias; Docker usa el mismo archivo. Resolución Linux x86_64/Python 3.13 comprobada. |
| Baja | Comandos con saltos de línea se interpretaron mal; nuevos estados de tareas se copiaban completos al trace. | Separación por whitespace y hashes/longitud para estado actual, antes y después. |

## Validación

- Suite original: 227 pruebas correctas. No ejecutaban las migraciones contra PostgreSQL.
- Suite final: 257 pruebas, incluidas 10 de integración SQL y migración. Proveedores simulados.
- Alembic normal: desde cero hasta 0009 en una base temporal vacía.
- Actualización 0007 → 0009 con datos: conserva 32 proyectos, fuente original, ID/offsets de chunk, vector y resultado parcial. Repetir `upgrade head` no duplica datos.
- SQL ejecutado: seed aditivo, idempotencia, run vigente, cambios manuales, notas trasladadas, deltas, commits/rollback, triggers de inmutabilidad, reutilización de partes, generaciones y versiones de vectores.
- `pip check`: sin dependencias rotas. `pip-audit`: sin vulnerabilidades conocidas reportadas para las versiones de runtime en esta consulta; no garantiza ausencia de vulnerabilidades desconocidas.
- Resolución de wheels de runtime para Linux x86_64 y Python 3.13: correcta. No se instaló esa resolución en el entorno de aplicación.
- Sintaxis, diff, exclusión de `.env` y revisión de credenciales en archivos modificados. No se cargó ni imprimió el `.env` real durante la auditoría.

Las bases usan PostgreSQL 18.3 embebido mediante PGlite y pgvector. Se ejecutaron ORM, transacciones y triggers. Su servidor de sockets comparte un backend; no acredita concurrencia real entre backends ni el comportamiento de Railway. La prueba de triggers captura excepciones dentro de PostgreSQL; el fixture desactiva prepared statements por sus limitaciones. [Referencia de PGlite](https://pglite.dev/docs/pglite-socket).

## Cambios para el operador

El catálogo HTTP requiere `Authorization: Bearer <TELEGRAM_BOT_TOKEN>`. No hay otra clave; el ejemplo PowerShell del README está actualizado. Las rutas de salud siguen disponibles para Railway.

Una edición de una nota con tareas modificadas manualmente se conserva como fuente, pero no reemplaza la extracción vigente. Envía la corrección como una nota nueva.

Con Project Memory habilitada, notas y cambios generan eventos; el worker consolida después del debounce. Actualizaciones y reconciliaciones consumen Claude al desplegar. No hubo backfill pagado durante la revisión. Cambiar embeddings conserva los vectores anteriores, pero requiere generar explícitamente los del nuevo modelo; los modelos no son intercambiables.

## Pendiente antes de producción

La validación local pasó. Esta versión todavía no se probó en Railway ni se construyó su contenedor: Docker no está instalado en este entorno.

1. Tener un respaldo verificable antes de migrar la base real. Los downgrades destructivos siguen deshabilitados; no usar `stamp` para ocultar errores.
2. Ejecutar las pruebas SQL en PostgreSQL nativo desechable con pgvector, preferentemente la misma versión de Railway. Comprobar exclusión de workers/polling entre dos conexiones nativas: PGlite no valida esa condición.
3. Construir y arrancar Docker; confirmar predeploy hasta 0009 y `/health/db` en 200. Mantener una réplica y el polling local detenido.
4. Probar nota, audio, `/pendientes SIMA`, tarea completada, pregunta y `/refrescar SIMA`. Verificar el modelo contratado de Claude y sus salidas estructuradas: los mocks no prueban su compatibilidad real ni la calidad de respuestas.
5. Medir consumo y latencia con el volumen real. La búsqueda vectorial es exacta y la recuperación tiene límites; no supone cobertura completa de las notas. Una interrupción conserva las etapas confirmadas; una llamada externa aún sin confirmar puede necesitar repetición.

Para reproducir SQL, usar `PERSONAL_BRAIN_TEST_DATABASE_URL` con una base desechable local ya migrada, cuyo nombre comience por `personal_brain_test_`. Para la actualización usar otra base **vacía** en `PERSONAL_BRAIN_MIGRATION_TEST_DATABASE_URL`. La prueba no elimina tablas y falla si ya existen. Sin esas variables, las pruebas de integración se omiten explícitamente.
