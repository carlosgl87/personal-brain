
# Personal Brain

Base de un asistente para organizar información de trabajo. Incluye FASE 1 (catálogo), FASE 2 (texto de Telegram) , FASE 3 (extracción con Claude), FASE 4 (consultas SQL por Telegram) y FASE 5 (audio y transcripción con OpenRouter). No incluye RAG, embeddings, recordatorios ni frontend.

FastAPI → SQLAlchemy 2 → PostgreSQL. Alembic administra el esquema; el seed administra los datos iniciales por separado. No se crean tablas al iniciar la aplicación.

El catálogo contiene 2 áreas (Laureate y Consultora), 4 categorías, 4 empresas, 32 proyectos y 79 aliases. No crea el área Personal. Las áreas, categorías, empresas y estados son extensibles.

## Inicio en Windows / PowerShell

Desde la raíz del repositorio, con Python 3.11 o superior:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

Si PowerShell bloquea la activación, puedes usar directamente ` .\.venv\Scripts\python.exe` en lugar de `python`, y `.\.venv\Scripts\alembic.exe` / `.\.venv\Scripts\uvicorn.exe`.

Conserva tu archivo `.env` existente. Si comienzas desde un clon nuevo, copia `.env.example` a `.env` y completa sus variables localmente. **Nunca subas .env a Git**, ni imprimas sus valores. El Dockerfile tampoco lo incluye.

La aplicación resuelve una sola conexión: `DATABASE_URL` si tiene valor; en caso contrario `DATABASE_PUBLIC_URL`. En tu laptop usa la conexión pública de Railway, dejando `DATABASE_URL` ausente o vacía. Dentro de Railway usa `DATABASE_URL` con la conexión interna. El driver utilizado es psycopg 3. Las credenciales de Telegram solo son necesarias al usar FASE 2. Las de LLM se utilizan solo al activar FASE 3.

Ejecuta primero las pruebas sin DB y después la migración y el seed:

```powershell
python -m unittest discover -s tests -v
alembic upgrade head
python -m app.seed
python -m app.seed
uvicorn app.main:app --reload
```

La migración inicial es aditiva y transaccional en PostgreSQL. Si encuentra tablas preexistentes incompatibles, se detiene: no borres tablas ni uses `stamp` para ocultar el conflicto. Revisa el esquema antes de continuar. Los downgrades destructivos están deshabilitados.

El seed usa claves estables y `ON CONFLICT DO NOTHING`, en una única transacción. Repetirlo no duplica registros; permite agregar proyectos y aliases nuevos. No elimina ni modifica registros existentes. Los slugs están declarados en `app/seed_data.json`: conserva un slug cuando cambies un nombre. Los renombres de registros ya existentes requieren un cambio explícito separado.

## Probar la API

