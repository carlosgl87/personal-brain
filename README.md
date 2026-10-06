
# Personal Brain

Base de un asistente para organizar información de trabajo. Incluye FASE 1 (catálogo), FASE 2 (texto de Telegram) , FASE 3 (extracción con Claude), FASE 4 (consultas SQL por Telegram) y FASE 5 (audio y transcripción con OpenRouter). FASE 6A agrega memoria por chunks, embeddings y razonamiento; no incluye recordatorios ni frontend.

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

La aplicación resuelve una sola conexión: `DATABASE_URL` si tiene valor; en caso contrario `DATABASE_PUBLIC_URL`. En tu laptop usa la conexión pública de Railway, dejando `DATABASE_URL` ausente o vacía. Dentro de Railway usa `DATABASE_URL` con la conexión interna. El driver utilizado es psycopg 3. Las credenciales de Telegram son necesarias para consultar el catalogo por HTTP y para usar el bot. Las de LLM se utilizan solo al activar FASE 3.

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

En otra terminal PowerShell. El catalogo requiere el mismo Bearer token que las rutas del bot. Si el token esta solo en `.env`, el siguiente ejemplo lo carga localmente sin imprimirlo:

```powershell
$apiToken = python -c "from app.config import get_settings; print(get_settings().telegram_credentials()[0])"
$headers = @{ Authorization = "Bearer $apiToken" }
Invoke-RestMethod 'http://127.0.0.1:8000/health'
Invoke-RestMethod 'http://127.0.0.1:8000/health/db'
$projects = @(Invoke-RestMethod 'http://127.0.0.1:8000/projects' -Headers $headers | ForEach-Object { $_ })
$projects.Count
Invoke-RestMethod 'http://127.0.0.1:8000/projects?area=Laureate' -Headers $headers
Invoke-RestMethod 'http://127.0.0.1:8000/projects?area=Laureate&category=Student%20Ecosystem' -Headers $headers
Invoke-RestMethod 'http://127.0.0.1:8000/projects?area=Consultora&company=Catusita' -Headers $headers
Invoke-RestMethod 'http://127.0.0.1:8000/projects?status=active' -Headers $headers
$projectId = [guid]$projects[0].id
Invoke-RestMethod ("http://127.0.0.1:8000/projects/" + $projectId) -Headers $headers
Invoke-RestMethod 'http://127.0.0.1:8000/projects?area=Personal' -Headers $headers
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

El endpoint interno POST /sources/{source_id}/process usa la misma autenticación Bearer que la recepción de Telegram. force=true solicita reprocesar de forma explícita. No hay endpoints de escritura sin autenticación para este flujo. FASE 4 agrega consultas por Telegram y FASE 5 admite audio; FASE 6A agrega embeddings y razonamiento; agentes y recordatorios siguen fuera del alcance.

Las pruebas cubren respuestas simuladas, rechazo de datos inválidos, idempotencia, historial y compilación offline de migraciones. La migración y la extracción real se validan con los comandos anteriores. No se despliega en Railway automáticamente.

## FASE 4: consultas por Telegram con SQL

En FASE 6A los ejemplos de preguntas naturales pasan al reasoning service; usa los comandos /pendientes y /decisiones para obtener el comportamiento SQL de esta fase.

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

Antes de FASE 6A, una pregunta no reconocida (comienza con ¿ o termina con ?) y los comandos desconocidos reciben ayuda. Para guardar una pregunta como nota, usa /nota seguido del texto. Ese texto completo, incluido el comando, se conserva como original.

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

Los fallos de transcripción o Claude conservan el original para reintentar con app.process. Todavía no hay una cola automática de recuperación de esos pendientes. Las confirmaciones de notas son de mejor esfuerzo; un reinicio puede repetir respuestas a consultas guardadas. La carga inicial por CLI se sustituyó posteriormente por la conexión de GitHub descrita abajo.


### Estado del primer despliegue

El servicio personal-brain quedó creado en el proyecto existente, sin dominio público y con Serverless desactivado. Se verificaron el arranque y un reinicio: API y receptor regresaron a estado activo. Las credenciales se cargaron desde el panel por el propietario, sin leer ni subir .env.

La carga inicial por CLI detectó railway.json, pero no aplicó correctamente el arranque. Se configuraron explícitamente en el servicio: startCommand=python -m app.cloud, preDeployCommand=alembic upgrade head, healthcheckPath=/health/db, una réplica, reinicio ON_FAILURE y los tiempos de apagado. Conserva esos ajustes en Settings al desplegar nuevamente.

La CLI actual advierte que railway.json sigue funcionando hasta el 1 de diciembre de 2026 y recomienda migrar a su formato Infrastructure as Code. Antes de esa fecha, revisar y migrar la configuración con railway config migrate; esta advertencia no significa que el servicio actual se haya detenido.

El despliegue inicial no conectó autodeploys; posteriormente se conectó GitHub como se explica abajo. No se modificó la configuración de backups de PostgreSQL. Los cambios futuros se publican mediante push a main. No enciendas el bot local mientras Railway esté activo.


### Despliegues automáticos desde GitHub

La fuente del servicio personal-brain es carlosgl87/personal-brain, rama main. El primer despliegue se cargó por CLI; el flujo habitual ahora es guardar cambios en Git y hacer push a main. Railway debe generar el despliegue asociado al commit. Guardar un archivo local o hacer solo commit no lo publica.

Antes de subir cambios ejecuta las pruebas y revisa los archivos:

```powershell
python -m unittest discover -s tests -q
git status --short
git diff --check
git add app migrations tests README.md Dockerfile railway.json
git commit -m "Describe el cambio"
git push origin main
```

Agrega explícitamente otros archivos nuevos que correspondan al cambio. Las claves viven en Variables de Railway y .env local; nunca las incluyas en commits. .env.example contiene solo la configuración de ejemplo.

Comprueba en Railway que el deployment corresponde al commit esperado y termina en Success. Las migraciones se ejecutan antes del arranque; un build o healthcheck fallido no debe darse por terminado. No uses railway up para el flujo habitual, porque desplegaría directamente el directorio local.

## FASE 6 — Memory & Reasoning

Implementación local de FASE 6A. Combina consultas SQL, recuperación semántica exacta y razonamiento con Claude. No agrega Project Memory, recordatorios, agentes, resúmenes programados ni frontend. Esta fase no requiere seed.

### Esquema y configuración

La migración **0006_memory_reasoning**, posterior a 0005, habilita pgvector con `CREATE EXTENSION IF NOT EXISTS vector` y crea dos tablas derivadas:

- `source_chunks`: fragmentos textuales, offsets, versión, metadata, vector, modelo y dimensión. La restricción `unique(source_id, chunk_version, chunk_index)` evita duplicados.
- `reasoning_runs`: pregunta, proveedor/modelo, versión del planner, plan validado, referencias recuperadas y respuesta. Guarda IDs, distancias y hashes; no copia transcripciones completas.

Las migraciones anteriores y los originales de `sources` se conservan. El downgrade de esta migración se bloquea para evitar borrar historia. No se ejecuta ningún backfill durante imports, startup o deployment.

**Antes de aplicar 0006 en producción, confirma el cambio.** PostgreSQL debe tener pgvector instalado y el usuario debe poder habilitar la extensión. La dependencia Python no instala la extensión del servidor. Si falta o no hay permisos, la migración aborta con un error claro; no reemplaces ni reinicies la base existente para resolverlo.

Instala dependencias y verifica primero:

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Solo cuando hayas aprobado la migración y comprobado la base de destino:

```powershell
alembic upgrade head
```

Variables nuevas, con sus valores predeterminados:

| Variable | Predeterminado | Uso |
|---|---|---|
| OPENROUTER_EMBEDDING_MODEL | vacío | Modelo de embeddings; vacío desactiva esa parte |
| OPENROUTER_EMBEDDING_DIMENSIONS | 2560 | Dimensión nativa esperada; se valida cada respuesta |
| MEMORY_AUTO_INDEX | false | Indexar cada nota nueva de Telegram y transcripción procesada |
| MEMORY_CHUNK_VERSION | paragraph-v1 | Versión del algoritmo |
| MEMORY_CHUNK_SIZE | 6000 | Caracteres por fragmento, entre 4000 y 7000 |
| MEMORY_CHUNK_OVERLAP | 350 | Overlap entre fragmentos |
| EXTRACTION_MAX_CHARS | 30000 | Máximo para extracción directa existente |

Se reutilizan `OPENROUTER_API_KEY` y `LLM_PROVIDER=anthropic`, `LLM_MODEL`, `LLM_API_KEY`. No necesitas una clave adicional. Para probar puedes seleccionar `OPENROUTER_EMBEDDING_MODEL=qwen/qwen3-embedding-4b` con dimensión 2560; consulta el [modelo en OpenRouter](https://openrouter.ai/qwen/qwen3-embedding-4b) y la [configuración oficial de Qwen](https://huggingface.co/Qwen/Qwen3-Embedding-4B/blob/main/config.json). No se carga el modelo en Railway ni en tu computadora.

El servicio utiliza la [API de embeddings de OpenRouter](https://openrouter.ai/docs/api/api-reference/embeddings/submit-an-embedding-request). Envía el texto de cada chunk y, en las búsquedas, el texto de la consulta; no envía metadata de Telegram ni credenciales dentro del contenido. Espera la dimensión nativa del modelo, sin solicitar reducción de dimensiones. Modelos y dimensiones diferentes nunca se mezclan en una búsqueda. Cambiar el modelo o la dimensión genera otra versión de embedding sobre los mismos chunks. Solo cambiar el algoritmo, tamaño u overlap cambia los chunks lógicos; los anteriores permanecen.

Reinicia la API tras cambiar configuración. Deja `MEMORY_AUTO_INDEX=false` mientras pruebas de forma manual. Activarlo no procesa el histórico: solo nuevas notas y transcripciones que pasan por el endpoint de procesamiento. Las llamadas a embeddings y Claude consumen saldo de sus proveedores.

### Chunking, backfill y reintentos

El chunking es determinístico: prioriza párrafos, saltos y frases, conserva offsets exactos y añade overlap. Se admiten telegram_text, audio_transcript, meeting_transcript, manual_note y document_text. Las preguntas y el audio original no se indexan; se utiliza su transcripción.

```powershell
python -m app.memory backfill --limit 10
python -m app.memory source UUID
```

Sustituye `UUID` por un UUID de fuente real. El backfill admite de 1 a 100 fuentes por ejecución. Selecciona fuentes vigentes sin chunks de la versión activa y también chunks sin embedding cuando este está configurado. Confirma los chunks antes de llamar a OpenRouter y cada embedding por separado. Si una llamada falla, los originales y los commits previos permanecen; el siguiente intento continúa con lo pendiente. Sin modelo configurado crea chunks y los deja pendientes de embeddings.

### Ingresar una reunión larga

Archivos UTF-8 `.txt` y `.md`, máximo 10 MiB:

```powershell
python -m app.ingest --file "C:\notas\reunion.txt" --type meeting_transcript --project "SIMA"
python -m app.ingest --file "C:\notas\nota.md" --type manual_note --process
```

Guarda el texto completo, sin normalizar sus saltos de línea, más filename, sha256, file_size e ingest_method. No persiste la ruta absoluta. Solo resuelve nombres y aliases existentes; un nombre inexistente o ambiguo deja la fuente sin proyecto. El mismo contenido, tipo y proyecto reutiliza la fuente al reintentar. Después genera chunks y, si está configurado, embeddings.

`--process` reutiliza la extraccion directa para fuentes cortas y la nueva extraccion jerarquica para fuentes largas cuando esta habilitada. Consulta la seccion de extraccion jerarquica mas abajo para limites, reintentos y provenance. Los originales se conservan completos.

### Búsqueda semántica

```powershell
python -m app.memory search "riesgos de adopción del dashboard" --project "SIMA" --limit 5
python -m app.memory search "inventarios" --company "Catusita" --area "Consultora" --type meeting_transcript
```

Los filtros se intersectan. Los nombres desconocidos o ambiguos requieren aclaración; nunca amplían el alcance a global. La búsqueda exacta por cosine distance selecciona únicamente chunks de fuentes vigentes y de la versión/modelo/dimensión activos. Devuelve chunk_id, source_id, project_id, fecha, contenido, offsets y distancia/score. No usa HNSW ni una base vectorial externa. El CLI imprime los resultados solicitados, incluidos sus textos.

### Preguntas por Telegram

```text
/ask ¿Qué temas importantes aparecen en mis últimas notas de Catusita?
/pregunta ¿Qué riesgos se repiten en las reuniones de SIMA?
¿Qué debería perseguir esta semana?
```

Los textos que comienzan con ¿ o terminan en ? usan reasoning. /ask y /pregunta lo fuerzan. Los demás textos se guardan como notas; /nota permite conservar una pregunta como nota. Los comandos /pendientes, /decisiones, /resumen, /estado, /completar, /reabrir, /fecha, /responsable, /proyecto y /ayuda mantienen su flujo determinístico sin llamadas a Claude.

La pregunta queda guardada como telegram_query y no crea tareas ni chunks. Claude recibe la pregunta, timestamp, America/Lima y catálogo; genera QueryPlan JSON. Pydantic rechaza campos extra, SQL, fechas sin zona, alcances inconsistentes y límites excesivos. La aplicación ejecuta funciones permitidas, sin SQL generado por el modelo.

El contexto ahora usa limites adaptativos focused / normal / broad y un presupuesto de caracteres; consulta la tabla de la evolucion siguiente. Reutiliza filtros de ultima extraccion exitosa, ediciones y transcripciones vigentes. El rango del plan filtra vencimiento de tareas, fecha de decision y recepcion de fuentes, respectivamente.

Una segunda llamada a Claude sintetiza en español hechos e inferencias, indica evidencia insuficiente y cita fuentes como `[Fuente: UUID]`. La aplicación comprueba que los UUIDs citados pertenecen a la evidencia recuperada. Si no hay evidencia, responde sin segunda llamada. Si falla embeddings, se puede responder con estructura y notas recientes, indicando cobertura incompleta. Un fallo de Claude conserva la pregunta y permite reenviarla. Reintentar el mismo update con una respuesta ya confirmada reutiliza reasoning_runs.

### Límites y validación

La recuperación es acotada; no garantiza revisar toda la historia ni reconstruir el estado global de cada proyecto. Los scopes de proyecto/empresa/área excluyen notas sin proyecto. Los originales completos permanecen en sources.raw_content; los chunks, resúmenes y respuestas son derivados. No hay clasificación NOTE/QUERY con LLM, cola de reintentos ni memoria consolidada por proyecto. La evolucion siguiente agrega extraccion jerarquica reanudable.

Las pruebas usan mocks, Settings sin .env y compilación SQL offline: no llaman a proveedores, no acceden a Railway ni borran datos. El funcionamiento real de la extensión, embeddings, planner y respuestas debe verificarse después de aprobar migración/configuración. No hacer push a main hasta esa aprobación: Railway está conectado para autodeploy.

## Recuperación adaptativa y extracción jerárquica

Esta evolución sobre FASE 6A cambia el presupuesto de recuperación y permite extraer reuniones largas. Conserva SQL, semantic search, comandos determinísticos, el flujo planner → retrieval → synthesis y los originales inmutables. No agrega Project Memory, agentes ni procesamiento durante startup.

### Adaptive retrieval

QueryPlan agrega `retrieval_depth`. Claude elige un enum, y la aplicación establece los máximos:

| Profundidad | Chunks | Fuentes recientes | Tareas | Decisiones |
|---|---:|---:|---:|---:|
| focused | 6 | 5 | 10 | 10 |
| normal | 12 | 10 | 20 | 20 |
| broad | 18 | 15 | 30 | 30 |

`focused` corresponde a un hecho, persona o costo puntual; `normal`, al estado general de un proyecto; `broad`, a panoramas, evolución, riesgos y conexiones. La profundidad no cambia el alcance: broad de SIMA sigue filtrando SIMA. Un límite explícito de notas recientes solo puede reducir el máximo del nivel, nunca ampliarlo. El planner puede desactivar tareas, decisiones o notas recientes cuando no sean útiles.

Para global + broad, cada consulta semántica obtiene hasta 54 candidatos, con hasta tres por proyecto antes del corte global. Fuentes sin proyecto forman otro grupo. La selección final alterna entre grupos ordenados por relevancia y conserva hasta 18 chunks. Se deduplican chunks entre consultas. Esto reduce el predominio de un proyecto, pero no garantiza revisar todos: depende de la relevancia, las fechas y la indexación disponible.

`REASONING_CONTEXT_MAX_CHARS` limita los caracteres del JSON de evidencia enviado a la síntesis, incluyendo metadata y warnings, sin contar pregunta y system prompt. Primero se admiten tareas y decisiones; luego chunks, notas recientes y catálogo. Los elementos que no caben se omiten del contexto, con advertencia explícita de cobertura parcial; los originales no cambian. Un chunk grande puede omitirse mientras otro más pequeño todavía cabe.

`reasoning_runs.plan` guarda la profundidad. `retrieved_context.retrieval` registra límites efectivos, cantidades antes/después del presupuesto, caracteres usados, presupuesto y si hubo reducción. La trace guarda IDs, fechas, estado, modelo/versión, distancias y hashes; no copia textos extensos de tareas, decisiones, chunks o transcripciones. La versión vigente del planner es `memory-planner-v3-project-memory`.

### Hierarchical extraction

Las fuentes de hasta `EXTRACTION_MAX_CHARS` conservan la extracción directa existente: una llamada, sin extracción por chunks. Las más largas usan automáticamente el flujo jerárquico si está habilitado. Se aplica también a audio_transcript después de transcribir; no cambia el límite de audio ni segmenta audio binario.

1. Se crean o reutilizan source_chunks de la versión activa mediante el servicio existente. Los embeddings no son un requisito para extraer.
2. Claude extrae cada chunk. Cada parcial exitoso se confirma antes de continuar.
3. Se guardan candidatos y evidencias exactas, con chunk_id y offsets globales. Un parcial no asigna ni cambia el proyecto de la fuente.
4. Claude consolida resúmenes y candidatos, sin recibir nuevamente la transcripción completa.
5. Después de validar la consolidación, se confirma atómicamente el ProcessingRun final, tareas, decisiones y evidencias.

La migración aditiva **0007_hierarchical_extraction**, posterior a 0006, agrega:

- `processing_run_parts`: source_id, chunk_id, chunk_version, índice, proveedor/modelo, prompt_version y resultado con evidencias.
- `task_evidence` y `decision_evidence`: item definitivo, parcial, chunk, cita exacta y offsets globales. Una tarea puede tener evidencia de varios chunks.

Las tablas nuevas tienen constraints e índices. Sus filas son inmutables mediante triggers; el downgrade destructivo está deshabilitado. No se modifican migraciones anteriores ni filas existentes, y no hay backfill.

La idempotencia de parciales usa fuente + chunk + versión + prompt + modelo. Reintentar reutiliza parciales confirmados; cambiar modelo, prompt o versión de chunks produce otros parciales y conserva los anteriores. Si falla el chunk N, los anteriores permanecen. Si falla la consolidación, todos los parciales permanecen. Una extracción final confirmada se reutiliza sin nuevas llamadas, salvo reprocesamiento explícito.

El consolidator elimina duplicados claros de overlap. La aplicación valida referencias de candidatos, citas y responsables/fechas fundamentados, conserva evidencias de todos los chunks y fusiona duplicados idénticos con offsets coincidentes. Hechos en posiciones distintas no se fusionan automáticamente solo por tener títulos iguales. La consolidación exige evaluar todos los candidatos mediante dispositions: kept, merged o rejected con motivo. Omitir una evaluación se rechaza; un candidato rechazado queda trazado sin crear un item final.

Se respeta primary_project_id existente. Una propuesta nueva necesita catálogo y coincidencia inequívoca en el original; ante ambigüedad queda null. La consolidación distingue compromisos y acuerdos de ideas, dudas e hipótesis. El resumen global debe cubrir temas, cambios, problemas, acuerdos, próximos pasos y puntos abiertos cuando haya evidencia.

### Configuración y costos

| Variable nueva | Predeterminado |
|---|---:|
| REASONING_CONTEXT_MAX_CHARS | 100000 |
| HIERARCHICAL_EXTRACTION_ENABLED | true |
| HIERARCHICAL_MAX_CHUNKS | 40 |
| HIERARCHICAL_PART_MAX_TOKENS | 4096 |
| HIERARCHICAL_CONSOLIDATION_MAX_ITEMS | 300 |
| HIERARCHICAL_CONSOLIDATION_MAX_CHARS | 160000 |
| HIERARCHICAL_CONSOLIDATION_MAX_TOKENS | 12000 |

`HIERARCHICAL_CONSOLIDATION_MAX_ITEMS` cuenta resúmenes parciales, candidatos y elementos auxiliares únicos. También se limita el JSON de consolidación por caracteres. Exceder un límite no trunca candidatos: detiene la extracción con originales, chunks y parciales confirmados conservados. Superar MAX_CHUNKS evita todas las llamadas de extracción de esa ejecución; los chunks quedan generados. Los embeddings son independientes y pueden estar pendientes.

Una fuente corta sigue costando una llamada de extracción. Una larga cuesta hasta una llamada por chunk pendiente más una de consolidación, además de embeddings si se solicitaron. Las preguntas broad envían más contexto que focused, dentro del presupuesto. Los reintentos reutilizan únicamente llamadas cuyos resultados se confirmaron: un fallo después de recibir una respuesta externa pero antes del commit puede requerir repetir esa llamada.

Estados de una fuente sin éxito previo: processing_parts → parts_complete → consolidating → processed; ante un fallo controlado, failed. Una interrupción puede dejarla en un estado intermedio, que app.process también selecciona para reintentar. En reprocesamiento, la extracción previa sigue vigente; un fallo no la reemplaza. El guard sobre tareas editadas manualmente sigue bloqueando reprocesamiento.

### Probar una transcripción larga

Ejecuta pruebas sin .env, proveedores ni base:

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Solo después de aprobar el cambio y elegir una base de prueba con pgvector disponible:

```powershell
alembic upgrade head
python -m app.ingest --file "reunion_cima.txt" --type meeting_transcript --project "SIMA" --process
```

El archivo debe ser UTF-8 .txt o .md. El texto completo queda guardado, incluso si después falla la extracción. Conserva el UUID de fuente que imprime el CLI. Para continuar o reprocesar explícitamente:

```powershell
python -m app.process --source-id UUID
python -m app.process --source-id UUID --reprocess
```

Sustituye UUID por el de esa fuente. No necesitas otro comando para seleccionar el modo largo. Para búsqueda, si hay embeddings configurados y algún fragmento sigue pendiente, puedes solicitar su indexación explícita después de aprobarla:

```powershell
python -m app.memory source UUID
python -m app.memory search "preocupaciones del cliente sobre costos" --project "SIMA"
```

Con la versión desplegada y un solo receptor Telegram activo:

```text
/ask ¿Qué preocupaciones manifestó Jorge sobre SIMA?
/ask Dame un panorama de SIMA: riesgos, pendientes y evolución.
/pendientes SIMA
/decisiones SIMA
```

Los detalles no convertidos en tareas o decisiones siguen presentes en los chunks. Los filtros de última extracción, edición y transcripción vigente continúan aplicándose al reasoning.

### Límites operativos

La extracción es síncrona y secuencial, sin cola ni scheduler; una reunión larga puede tardar varios minutos. El receptor espera hasta 3900 segundos por procesamiento; un corte o reinicio conserva commits anteriores y permite continuar por CLI. Para reuniones extensas, se recomienda ingesta por CLI. El recibo de Telegram sigue mostrando un resumen abreviado; el resumen consolidado completo queda en processing_runs.result.

No hay consolidación por múltiples niveles si los parciales exceden los límites: la ejecución se detiene explícitamente para ajustar el tratamiento. La evidencia textual se valida de forma determinística; la calidad de resúmenes e inferencias todavía depende de Claude y necesita evaluación con reuniones reales.

Las pruebas incluyen fallos por chunk, reanudación, consolidación, evidencias de tareas/decisiones, overlap, proyectos ambiguos, reuniones largas, audio y presupuestos adaptativos. Usan mocks y repositorios transaccionales en memoria; la migración se compila offline. Esta evolución queda local hasta confirmar migración, push y despliegue; no utiliza la autorización del despliegue anterior.

## Evolución final: contexto vivo por proyecto

Esta fase agrega semántica temporal, embeddings separados, generaciones explícitas, rechazo trazable de candidatos y Project Memory. Conserva PostgreSQL, SQL controlado, fuentes originales, chunks, extracción y consultas existentes. No cambia el texto ni metadata originales. No agrega frameworks de agentes ni servicios externos adicionales. Esta implementación queda local: no implica push, despliegue, migración de Railway ni llamadas pagadas de validación.

### Fechas de las preguntas

`QueryPlan.time_basis` admite `source_date`, `due_date`, `decision_date`, `created_date`, `mixed`, `none`. La aplicación elige columnas SQL; Claude solo selecciona una opción validada.

| Intención | Base temporal | Campo |
|---|---|---|
| Qué compromisos asumí / qué pasó esta semana | source_date | Source.received_at |
| Qué vence / pendientes para esta semana | due_date | Task.due_at |
| Cuándo se registró técnicamente | created_date | Task/Decision/Source.created_at |
| Fecha explícita de un acuerdo | decision_date | Decision.decided_at |
| Eventos combinados | mixed | Tareas: due_at; decisiones: coalesce(decided_at, source.received_at); fuentes: received_at |
| Sin ventana | none | Sin filtro de fechas |

Una tarea recibida esta semana con vencimiento próximo mes aparece en compromisos asumidos; una tarea antigua que vence esta semana aparece en vencimientos. Una decisión sin decided_at aparece con source_date. En due_date no se filtran decisiones por un vencimiento inexistente; el planner debe excluir clases ajenas a la intención. La recepción de una transcripción no garantiza la fecha real de la reunión: si el texto no informa la fecha, se conserva esa incertidumbre. Las ventanas tienen zona explícita de America/Lima. Una consulta con ventana temporal usa SQL/fuentes/chunks filtrados, sin introducir la memoria actual como si fuera historia del período.

### Chunks, embeddings y generaciones

`chunk_version` depende de algoritmo, tamaño y overlap. `chunk_embeddings` conserva múltiples vectores por chunk, separados por provider/model/dimensions. La búsqueda compara exclusivamente el modelo y dimensiones configurados. Cambiar embeddings no cambia texto, offsets ni identidad de chunks y no invalida parciales de Claude.

Los chunks históricos que antes incluían el modelo en su versión conservan sus IDs y versión física. Al reutilizarlos se añade `logical_version` cuando contenido, índices y offsets coinciden exactamente. Esto permite mantener las referencias y reutilizar parciales anteriores. Las columnas de embedding antiguas permanecen; la migración copia localmente los vectores conocidos a la tabla nueva, sin red ni pago. Seleccionar otro modelo requiere indexación explícita o el mecanismo auto-index ya configurado; no hay backfill automático de todo el historial.

```powershell
python -m app.process --source-id UUID --reprocess
python -m app.process --source-id UUID --reprocess --refresh-parts
```

Para fuentes largas, `--reprocess` reutiliza parciales compatibles y paga la nueva consolidación. Si faltan parciales compatibles por cambio del modelo/prompt/segmentación, solo se extraen los faltantes. `--refresh-parts` requiere `--reprocess` y un UUID explícito; solicita nuevas llamadas por chunk y después consolida. Para fuentes cortas sigue existiendo una sola llamada directa: no hay parciales que refrescar. Se mantiene el bloqueo de reprocesamiento cuando hay tareas editadas manualmente.

`extraction_generations` identifica cada refresh, su ejecución anterior y estado pending/failed/processed; `extraction_generation_parts` conserva los resultados nuevos y su vínculo con el parcial base. Los parciales existentes permanecen inmutables. Un refresh fallido se reanuda con la misma generación y no repite parciales ya confirmados. Después de publicarla, la reconsolidación económica reutiliza esa generación publicada. Las evidencias finales guardan también generation_part_id. La extracción previa sigue vigente si falla la nueva.

Cada candidato debe figurar exactamente una vez en `candidate_dispositions` del ProcessingRun final. kept y merged apuntan al índice del item final de su tipo; rejected exige motivo y no crea task/decision. Se validan todos los IDs, citas exactas, responsables, fechas y cobertura. La deduplicación de overlap mantiene evidencias y ajusta los índices de disposition al resultado definitivo. Los candidatos rechazados siguen disponibles en los parciales.

### Project Memory y worker

`project_memory_versions` conserva versiones inmutables con memoria JSON de 12 secciones, referencias originales por sección, versión anterior, fuentes usadas, cursor, proveedor/modelo y prompt. Secciones: executive_summary, objective, current_state, recent_progress, open_items, risks_and_blockers, key_decisions, recently_completed, key_people, important_facts, open_questions y recent_changes. Secciones sin evidencia quedan vacías; no se fabrican memorias para los 32 proyectos al migrar.

`project_memory_state` mantiene el puntero vigente y scheduling. `project_memory_events` registra cambios inmutables en la misma transacción que la extracción o edición. Un contador por proyecto, serializado con bloqueo de fila, evita perder cambios concurrentes. `through_revision` identifica exactamente el cursor consumido; consolidated_through_at conserva el timestamp asociado, pero el cursor de eventos gobierna la actualización, incluso con timestamps iguales o fechas de recepción antiguas.

Una extracción exitosa marca dirty; completar, reabrir, cambiar fecha/responsable o mover una tarea también. Moverla marca el proyecto anterior y el nuevo. La memoria del destino recibe el cambio y la tarea vigente con sus referencias; no incorpora reuniones completas de otro proyecto solo porque esa tarea provenga de ellas. Cada cambio mueve refresh_after al último cambio + 300 segundos. Un update repetido no genera otro evento. Una ráfaga cercana se procesa en un batch; si supera el límite, el cursor avanza solo hasta los eventos consumidos y queda trabajo pendiente para el siguiente batch.

El worker consulta únicamente proyectos pendientes o elegibles para reconciliación y usa un advisory lock por proyecto con conexión dedicada. No sostiene el bloqueo de estado mientras llama a Claude. Antes de publicar verifica el puntero y la conexión de propiedad; si llegan cambios mientras genera, preserva sus eventos y debounce. Dos workers no generan una actualización simultánea del mismo proyecto. La publicación de versión y puntero es atómica.

La actualización incremental envía memoria previa + fuentes de los eventos nuevos + SQL vigente de tareas, incluidas completadas, y decisiones + cambios manuales. La evidencia es acotada: como máximo 20 eventos/fuentes por batch, 60 tareas y 40 decisiones. El límite de eventos incluye cambios de tarea/refresh, no solo fuentes distintas. Fuentes largas usan resumen y extracto con marca de truncamiento; sus originales y chunks siguen disponibles. Un batch incremental cuyo contexto excede el presupuesto se rechaza sin adelantar el cursor; reduce el batch antes de reintentar. No se trunca la salida de Project Memory: se rechaza si supera MAX_CHARS o tiene referencias inválidas.

Alrededor de las 03:00 America/Lima se reconcilian proyectos cambiados desde la última reconciliación: memoria anterior, evidencia reciente, SQL actual y hasta 8 chunks semánticos históricos si están disponibles. No se recorre el archivo completo. Cambios llegados después de la hora nocturna siguen el debounce normal y quedan para la noche siguiente. Proyectos sin cambios no llaman a Claude. Un fallo por proyecto registra un mensaje seguro y aplica 60 segundos de espera antes de reintentar.

`app.cloud` inicia FastAPI, el receptor Telegram y el worker dentro del mismo contenedor. El worker inicia al obtener el turno del bot; si termina, se reinicia tras 30 segundos y Telegram sigue activo. Una actualización fallida no termina el worker ni los otros procesos. El apagado del supervisor detiene sus hijos. La extracción jerárquica continúa síncrona; la nueva consolidación de memoria se realiza fuera del request Telegram.

### Consultas, refresh y costos

El planner puede incluir Project Memory para estado general y panorama. Reasoning combina memoria + delta posterior a through_revision + SQL + semantic retrieval y fuentes recientes. Durante el debounce incorpora fuentes nuevas y, para comandos de tareas, before/after y estado SQL actual: una tarea completada no depende de que la memoria antigua siga diciendo pendiente. Las consultas globales recuperan memorias compactas de varios proyectos. En el presupuesto, los deltas se incorporan antes de la memoria; si no cabe el delta de una memoria dirty, se omite esa memoria vieja para evitar responder solo con ella; los límites y la reducción se reportan. Las referencias de la síntesis deben pertenecer a fuentes recuperadas o secciones de memoria usadas. La trace registra versiones, referencias, cantidades y hashes, sin copiar cuerpos completos.

Si no existe memoria, está deshabilitada o falla el worker, siguen disponibles SQL, notas recientes y chunks. Las preguntas específicas pueden recuperar originales semánticos; Project Memory no sustituye historia ni `/pendientes`. La calidad de la resolución de contradicciones sigue dependiendo de Claude: reemplaza estado cuando hay evidencia clara, y conserva incertidumbre cuando falta. SQL sigue siendo la lista exacta de tareas.

```text
/refrescar SIMA
/refresh CIMA
```

Estos comandos solo guardan y encolan la solicitud, sin trabajo pesado dentro de Telegram. CIMA necesita existir como alias inequívoco de SIMA. El worker genera una versión sin esperar el debounce ordinario. Por CLI:

```powershell
python -m app.project_memory refresh --project SIMA
python -m app.project_memory rebuild --project SIMA
python -m app.project_memory_worker --once
python -m app.project_memory_worker
```

`refresh` ejecuta un incremental explícito; `rebuild` ejecuta una reconciliación acotada y conserva versiones anteriores. `--once` atiende trabajo que ya venció, no fuerza proyectos ni espera cinco minutos. Estos comandos pueden llamar a proveedores. Si una actualización queda pendiente por error/lock/evidencia insuficiente, el CLI lo indica; no garantiza que se haya generado una versión.

| Variable nueva | Default |
|---|---|
| PROJECT_MEMORY_ENABLED | true |
| PROJECT_MEMORY_DEBOUNCE_SECONDS | 300 |
| PROJECT_MEMORY_MAX_CHARS | 12000 |
| PROJECT_MEMORY_INCREMENTAL_MAX_SOURCES | 20 |
| PROJECT_MEMORY_RECONCILIATION_HOUR | 3 |
| PROJECT_MEMORY_TIMEZONE | America/Lima |

Se reutilizan las claves existentes. Cada actualización con evidencia cuesta una llamada a Claude; reconciliar puede añadir una consulta de embeddings. El debounce agrupa cambios. Deshabilitar PROJECT_MEMORY_ENABLED evita scheduling y llamadas del worker. No se hacen llamadas para proyectos sin cambios/evidencia, salvo refresh manual con evidencia existente. Un fallo externo después de cobrar pero antes de confirmar puede repetir el costo al reintentar; no hay garantía de exactly-once de proveedores. MAX_CHARS y los presupuestos limitan tamaño, no garantizan calidad ni tarifa fija.
### Migraciones y prueba local con CIMA

Archivos principales: `app/services/project_memory*.py`, `app/schemas/project_memory.py`, `app/models/project_memory.py`, `app/project_memory.py`, `app/project_memory_worker.py`, `app/cloud.py`; además planner/reasoning, chunking/memory, hierarchical, processing, task_management, Telegram y sus schemas/modelos. Configuración de ejemplo en `.env.example`.

Migraciones nuevas, sin modificar 0001–0007:

- **0008_vector_generations**: logical_version, chunk_embeddings con copia SQL local de vectores existentes, generaciones/parciales nuevos y generation_part_id en evidencias. Conserva columnas, vectores e IDs antiguos.
- **0009_project_memory**: versiones, estado y eventos; índices, referencias, unicidad e inmutabilidad de historia. No crea memorias ni llama a proveedores.

Ambas tienen downgrade destructivo deshabilitado. Las pruebas compilan el SQL offline; no sustituyen una ejecución contra PostgreSQL real con pgvector. No se ejecutaron migraciones de esta fase contra la base existente. Para comprobarlas usa una base de prueba aislada y una copia representativa si necesitas validar datos previos.

1. En PowerShell, con la venv activa, ejecuta las pruebas sin proveedores ni configuración real:

```powershell
python -B -m unittest discover -s tests -v
python -m pip check
git diff --check
```

2. Prepara una base local de PostgreSQL con pgvector disponible. En el cliente SQL de esa instancia crea una base nueva:

```sql
CREATE DATABASE personal_brain_test;
```

En **cada terminal de prueba**, establece una conexión a esa base; reemplaza usuario/clave por los de tu PostgreSQL local. No uses una URL de Railway. Las variables de una terminal no se comparten con otras; si abres otra, repite este bloque. Estas variables de proceso tienen prioridad sobre `.env` y no modifican el archivo.

```powershell
$env:DATABASE_URL = 'postgresql://postgres:TU_CLAVE@127.0.0.1:5432/personal_brain_test'
$env:PROJECT_MEMORY_ENABLED = 'true'
$env:PROJECT_MEMORY_DEBOUNCE_SECONDS = '300'
$env:MEMORY_AUTO_INDEX = 'false'
alembic upgrade head
python -m app.seed
alembic current
```

El último comando debe mostrar `0009_project_memory`. Seed se solicita solamente para esta base nueva de prueba, no para producción. Las claves LLM/OpenRouter pueden seguir en tu `.env` privado; no las imprimas.

3. Solo cuando quieras consumir APIs con el archivo real, sustituye la ruta por tu archivo CIMA UTF-8 `.txt` o `.md`:

```powershell
python -m app.ingest --file 'C:\ruta\reunion_cima.txt' --type meeting_transcript --project SIMA --process
```

Este comando guarda el archivo original completo, crea chunks, solicita embeddings si hay un modelo configurado y procesa con Claude. **La ingesta explícita indexa aunque MEMORY_AUTO_INDEX=false**; ese flag controla solamente el auto-index de capturas Telegram. Revisa el UUID impreso después de “Fuente completa guardada”. En esta base nueva puedes probar reanudación/reconsolidación y refresh pagado sobre ese UUID:

```powershell
$sourceId = 'PEGA_AQUI_EL_UUID_DE_LA_FUENTE'
python -m app.process --source-id $sourceId
python -m app.process --source-id $sourceId --reprocess
python -m app.process --source-id $sourceId --reprocess --refresh-parts
```

Sin cambios, la primera reutiliza la extracción final; la segunda reutiliza parciales y paga consolidación; la tercera genera parciales nuevos y paga consolidación. No necesitas ejecutar las tres para validar una ingesta normal. El archivo CIMA debe asociarse a SIMA si ese es el proyecto del catálogo. Revisa manualmente resumen, compromisos, rechazos y provenance: un resultado técnicamente válido puede contener errores de interpretación.

4. Para validar el debounce, ejecuta el worker en una terminal con el mismo DATABASE_URL de prueba y claves privadas:

```powershell
python -m app.project_memory_worker
```

Tras procesar una fuente verás “Project memory scheduled”. Varias notas procesadas del mismo proyecto dentro de cinco minutos deben mover refresh_after y producir una sola actualización, salvo que excedan el tamaño del batch. Tras el último cambio + cinco minutos, el worker debe registrar “Project memory refreshed”. Revisa el estado con esta consulta de solo lectura desde esa misma terminal/base:

```powershell
@'
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_engine
from app.models import Project, ProjectMemoryState, ProjectMemoryVersion
with Session(get_engine()) as db:
    rows = db.execute(select(Project.name, ProjectMemoryState, ProjectMemoryVersion)
        .join(ProjectMemoryState, ProjectMemoryState.project_id == Project.id)
        .outerjoin(ProjectMemoryVersion, ProjectMemoryVersion.id == ProjectMemoryState.current_version_id)
        .where(Project.name == 'SIMA')).all()
    for name, state, version in rows:
        print(name, 'dirty=', state.is_dirty, 'refresh_after=', state.refresh_after,
              'revision=', state.change_revision, 'retry_after=', state.retry_after)
        if version:
            print('version=', version.version_number, 'cursor=', version.through_revision,
                  'previous=', version.previous_version_id, 'type=', version.update_type)
            print('estado=', version.memory['current_state'])
