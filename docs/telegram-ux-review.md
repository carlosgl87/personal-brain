# Validación local de UX e ingesta — 6 de octubre de 2026

Se implementaron documentos TXT/MD y enrutamiento de intención sobre la arquitectura vigente. No hubo push, despliegue, migración productiva, modificación de claves ni backfill. Los proveedores se simularon; la base de Railway no se consultó.

## Diseño y persistencia

- Documento autenticado → validación → descarga oficial acotada → UTF-8 estricto → transacción con Source completo, DocumentAsset original inmutable y SourceProcessingJob único por fuente → recibo encolado.
- Worker con advisory lock de sesión por fuente, claim SKIP LOCKED y token por intento → preflight → index_source → process_source existente → completed → notificación de mejor esfuerzo. Una misma conexión ejecuta las transacciones por etapa; la notificación sucede después de liberar ownership.
- Caídas recuperan trabajos processing cuando el bloqueo está libre. Reintentos con backoff hasta seis intentos; reencolado manual protegido por el mismo bloqueo. Se conservan partes, chunks, vectores y extracciones confirmadas.
- Intención independiente del scope: comandos → reglas claras → clasificador estricto con confianza mínima 0.80 → consulta/nota/aclaración. Consultas simples del catálogo usan SQL. Las consultas no generan tareas, decisiones ni dirty de Project Memory.
- Caption explícito desconocido/ambiguo conserva proyecto nulo incluso si el contenido menciona uno conocido. Audio no pasa por el router de texto.

La migración aditiva `0010_documents_jobs` crea únicamente `document_assets`, `source_processing_jobs`, sus restricciones/índice y el trigger de original inmutable. 0001–0009 no se modificaron; downgrade deshabilitado. No hay nuevas dependencias ni API keys.

## Evidencia

Baseline: 257 pruebas, 10 SQL omitidas sin variables locales. Suite final: **305 pruebas, 303 ejecutadas correctamente y 2 omitidas porque requieren PostgreSQL nativo con backends independientes**. Los 48 casos nuevos incluyen integración SQL. Las pruebas anteriores se conservaron; el fixture antiguo que consideraba «que tengo pendiente de SIMA» una nota se adaptó a la intención de consulta requerida. Salud espera la nueva cabecera; el test aislado del worker de memoria desactiva el nuevo worker.

SQL verificado en PostgreSQL 18.3 embebido/PGlite con pgvector, exclusivamente localhost:

- Migraciones desde cero hasta 0010; upgrade de 0007 con proyectos, fuente, chunk, vector y parte histórica hasta 0010; repetir upgrade no duplica datos.
- Guardado atómico y rollback de fuente/asset ante fallo de cola; duplicado por update_id y nuevo update como otra entrada.
- Trigger bloqueando UPDATE/DELETE del documento original.
- Claim, token obsoleto, backoff, completado y ausencia de tareas/eventos al guardar una consulta.
- Worker con extracción real del código y modelo simulado: una sola extracción, dirty/evento tras éxito, notificación fallida sin reabrir el trabajo.
- Documento largo: fallo parcial, reencolado manual, reutilización de partes confirmadas, consolidación final y evidencia de tarea, conservando bytes/texto originales.

Comando final ejecutado (los dos servidores temporales ya estaban iniciados; la segunda base estaba vacía):

```powershell
$env:PERSONAL_BRAIN_TEST_DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1:55439/postgres?sslmode=disable'
$env:PERSONAL_BRAIN_MIGRATION_TEST_DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1:55440/postgres?sslmode=disable'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
```

`pip check` correcto, sintaxis de los 23 Python modificados/nuevos validada y `git diff --check` correcto. `.env` y `.venv` continúan ignorados; `.env` no está versionado ni se modificó.

## Límites pendientes

PGlite tiene un solo backend; su fixture reutiliza una conexión y desactiva prepared statements. No acredita exclusión entre sesiones independientes. Quedaron dos pruebas optativas para PostgreSQL nativo: advisory lock entre backends y dos workers procesando la misma fuente.

No se construyó el contenedor ni se probó con APIs reales. Después de autorización, verificar Telegram, calidad del clasificador, compatibilidad del modelo, latencia y consumo. Archivos completos pueden superar HIERARCHICAL_MAX_CHUNKS y quedar conservados sin extracción. La descarga todavía forma parte de la recepción. Las notificaciones no tienen garantía de entrega; una llamada externa interrumpida antes de confirmar su resultado puede repetirse.

Variables, captions, comandos y pasos manuales de Telegram están en el último apartado del README. Se espera autorización del usuario antes de publicar o desplegar.