En otra terminal PowerShell:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/health'
Invoke-RestMethod 'http://127.0.0.1:8000/health/db'
$projects = @(Invoke-RestMethod 'http://127.0.0.1:8000/projects' | ForEach-Object { $_ })
$projects.Count
Invoke-RestMethod 'http://127.0.0.1:8000/projects?area=Laureate'
Invoke-RestMethod 'http://127.0.0.1:8000/projects?area=Laureate&category=Student%20Ecosystem'
Invoke-RestMethod 'http://127.0.0.1:8000/projects?area=Consultora&company=Catusita'
Invoke-RestMethod 'http://127.0.0.1:8000/projects?status=active'
$projectId = [guid]$projects[0].id
Invoke-RestMethod ("http://127.0.0.1:8000/projects/" + $projectId)
Invoke-RestMethod 'http://127.0.0.1:8000/projects?area=Personal'
git check-ignore .env
git ls-files -- .env
git status --short
```

En un catálogo inicial limpio deben aparecer 32 proyectos, 26 de Laureate, 6 de Consultora y 3 de Catusita. Personal devuelve una lista vacía. `git ls-files -- .env` no debe devolver nada.

`/health` responde `{"status":"ok"}` sin necesitar DB. `/health/db` ejecuta `SELECT 1`; devuelve 503 genérico si falla. Los filtros aceptan nombre (sin distinguir mayúsculas) o slug y se combinan. El detalle incluye área, categoría, empresa y aliases. UUID inválido devuelve 422; proyecto inexistente, 404. Documentación interactiva: http://127.0.0.1:8000/docs.

Los endpoints de lectura del catálogo no tienen autenticación. Úsala localmente o en una red privada; el catálogo de trabajo no debe exponerse públicamente sin protección.

## Datos y fuentes originales

Las ocho tablas iniciales usan UUID y timestamps con zona horaria: `areas`, `categories`, `companies`, `projects`, `project_aliases`, `sources`, `tasks`, `decisions`. Las FK preservan referencias; una categoría debe pertenecer al área del proyecto. No se impone una regla específica de Laureate/Consultora que bloquee áreas futuras.

Los aliases exactos son únicos dentro de un proyecto. Variantes como ALMA/Alma y Trámites/Tramites se conservan con su misma forma normalizada (minúsculas, sin tildes y separadores simplificados). Esto permite búsqueda futura sin perder las variantes solicitadas.

`sources.raw_content` y `raw_metadata` conservan la fuente original. Un trigger bloquea su modificación, la modificación de su procedencia y fecha de recepción, y el borrado de fuentes. Se puede actualizar la asociación a proyecto y el estado de procesamiento. Tasks y decisions son información derivada independiente; FASE 3 las completa y conserva el resultado de cada extracción en processing_runs. La recepción interna de Telegram de FASE 2 permite insertar fuentes; no hay endpoints generales de edición o borrado.

## Docker / Railway

```powershell
docker build -t personal-brain .
docker run --rm -p 8000:8000 --env-file .env personal-brain
```

El contenedor corre como usuario sin privilegios, escucha en `0.0.0.0` y respeta `PORT` (por defecto 8000). No ejecuta migraciones ni seed automáticamente. Antes de desplegar, ejecuta esos comandos desde un entorno autorizado con acceso a la base. Esta fase no despliega ni modifica Railway.

## Pruebas y mantenimiento

Las pruebas usan mocks, datos ficticios y compilación SQL PostgreSQL; no leen `.env`, no conectan a Railway, ni crean/borran tablas. No usan SQLite. La conexión real, los triggers y la idempotencia real en PostgreSQL se verifican al ejecutar el flujo local anterior.

Para cambios futuros de esquema, crea y revisa una migración Alembic. No uses `create_all`, DROP, TRUNCATE, resets ni tests destructivos contra producción.

## FASE 2: texto de Telegram

Flujo local: Telegram → receptor de polling → POST autenticado en FastAPI → guardar fuente original y asociar proyecto en PostgreSQL → confirmar en Telegram.

El receptor usa [getUpdates (long polling)](https://core.telegram.org/bots/api#getupdates), por lo que no necesitas dominio público, túnel ni webhook para esta prueba. FastAPI no inicia el bot automáticamente. Mantén una sola instancia del receptor por bot. Solo se admite chat privado del usuario configurado en TELEGRAM_USER_ID; otros usuarios, grupos y bots se ignoran sin guardarse ni responderles. FASE 5 también admite notas de voz y audio; otros archivos no se procesan.

### Ejecutar después de FASE 1

1. Detén el Uvicorn actual con Ctrl+C.
2. En esa terminal, desde la raíz y con el entorno activado:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
alembic upgrade head
uvicorn app.main:app --reload
```

No necesitas ejecutar el seed nuevamente. La migración 0002 agrega un índice único parcial para las fuentes de Telegram; no borra ni modifica fuentes ni proyectos. Si encuentra IDs de Telegram duplicados existentes, falla y conserva los datos: revisa el problema en lugar de borrar registros.

3. En una segunda terminal, desde la misma raíz:

```powershell
.\.venv\Scripts\Activate.ps1
python -m app.telegram
```

El programa lee TELEGRAM_BOT_TOKEN y TELEGRAM_USER_ID de tu .env existente. No pases estos valores por consola ni los copies en una URL. No necesita LLM_API_KEY. Para un puerto local diferente, usa `python -m app.telegram --port 8001`.

4. Desde tu usuario autorizado, abre el chat privado con el bot y envía, por ejemplo:

```text
SIMA: revisar el avance del proyecto el viernes.
```

Debes recibir `Nota guardada: SIMA.` y el UUID de la fuente. La confirmación se envía después del commit. Un mensaje como `Nota general: revisar pendientes.` se guarda sin proyecto. Un mensaje que mencione SIMA e Inventarios también se guarda sin proyecto porque es ambiguo. Editar un texto crea otra fuente con los datos de esa edición; nunca reemplaza la fuente previa. A partir de FASE 4, /start y /ayuda responden ayuda; las consultas se conservan por separado y no se extraen como tareas.