'@ | python -B -
```

Después de éxito sin cambios posteriores: dirty=False, cursor=revision, refresh_after=None; la sección muestra texto y source_ids. Si no hay filas, el proyecto aún no recibió eventos/refresh en esta base. Para inspeccionar historia, consulta project_memory_versions por project_id ordenada por version_number y comprueba que las versiones anteriores siguen presentes.

5. Para una prueba inmediata puedes ejecutar, sin esperar el debounce:

```powershell
python -m app.project_memory refresh --project SIMA
python -m app.project_memory rebuild --project SIMA
```

Ambos consumen Claude si hay evidencia; rebuild puede consultar embeddings. Cada éxito debe crear otra versión y conservar previous_version_id. Para validar ejecución única del worker usa `python -m app.project_memory_worker --once` cuando el deadline ya haya vencido. Ejecutar dos workers sobre un mismo proyecto debe permitir publicar solo a quien obtiene el advisory lock. Proyectos sin datos no deben producir contenido artificial ni llamadas pagadas. Para la reconciliación nocturna, deja el worker activo hasta la siguiente hora configurada y comprueba update_type=reconciliation solo en proyectos cambiados; las pruebas automatizadas cubren horarios simulados sin esperar una noche.

6. Para Telegram local utiliza preferiblemente un bot de prueba distinto del bot productivo, con su token en una variable privada y el usuario autorizado. No ejecutes dos receptores del mismo bot. Inicia el supervisor desde una terminal con la conexión de prueba:

```powershell
python -m app.cloud
```

El supervisor arranca API, receptor y worker; no ejecuta Alembic por sí mismo. Detén el worker independiente si lo usaste antes, para simplificar la observación. No se ha detenido ni cambiado el bot de Railway durante esta fase.

En Telegram prueba:

```text
/ask ¿Cómo está SIMA?
/ask ¿Qué compromisos asumí esta semana en SIMA?
/ask ¿Qué vence esta semana en SIMA?
/ask ¿Qué decisiones tomamos esta semana en SIMA?
/ask Dame un panorama global de los proyectos.
/pendientes SIMA
/refrescar SIMA
```

Después de una nueva nota y antes de cinco minutos, pregunta por el nuevo dato: la respuesta debe combinar memoria previa y fuente nueva. Completa una tarea usando `/completar UUID` y consulta inmediatamente: el delta debe reflejar la finalización aunque aún no haya nueva versión. Mueve una tarea con `/proyecto UUID AI Tutor` y comprueba dirty en ambos proyectos. Verifica que `/pendientes` continúa siendo exacto y determinístico. Las preguntas de detalle deben poder citar fuentes/chunks originales.

Para validar un fallo del worker, las pruebas automatizadas simulan caída/reinicio y errores de actualización sin afectar Telegram ni revelar contenido/claves. No hace falta provocar fallos en producción. Logs permitidos: scheduled/refreshed/reconciliation completed/refresh failed con project_id; nunca prompts ni respuestas completas. Reintentos conservan la memoria previa y mantienen dirty.

Esta fase termina aquí. No incluye push, despliegue, migración productiva, backfill del archivo existente ni validación pagada real; los comandos con proveedores anteriores quedan para tu prueba explícita.
Si deshabilitas PROJECT_MEMORY_ENABLED, tampoco se registran eventos nuevos de esa función. Al reactivarla, solicita `rebuild --project SIMA` para cada proyecto que cambió durante el período deshabilitado, y revisa el resultado acotado. Las fuentes y los estados SQL permanecen, pero ese período no tiene un cursor incremental de Project Memory.

## Revision de produccion

Los hallazgos, correcciones y limites de la validacion estan en [docs/production-review.md](docs/production-review.md).
Las dependencias de runtime se fijan en `constraints.txt`; Docker y pip usan las mismas versiones. Actualizalas deliberadamente y vuelve a ejecutar las pruebas.
`/health/db` comprueba conexion y que Alembic este en la revision requerida por este codigo. `/projects` y `/projects/{id}` requieren `Authorization: Bearer <token del bot>`.
Una edicion de Telegram que reemplazaria tareas modificadas manualmente se bloquea para conservar esos cambios. Guarda la correccion como una nota nueva.
Los errores temporales de Telegram, incluidos 429 y fallos de red, se reintentan conservando el offset. Credenciales invalidas, polling duplicado y webhook activo detienen el receptor.

Las pruebas SQL son optativas y escriben solo en una base desechable local ya migrada. No uses una copia que quieras conservar:

```powershell
$env:PERSONAL_BRAIN_TEST_DATABASE_URL = 'postgresql+psycopg://usuario:clave@127.0.0.1:5432/personal_brain_test_audit'
python -B -m unittest discover -s tests -v
Remove-Item Env:PERSONAL_BRAIN_TEST_DATABASE_URL
```

Las pruebas habituales sin esa variable omiten las pruebas SQL; no cargan `.env` ni consumen APIs.
