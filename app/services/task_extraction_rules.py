"""Shared semantic task granularity policy for existing extraction calls."""

TASK_ATOMICITY_RULES = """
GRANULARIDAD DE TAREAS:
Cada tarea representa una unidad de trabajo con estado independiente. Antes de emitirla,
identifica las acciones comprometidas: si pueden terminar en momentos distintos, tener
responsables o fechas diferentes, depender una de otra, o una estar hecha y otra pendiente,
emite tareas separadas. La separacion es semantica, nunca mecanica por la palabra 'y'.
No emitas ademas una tarea paraguas que repita el conjunto de acciones ya separado.
Cada titulo debe entenderse por si solo: resuelve pronombres como 'sus respuestas',
'su feedback' o 'mandarles' usando SOLO el contexto explicito de la fuente.
No agregues pasos implicitos, microtareas, responsables, fechas ni compromisos nuevos.
Esperar una respuesta no es una tarea adicional salvo que se haya comprometido como accion.
Una sola accion aplicada a objetos relacionados, atributos o una pregunta subordinada
permanece junta. Verbos dentro de algo que se pide confirmar no son tareas de ejecucion.
No fragmentes una unidad de trabajo solo por enumerar objetos, cualidades o pasos inseparables.

SI SEPARAR (ejemplos de entrada -> titulos):
- 'Tengo que enviar los correos a los responsables de IT y consolidar sus respuestas.'
  -> 'Enviar los correos a los responsables de IT';
     'Consolidar las respuestas de los responsables de IT'.
- 'Mañana debo enviar el dashboard a Luis y luego recoger su feedback.'
  -> 'Enviar el dashboard a Luis'; 'Recoger el feedback de Luis'.
- 'Hay que corregir las observaciones y después poner la nueva versión en producción.'
  -> 'Corregir las observaciones'; 'Poner la nueva versión en producción'.
- 'Agregar los usuarios al sistema y mandarles el correo con el link.'
  -> 'Agregar los usuarios al sistema'; 'Enviar el correo con el link a los usuarios'.
- 'Validar la información con cada equipo, consolidar las correcciones y devolver la versión final a McKinsey.'
  -> 'Validar la información con cada equipo'; 'Consolidar las correcciones';
     'Enviar la versión final validada a McKinsey'.

NO SEPARAR (cada ejemplo es una sola tarea):
- 'Revisar costos y presupuesto del proyecto'.
- 'Validar nombre y correo de cada usuario'.
- 'Revisar los datos de créditos y cobranzas'.
- 'Preparar una presentación clara y ejecutiva'.
- 'Confirmar si la orden de compra y el acta de conformidad pueden hacerse antes de la nueva firma del contrato'.

DESCRIPCION, FECHAS, RESPONSABLES Y EVIDENCIA:
Conserva en description las condiciones o dependencias explicitas utiles, sin un sistema
formal de dependencias. Si dice enviar correos y luego consolidar respuestas, la segunda
descripcion puede indicar 'Realizar después de recibir las respuestas a los correos enviados'.
Asigna due_at y owner_text por ACCION, no por frase. No copies automaticamente una fecha
o responsable de una accion a las demas. Si 'mañana tengo que enviar los correos y cuando
respondan debo consolidar la información', solo enviar vence mañana; consolidar due_at=null.
Si 'antes del viernes tengo que revisar los datos y enviar el informe', el plazo compartido
puede aplicarse a ambas. Resuelve las fechas relativas con la fecha original y America/Lima.
Si 'yo enviaré el informe y Ana consolidará las respuestas', la primera owner_text='yo'
y la segunda owner_text='Ana'; no infieras el nombre del usuario ni mezcles responsables.
Si un responsable o plazo aplica claramente a todo el conjunto, puede conservarse en ambas.
Si no hay evidencia suficiente, usa null. No conviertas acciones ya realizadas en nuevas
tareas abiertas; extrae solo pendientes/compromisos que quedan por hacer.
Cada tarea separada conserva evidence literal de la fuente que fundamente su accion y
metadatos. Dos tareas distintas pueden compartir la misma cita completa; no inventes
citas a partir de titulos reformulados ni asumas que compartir evidence las vuelve duplicadas.
""".strip()

CONSOLIDATION_ATOMICITY_RULES = """
Conserva la granularidad de las tareas parciales: acciones con estado independiente siguen
siendo tareas distintas aunque compartan frase, evidence, proyecto, responsable o fecha.
No unas enviar con consolidar, entregar con recoger feedback, ni corregir con desplegar.
Solo fusiona duplicados claros de la MISMA unidad de trabajo. No crees una tarea paraguas
ademas de las tareas independientes. Conserva las condiciones/dependencias fundamentadas
en description y el contexto de objetos/personas necesario para entender cada titulo.
No traslades fechas o responsables entre acciones. Una fecha o responsable presente en
otra tarea no fundamenta copiarlo a esta. No fragmente objetos relacionados o preguntas
subordinadas de una misma accion. Respeta candidate_ids y dispositions existentes.
""".strip()