Puedes detener el receptor con Ctrl+C y reiniciarlo. Si FastAPI no confirma el guardado, el receptor reintenta sin confirmar el update a Telegram. Los duplicados se reconocen por external_source y external_id (update_id), incluso ante solicitudes concurrentes. No se sobrescribe el original. Las confirmaciones en Telegram son de mejor esfuerzo: un fallo al enviarlas no revierte una nota ya guardada ni garantiza una nueva confirmación tras reiniciar.

### Asociación y conservación

Solo se comparan nombres, slugs y aliases de proyectos activos existentes, por palabras completas y con la normalización de FASE 1. No se hace fuzzy matching ni se crean proyectos. Una única coincidencia permite asociar; cero o varios proyectos deja primary_project_id vacío. No se infiere un proyecto solo por mencionar una empresa.

Se guarda el texto exacto en sources.raw_content y el update completo (IDs de usuario, chat, mensaje, fechas y datos de edición) en raw_metadata. processing_status queda pending y processed_at vacío para una futura fase de IA.

POST /telegram/updates es un endpoint interno autenticado mediante Bearer con el token del bot, enviado solo en un header por el receptor. No es un webhook público de Telegram. Mantén FastAPI en localhost para este flujo. Si falta configuración devuelve 503; sin autenticación válida devuelve 401; updates no admitidos responden ignored sin insertar datos.

Antes de iniciar, el receptor consulta getWebhookInfo. Si hay un webhook activo se detiene sin eliminarlo ni descartar mensajes pendientes. Si hay otro polling, Telegram rechaza la segunda instancia y se informa un error seguro. Los errores y logs no imprimen tokens, IDs configurados, textos recibidos ni respuestas externas completas.

No se desplegó en Railway ni se configuró un webhook. El Dockerfile continúa iniciando únicamente FastAPI. La persistencia real de esta fase queda por verificar con el flujo anterior; las pruebas automatizadas usan mocks y transporte HTTP simulado, sin escribir en producción.

## FASE 3: extracción con Claude

