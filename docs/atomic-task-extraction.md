# Tareas con estados independientes

Las llamadas existentes de extracción a Claude reciben un criterio compartido de
granularidad: cada tarea representa una unidad de trabajo con estado independiente.
«Enviar correos y consolidar respuestas» genera dos pendientes; «Revisar costos y
presupuesto» conserva una sola revisión. La decisión es semántica, sin división
mecánica por conectores, pasos inventados ni otra llamada al proveedor.

El prompt incluye los cinco ejemplos de separación y los cinco que deben permanecer
juntos, así como condiciones temporales, dependencias en `description`, fechas por
acción y responsables distintos. Los títulos conservan contexto explícito suficiente
para entender cada acción. Las tareas separadas pueden usar la misma cita literal de
la fuente: compartir evidencia no las convierte en duplicadas.

La política se aplica en la extracción corta y por fragmentos. La consolidación
conserva unidades independientes y fusiona solo duplicados de una misma acción,
manteniendo `candidate_ids`, `dispositions` y enlaces de evidencia existentes.
El schema tiene descripciones más precisas; sus campos y tipos permanecen iguales.
`processing` y la extracción jerárquica siguen creando una fila `Task` por elemento
del resultado, con su responsable, fecha y descripción.

Versiones de prompts:

- Extracción corta: `work-extraction-v2-atomic-tasks`.
- Fragmentos: `meeting-part-v2-atomic-tasks`.
- Consolidación: `meeting-consolidation-v3-atomic-tasks`.

La nueva versión distingue los nuevos derivados y evita reutilizar parciales con el
prompt antiguo cuando se realiza una extracción nueva. No activa un reprocesamiento
de fuentes ya procesadas ni modifica tareas históricas. No agrega migraciones ni
dependencias formales entre tareas. Project Memory y natural task completion reciben
las filas independientes por sus mecanismos habituales.

Las pruebas verifican los diez ejemplos con proveedores simulados, fechas individuales
y compartidas, responsables distintos, descripciones de dependencia, evidencia,
persistencia corta/jerárquica y finalización de una tarea sin cerrar la otra. Validan
el contrato del modelo y la integración; no acreditan la precisión semántica de Claude
real. Un modelo aún puede interpretar mal una frase ambigua; se necesita una evaluación
con respuestas reales para medir esa calidad.
