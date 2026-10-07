# Finalización natural de tareas por Telegram

Una nota de texto como «Ya presenté el dashboard de cobranzas al equipo de créditos
de Catusita» se conserva primero como `Source`. Después puede completar una tarea
existente y continúa por la indexación y extracción habituales, incluido el resto de
la nota y sus nuevos pendientes. Los comandos explícitos conservan prioridad.

## Resolución y validación

Un filtro local busca señales afirmativas de ejecución pasada. Futuro, preparación,
pendiente, negación, condiciones y citas no activan el matching en sus cláusulas.
Este filtro no compara títulos: la equivalencia de acción, objeto, equipo y contexto
se evalúa mediante `call_json` de Claude con salida estructurada.

El catálogo determinístico existente resuelve el proyecto y el contexto de empresa
y área. Solo se proporcionan tareas vigentes `open` o `in_progress`, sin fecha de
finalización, dentro del alcance identificado. Si no se identifica contexto, la
búsqueda puede ser global. Un alcance ambiguo o más de 50 candidatas impide completar;
no se ocultan alternativas mediante un corte de resultados. No se envía el historial.

La aplicación exige un ID perteneciente a las candidatas, evidencia literal dentro
de una cláusula afirmativa, motivo `completed` y confianza mínima 0.95. Otra alternativa
con confianza >= 0.60 impide modificar y produce opciones legibles. Confianza baja o
ausencia de match conserva la nota sin editar tareas. Estos valores son puntuaciones
del modelo, no probabilidades calibradas.

Antes de escribir se bloquean la fuente original de la tarea y la tarea seleccionada,
y se vuelve a comprobar vigencia, estado y datos del conjunto candidato. Cambios
detectados durante la llamada cancelan la finalización. No se intenta completar más
de una tarea por nota.

## Auditoría, idempotencia y Project Memory

`TaskChange` conserva `before`, `after`, timestamp, fuente de la nota mediante
`command_source_id`, y `action=natural_completion`. Se reutiliza `record_task_change`
para los comandos explícitos y la finalización natural. La mutación, auditoría,
evento de memoria y resultado del intento se confirman en una misma transacción.
La nota ya guardada sobrevive si esa transacción falla.

`TaskCompletionAttempt`, único por `source_id`, almacena la propuesta, IDs candidatos,
resultado y recibo, incluso cuando no cambia ninguna tarea. Un duplicado reutiliza
ese resultado y no vuelve a decidir sobre tareas nuevas. Los fallos del proveedor
también se conservan: para volver a evaluar se necesita una nueva nota o usar el
comando explícito. La tarea completada deja de ser candidata en futuras notas.

`mark_dirty` genera el evento habitual `task_change` con las referencias de tarea,
fuente original y nota. La capa de delta expone la acción y el estado actual antes
del refresh; el snapshot y las versiones posteriores usan ese mismo evento.

La migración aditiva `0011_task_completion_attempts` crea solo la tabla de intentos
y su trigger inmutable. No modifica tareas, fuentes, `TaskChange` ni historia previa.
Debe aplicarse en un entorno autorizado antes de activar esta versión del backend.

## Alcance y pruebas

La función nueva se ejecuta en notas de texto autenticadas de Telegram. Documentos
y audios mantienen su procesamiento existente y no activan este mecanismo. Las
frases sin contexto suficiente pueden requerir una nueva nota con proyecto y acción;
no se implementó un diálogo para resolver referencias como «esa tarea» entre mensajes.
Las notas mayores de 10 000 caracteres conservan su procesamiento habitual pero no
se evalúan para completar automáticamente.

Las pruebas simulan Claude para comprobar contrato, paráfrasis, rechazos, scope,
ambigüedad, idempotencia, cambios durante la llamada, auditoría, delta, versiones de
memoria y compatibilidad con extracción. Las pruebas optativas de PostgreSQL usan
exclusivamente una base local desechable y verifican persistencia y rollback. La
calidad semántica y la latencia con un modelo real requieren una evaluación posterior.

## Ejemplos

- `Ya presenté el dashboard de cobranzas al equipo de créditos de Catusita.`
- `Ya tuve la reunión para mostrarle la nueva pestaña al equipo de créditos de Catusita.`
- `Ya terminé la presentación de la nueva pestaña de cobranzas.`
- `Ya presenté el dashboard al equipo de créditos de Catusita. Marca esa tarea como completada.`
- `Ya presenté el dashboard al equipo de créditos de Catusita. Me pidieron agregar un filtro por cliente para el jueves.`

Si existe un único match validado, Telegram añade `✅ Tarea completada: <título>` al
recibo normal. Si hay alternativas, presenta sus títulos y solicita precisar cuál.