Se usa la API Messages de Anthropic con [salida JSON estructurada](https://platform.claude.com/docs/en/build-with-claude/structured-outputs). La aplicación valida el resultado con Pydantic antes de persistirlo. HTTPX ya forma parte de las dependencias; no se requiere otro SDK.

Configura LLM_API_KEY en tu .env existente (ya previsto desde FASE 1), LLM_PROVIDER=anthropic y LLM_MODEL con un modelo que soporte structured outputs. No muestres ni copies el valor de la key por consola. El siguiente ejemplo selecciona [claude-sonnet-5-5](https://platform.claude.com/docs/en/models/overview) en variables de entorno de cada terminal; no modifica .env. Si ya elegiste otro modelo compatible, usa ese mismo ID en ambas terminales.

### Activar el flujo completo

Detén con Ctrl+C el receptor Telegram y Uvicorn. En la primera terminal, desde la raíz:

```powershell
.\.venv\Scripts\Activate.ps1
$env:LLM_PROVIDER = 'anthropic'
$env:LLM_MODEL = 'claude-sonnet-5-5'
python -m unittest discover -s tests -v
alembic upgrade head
uvicorn app.main:app --reload
```

En la segunda terminal:

```powershell
.\.venv\Scripts\Activate.ps1
$env:LLM_PROVIDER = 'anthropic'
$env:LLM_MODEL = 'claude-sonnet-5-5'
python -m app.telegram --process
```

Mantén una sola instancia del receptor. Sin --process, sigue disponible el comportamiento de FASE 2. No necesitas repetir el seed. Reinicia Uvicorn si cambias configuración, ya que Settings se almacena en caché.

Envía desde tu cuenta autorizada:

```text
SIMA: Ana enviará el informe el 9 de octubre de 2026 a las 17:00, hora de Lima. Decidimos usar PostgreSQL.
```

La respuesta debe confirmar la fuente guardada e incluir el número de tareas, decisiones y un resumen. El guardado de la fuente se confirma antes de solicitar el procesamiento. Si falla Claude, la respuesta indica procesamiento pendiente y la fuente permanece intacta.

Activar --process envía el texto original y el catálogo de proyectos a Anthropic y consume tu API. No se envían tokens de Telegram ni las claves de configuración dentro del prompt. Las pruebas automatizadas no realizan estas llamadas reales.

### Procesar pendientes y reprocesar

Con Uvicorn y el receptor detenidos puedes procesar hasta diez fuentes pending o failed desde una terminal con el mismo entorno:

```powershell
python -m app.process --limit 10
```

Para una fuente concreta, usa el UUID que recibió tu bot:

```powershell
$sourceId = Read-Host 'UUID de la fuente'
python -m app.process --source-id $sourceId
```

Repetir ese comando devuelve already_processed sin llamar a Claude ni duplicar tareas. Para reprocesar de forma explícita:

```powershell
python -m app.process --source-id $sourceId --reprocess
```

El reprocesamiento genera una versión nueva y conserva resultados, tareas y decisiones anteriores. Las tareas previas mantienen sus estados y responsables. La fuente apunta a la última extracción exitosa mediante latest_processing_run_id. Las consultas de una fase futura deberán seleccionar la versión vigente y tratar las filas históricas como historia, para evitar contar tareas de distintas versiones como pendientes nuevos.

### Persistencia, controles y límites

La migración 0003 agrega processing_runs y referencias nullable en sources, tasks y decisions. No elimina ni modifica contenido existente. Cada ejecución exitosa guarda provider, model, prompt_version y el resultado JSON completo: proyecto, resumen, tareas, decisiones, personas, fechas, follow-ups y tags. Personas, fechas, follow-ups y tags se conservan en el JSON; todavía no se crean catálogos adicionales ni recordatorios.

Las tareas y decisiones nuevas apuntan tanto a su source como a su processing_run. Se guardan en una única transacción con el resultado y el estado processed. Un bloqueo de fila serializa el procesamiento de la misma fuente, evitando duplicados concurrentes. Por simplicidad, el bloqueo se mantiene durante la llamada a Claude; no hay colas ni servicios adicionales.

Una salida truncada, un rechazo, JSON inválido, fechas sin zona, citas de evidencia ausentes de la fuente o un proyecto inexistente no se guardan como extracción válida. La asociación existente se conserva. Para asociar una fuente previamente sin proyecto, la propuesta de Claude también debe tener una coincidencia inequívoca de nombre/alias. Nunca crea proyectos ni resuelve arbitrariamente fuentes ambiguas.

Si falla la extracción inicial, processing_status pasa a failed para reintentar; si falla un reprocesamiento, la extracción exitosa anterior continúa vigente. Los errores de persistencia revierten la transacción. Ninguno de estos caminos cambia raw_content o raw_metadata. Las fechas derivadas se guardan con zona horaria; el prompt usa America/Lima y la fecha original del mensaje como referencia.

El endpoint interno POST /sources/{source_id}/process usa la misma autenticación Bearer que la recepción de Telegram. force=true solicita reprocesar de forma explícita. No hay endpoints de escritura sin autenticación para este flujo. FASE 4 agrega consultas por Telegram y FASE 5 admite audio; embeddings, RAG, agentes y recordatorios siguen fuera del alcance.

Las pruebas cubren respuestas simuladas, rechazo de datos inválidos, idempotencia, historial y compilación offline de migraciones. La migración y la extracción real se validan con los comandos anteriores. No se despliega en Railway automáticamente.

## FASE 4: consultas por Telegram con SQL

El mismo bot ahora responde consultas por proyecto, alias, empresa, área y responsable. No llama a Claude para interpretar o responder estas consultas: el reconocimiento de frases y comandos es determinista y la lectura se hace con SQLAlchemy/PostgreSQL.

No hay cambios de esquema ni dependencias nuevas en esta fase. Si FASE 3 ya funciona, no necesitas migración ni seed.

### Reiniciar y probar

Detén Uvicorn y el receptor con Ctrl+C. En la primera terminal, desde la raíz:

```powershell
.\.venv\Scripts\Activate.ps1
$env:LLM_PROVIDER = 'anthropic'
$env:LLM_MODEL = 'claude-sonnet-5-5'
python -m unittest discover -s tests -v
uvicorn app.main:app --reload
```

En la segunda terminal:

```powershell
.\.venv\Scripts\Activate.ps1
$env:LLM_PROVIDER = 'anthropic'
$env:LLM_MODEL = 'claude-sonnet-5-5'
python -m app.telegram --process
```

--process conserva la extracción de notas nuevas. Para usar solo captura y consultas, puedes ejecutar python -m app.telegram sin esa opción y sin configurar LLM_MODEL en esa terminal.

Desde tu usuario autorizado, prueba:

```text
¿Qué tengo pendiente de SIMA?
¿Qué decidimos sobre SIMA?
/pendientes Catusita
/pendientes con Ana
/pendientes SIMA con Ana
/resumen SIMA
/estado SIMA
/ayuda
```

Con la nota de prueba de FASE 3, la primera pregunta debe mostrar la tarea de Ana y la segunda la decisión sobre PostgreSQL. Las respuestas incluyen el UUID de la fuente. Catusita consulta sus proyectos; Ana filtra por owner_text, coincidiendo con el nombre completo registrado, sin distinguir mayúsculas o tildes.

### Cómo se reconocen las consultas

Los comandos estables son /pendientes, /decisiones, /resumen, /estado, /ayuda y /start. También se admiten las frases del ejemplo, preguntas como “¿Cuál es el estado actual del proyecto SIMA?” y “¿Qué pasó en las últimas reuniones de SIMA?”. Esta última muestra notas recientes; no identifica automáticamente cuáles fueron reuniones.

Los nombres y aliases deben coincidir completos. Los nombres ambiguos requieren aclaración. Puedes precisar /pendientes proyecto SIMA, /pendientes empresa Catusita o /pendientes area Laureate. No hay fuzzy matching ni creación de entidades.

Una pregunta no reconocida (comienza con ¿ o termina con ?) y los comandos desconocidos reciben ayuda. Para guardar una pregunta como nota, usa /nota seguido del texto. Ese texto completo, incluido el comando, se conserva como original.

Las consultas se guardan como sources de tipo telegram_query con processing_status=skipped, conservando texto y metadatos. No se extraen tareas de ellas ni aparecen entre los resúmenes de notas. El endpoint de procesamiento también rechaza una consulta, incluso con force=true. Reintentar un update reutiliza su fuente sin duplicarla y vuelve a consultar la información vigente.

### Información vigente y límites

Se leen las tareas open o in_progress que no tengan completed_at. Las tareas y decisiones extraídas deben pertenecer al latest_processing_run_id de su fuente; las versiones anteriores se conservan pero no se cuentan nuevamente. Las filas manuales sin processing_run_id siguen incluidas.

Cuando una edición posterior del mismo mensaje y chat fue procesada con éxito, sus datos sustituyen a los de la edición anterior en las consultas. No se borra ninguna fuente ni extracción. Si una nueva edición sigue pendiente o falló, la última versión procesada continúa disponible. La fecha de edición se usa para ordenar revisiones; el update_id solo desempata, porque Telegram puede reiniciarlo tras inactividad ([documentación](https://core.telegram.org/bots/api#update)).

Las consultas por proyecto/empresa/área no incluyen fuentes sin proyecto. /pendientes sin argumento incluye todos los proyectos y tareas sin proyecto. Una empresa sin tareas no provoca asociaciones inventadas. Las notas sin procesar todavía pueden contener pendientes que no aparecen en la lista SQL.

Se muestran hasta 20 tareas o decisiones; el bot avisa si hay más y sugiere acotar el alcance. /resumen muestra las últimas 5 notas con su resumen guardado o indica que no tienen extracción. /estado muestra el estado del catálogo y contadores registrados; no inventa un diagnóstico de avance.

Las fechas se presentan en America/Lima. Las respuestas largas se dividen en mensajes de hasta 3500 unidades UTF-16 para respetar el [límite de sendMessage](https://core.telegram.org/bots/api#sendmessage). Si falla el envío de una respuesta, el receptor se detiene sin avanzar el offset; al reiniciarlo puede repetir la consulta o una parte ya enviada.

La autorización sigue validándose en el receptor y en FastAPI: solo tu usuario en chat privado y el endpoint interno con Bearer válido. No se añade un endpoint público de consultas ni se imprimen secretos o contenido de mensajes en logs.

Las pruebas verifican autorización, consultas sin LLM, filtros, versiones vigentes, límites, referencias y conservación de la pregunta. Usan mocks y SQL PostgreSQL compilado; la ejecución real de las consultas se valida con los ejemplos anteriores.


## FASE 5: notas de voz y audio con OpenRouter

El bot recibe notas de voz y archivos de audio desde tu chat privado autorizado. Conserva los bytes originales en PostgreSQL (audio_assets), su SHA-256, caption y metadatos. Los triggers impiden modificar o borrar el original.

La transcripción crea otra fuente audio_transcript vinculada al audio. Claude extrae tareas y decisiones de ese texto; las consultas SQL evitan contar dos veces el audio y su transcripción. Las transcripciones existentes se reutilizan, incluso si fueron creadas con la versión local anterior.

Se utiliza [la API de transcripción de OpenRouter](https://openrouter.ai/blog/tutorials/transcription-on-openrouter/) con openai/whisper-large-v3-turbo y language=es. El archivo se envía en base64 por HTTPS usando OPENROUTER_API_KEY. No se envían la clave de Claude, los metadatos de Telegram ni el catálogo en esa solicitud. El texto transcrito se envía a Anthropic cuando activas --process.

### Configuración y prueba

Conserva LLM_API_KEY con tu clave de Claude. Agrega OPENROUTER_API_KEY en tu .env local. OPENROUTER_STT_MODEL es opcional y su valor predeterminado es openai/whisper-large-v3-turbo; STT_LANGUAGE es es. La antigua variable STT_MODEL ya no se utiliza.

No necesitas descargar modelos, instalar faster-whisper ni ejecutar --prepare-model. HTTPX ya está incluido en requirements.txt. El Dockerfile usa esas dependencias y no carga ningún modelo local. Las dependencias locales instaladas anteriormente pueden quedarse en tu entorno; la aplicación ya no las importa.

Detén Uvicorn y el receptor con Ctrl+C. Desde la raíz y con .venv activo:

```powershell
alembic upgrade head
uvicorn app.main:app --reload
```

La migración 0004 conserva los datos existentes; si ya la aplicaste, upgrade head no la repite. No ejecutes nuevamente el seed. Conserva la configuración LLM que ya funcionó y vuelve a establecer sus variables de terminal si abriste terminales nuevas.

En la segunda terminal:

```powershell
.\.venv\Scripts\Activate.ps1
python -m app.telegram --process
```

Envía una nota de voz corta: “SIMA. Ana enviará el informe mañana. Decidimos usar PostgreSQL.” La respuesta debe confirmar Audio original guardado, mostrar el UUID de la transcripción y el resultado de Claude. Luego prueba /pendientes SIMA y /decisiones SIMA. Revisa nombres y proyectos: el reconocimiento puede cometer errores.

### Persistencia, costos y recuperación

La fuente de transcripción guarda proveedor, modelo solicitado, idioma, hash del original y los campos numéricos de usage que devuelve OpenRouter, incluido cost cuando está presente. El proveedor interno lo selecciona OpenRouter. No se registra la respuesta completa ni la clave. Se necesita saldo y permisos para el modelo.

Para transcribir sin Claude o diagnosticar un fallo con un mensaje seguro:

```powershell
$sourceId = Read-Host 'UUID del audio original'
python -m app.transcribe --source-id $sourceId
```

Para transcribir y extraer o reintentar:

```powershell
python -m app.process --source-id $sourceId
```

Repetir el procesamiento exitoso reutiliza la transcripción. --reprocess crea otra versión de la extracción de Claude y conserva la transcripción. Un timeout remoto puede haber consumido saldo aunque no recibamos la respuesta; los reintentos explícitos pueden generar otro cargo.

Se mantienen los límites de 20 MiB y diez minutos. OpenRouter tiene además un timeout de procesamiento de aproximadamente 60 segundos; no equivale a 60 segundos de duración del audio. Esta fase no divide audios largos en segmentos. Los formatos admitidos incluyen OGG y WAV, pero pueden variar por proveedor; un fallo conserva el original y permite reintentar.

Sin --process solo se guarda el audio. Las consultas por voz se tratan como notas; usa comandos de texto para consultar. Documentos y videos no se procesan. Los audios que exceden los límites conservan únicamente su referencia y el bot lo informa.

Las pruebas usan una clave ficticia, transporte HTTP simulado y SQL offline. No consumen saldo ni leen .env. No se ejecutó la migración contra Railway ni una transcripción real: compruébalas con el flujo anterior. El contenedor está preparado, sin desplegar.

CIMA se registra como alias de SIMA para resolver esa variante de transcripcion. Ejecuta python -m app.seed para incorporarlo de forma aditiva. El texto original no se corrige ni se reemplaza. Una nota ya procesada necesita --reprocess para generar una nueva extraccion con el catalogo actualizado.


## Gestión de tareas desde Telegram

/pendientes muestra Tarea: UUID y Fuente: UUID. Usa el UUID de la tarea para editarla; no uses el UUID de la fuente ni números de posición de una lista.

Comandos explícitos (reemplaza UUID por el de tu tarea):

```text
/completar UUID
/reabrir UUID
/fecha UUID 2026-10-09 17:00
/fecha UUID sin fecha
/responsable UUID Ana Gómez
/responsable UUID sin asignar
/proyecto UUID SIMA
/proyecto UUID sin proyecto
```

Las fechas requieren AAAA-MM-DD HH:MM y se interpretan en America/Lima. Los proyectos y aliases deben coincidir exactamente con el catálogo; los nombres ambiguos se rechazan. /proyecto cambia solo el proyecto de la tarea seleccionada: no cambia la fuente, otras tareas, decisiones ni el resumen original.

Una tarea completada deja de aparecer en /pendientes. Conserva su UUID para /reabrir; todavía no hay una lista de tareas completadas. Reabrirla vuelve a estado open. La confirmación incluye el UUID y la acción aplicada. /ayuda muestra los comandos.

Los comandos requieren el mismo usuario y chat privado autorizado que las notas. Se guardan como fuentes telegram_query y no se extraen como notas. La tabla task_changes registra valores anteriores/nuevos y la fuente del comando. Un trigger impide modificar o borrar el historial. El cambio y su registro se confirman en la misma transacción. Repetir un update exitoso devuelve su respuesta guardada sin volver a aplicar la acción.

Solo se editan tareas de la versión vigente. Los registros históricos se conservan y no se editan con estos comandos. Los bloqueos de fila coordinan la edición con el procesamiento. Para proteger cambios manuales, --reprocess se rechaza si la fuente tiene tareas con historial de edición. Una nueva nota o edición de Telegram sigue siendo una fuente nueva y puede sustituir la nota anterior en las consultas; los cambios anteriores permanecen en el historial.

### Activar y probar

Detén Uvicorn y el receptor con Ctrl+C. Con .venv activo:

```powershell
alembic upgrade head
uvicorn app.main:app --reload
```

En la segunda terminal, conserva la configuración LLM que ya funcionó:

```powershell
.\.venv\Scripts\Activate.ps1
python -m app.telegram --process
```

No necesitas seed ni dependencias nuevas. La migración 0005 agrega task_changes y su trigger sin modificar tareas existentes. Prueba /pendientes SIMA, copia un UUID de Tarea, cambia responsable y fecha, completa la tarea y verifica que desaparece. Luego reábrela con ese mismo UUID y verifica que conserva los cambios. La confirmación se envía tras el commit; un fallo de envío no revierte el cambio.

Las pruebas verifican autorización, idempotencia, auditoría, fechas, proyectos, tareas vigentes y protección frente al reprocesamiento, usando mocks y migración SQL offline. No se aplicó la migración a Railway ni se desplegó. El siguiente paso, tras probar este flujo, es preparar el despliegue.


## Despliegue en Railway

railway.json configura un servicio con Dockerfile, pre-deploy alembic upgrade head, arranque python -m app.cloud, healthcheck /health/db y reinicios ante fallos. app.cloud supervisa FastAPI y Telegram: si un proceso falla, detiene el otro para que Railway reinicie el servicio. Ctrl+C/SIGTERM detiene ambos.

La API y el bot viven en el mismo contenedor y se comunican por localhost. No necesitas dominio público ni webhook. No habilites un dominio para el servicio: los endpoints de catálogo siguen sin autenticación. Usa una sola réplica y desactiva Serverless/suspensión automática. No ejecutes otro receptor local con el mismo token.

Un bloqueo advisory de PostgreSQL por bot impide dos receptores cloud simultáneos durante un despliegue. La API puede estar lista mientras el nuevo contenedor espera que el anterior libere el bloqueo. La conexión se verifica periódicamente; si falla, se detienen los procesos. El receptor local independiente no adquiere este bloqueo: detén python -m app.telegram antes de activar Railway.

### Variables del servicio

En el proyecto EXISTENTE de Railway que contiene tu PostgreSQL, crea un servicio de aplicación personal-brain. No crees otra base ni otro proyecto. Configura las variables en Variables del servicio; .env no se sube y los secretos nunca se incluyen en el código.

- DATABASE_URL: referencia a DATABASE_URL del servicio PostgreSQL existente, usando Add Reference; por ejemplo ${{Postgres.DATABASE_URL}} si se llama Postgres.
- TELEGRAM_BOT_TOKEN y TELEGRAM_USER_ID: tus valores configurados.
- LLM_API_KEY: tu clave de Anthropic.
- LLM_PROVIDER=anthropic.
- LLM_MODEL: el mismo modelo que ya probaste localmente.
- OPENROUTER_API_KEY: tu clave de OpenRouter.
- OPENROUTER_STT_MODEL=openai/whisper-large-v3-turbo (opcional).
- STT_LANGUAGE=es (opcional).

No necesitas DATABASE_PUBLIC_URL dentro de Railway, ni GPU, ni un volumen de modelo. Los audios y el historial permanecen en PostgreSQL. PORT lo proporciona Railway. Antes del primer despliegue revisa que la base tenga un backup reciente; la configuración de backups depende de tu plan.

### CLI y primer despliegue

Desde PowerShell, autentícate mediante el navegador, sin pegar tokens en la conversación:

```powershell
npx.cmd --yes @railway/cli login
npx.cmd --yes @railway/cli link
```

Selecciona tu proyecto y entorno existentes y el servicio personal-brain. Una vez configuradas sus variables y detenido el receptor local:

```powershell
npx.cmd --yes @railway/cli up --service personal-brain --detach
npx.cmd --yes @railway/cli status
```

up usa .gitignore; nunca uses --no-gitignore. No necesita un commit para cargar el código local. .dockerignore y las instrucciones COPY del Dockerfile también excluyen .env. No ejecutes railway variables sin un filtro seguro, porque puede mostrar valores.

En Railway verifica el build, la migración previa y el deployment. Los logs deben indicar API lista y Receptor Telegram iniciado. Un healthcheck exitoso confirma API y DB; no garantiza por sí solo que Telegram o las APIs externas estén funcionando.

Comprueba /pendientes SIMA y una nota de voz desde Telegram. Después reinicia el servicio y vuelve a consultar. Solo tras estas comprobaciones puedes cerrar las terminales locales y apagar la computadora. No ejecutes un seed adicional ni cambies el servicio PostgreSQL para desplegar.

Los fallos de transcripción o Claude conservan el original para reintentar con app.process. Todavía no hay una cola automática de recuperación de esos pendientes. Las confirmaciones de notas son de mejor esfuerzo; un reinicio puede repetir respuestas a consultas guardadas. La CLI carga código, pero no crea un flujo de autodeploy en GitHub: esa conexión se puede configurar posteriormente.


### Estado del primer despliegue

El servicio personal-brain quedó creado en el proyecto existente, sin dominio público y con Serverless desactivado. Se verificaron el arranque y un reinicio: API y receptor regresaron a estado activo. Las credenciales se cargaron desde el panel por el propietario, sin leer ni subir .env.

La carga inicial por CLI detectó railway.json, pero no aplicó correctamente el arranque. Se configuraron explícitamente en el servicio: startCommand=python -m app.cloud, preDeployCommand=alembic upgrade head, healthcheckPath=/health/db, una réplica, reinicio ON_FAILURE y los tiempos de apagado. Conserva esos ajustes en Settings al desplegar nuevamente.

La CLI actual advierte que railway.json sigue funcionando hasta el 1 de diciembre de 2026 y recomienda migrar a su formato Infrastructure as Code. Antes de esa fecha, revisar y migrar la configuración con railway config migrate; esta advertencia no significa que el servicio actual se haya detenido.

No se conectaron autodeploys de GitHub ni se modificó la configuración de backups de PostgreSQL. Los cambios locales futuros necesitan otro railway up o una conexión de repositorio configurada posteriormente. No enciendas el bot local mientras Railway esté activo.
