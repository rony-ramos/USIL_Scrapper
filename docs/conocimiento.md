# Clases, PDFs y transcripciones

El flujo referencia los PDFs de `biblioteca`, conserva los VTT originales y
organiza clases con fechas verificadas. `conocimiento.py` funciona sin `agy`;
los agentes son un paso posterior y opcional.

## Preparar la estructura

Actualiza primero el inventario del curso con `canvas_sync.py`. El curso debe
pertenecer al periodo configurado. Los comandos siguientes usan un ID de ejemplo;
reemplaza `123` por el ID Canvas de tu curso.

```powershell
$cursoId = 123
.\.venv\Scripts\python.exe conocimiento.py --course $cursoId --dry-run
.\.venv\Scripts\python.exe conocimiento.py --course $cursoId
```

Configura `knowledge_root` y `knowledge_timezone` en `biblioteca.config.json`.
Las rutas relativas parten de ese archivo. La zona predeterminada es
`America/Lima`; `tzdata` permite convertir fechas tambien en Windows.

```text
conocimiento/<periodo>/<curso>-<id>/
  curso.json
  calendario.json
  materiales.json                 # referencias y hashes; no copias de PDFs
  relaciones-manuales.json
  indice.md
  pendientes.md
  entrada-transcripciones/        # puedes poner aqui tus exportaciones VTT
  semanas/S01/.../S08/
    materiales.json
    clases/<fecha_hora>-<id>/
      clase.json
      fuentes/*.vtt
      fuentes/versiones/          # originales anteriores cuando cambian
      procesado/transcripciones/
  semanas/sin-semana/             # fechas o materiales sin evidencia suficiente
  procesado/pdfs/<recurso>/<hash>/<modelo_y_skill>/
```

El calendario se deriva de intervalos publicados en Canvas solo cuando coinciden
con la cantidad de semanas. Un PDF ubicado en S05 cuyo titulo indica S06 queda
pendiente. Compartir semana con una clase no prueba que se haya usado en ella.

Puedes editar `relaciones-manuales.json` y repetir `conocimiento.py`:

```json
{
  "pdf_weeks": {"456": "S06"},
  "classes": {
    "ID_EXISTENTE_DE_CLASE": {"pdf_ids": ["canvas:123:456"]}
  },
  "calendar": [
    {"week": "S06", "start": "2026-09-21", "end": "2026-09-27"}
  ]
}
```

Si proporcionas `calendar`, reemplaza el calendario inferido: incluye todas las
semanas necesarias. El ID del PDF es el ID del elemento de modulo registrado en
`materiales.json`, y el de la clase aparece en `clase.json` o `grabaciones.json`.
Las carpetas ya creadas conservan su ubicacion aunque corrijas una semana;
el indice y los manifests reflejan la nueva relacion.

## Importar una transcripcion manual

Exporta el contenido original en formato WebVTT (`WEBVTT`, extension `.vtt`, UTF-8).
No guardes como VTT una pagina HTML de error. No necesitas copiar cookies o cURL.

Para una clase nueva indica una fecha ISO **con offset**:

```powershell
.\.venv\Scripts\python.exe conocimiento.py --course $cursoId `
  --import-vtt .\mis-transcripciones\clase.vtt `
  --class-date '2026-10-07T19:03:53-05:00' --meeting-id 'mi-reunion'
```

Para asociarla a una clase que Zoom ya inventario, usa su ID. No repitas fecha
ni reunion:

```powershell
.\.venv\Scripts\python.exe conocimiento.py --course $cursoId `
  --import-vtt .\mis-transcripciones\clase.vtt --class-id ID_EXISTENTE_DE_CLASE
```

Si importaste un VTT a mano antes de capturar los metadatos de Zoom, la misma
sesion puede quedar como dos clases (`pendientes.md` lo indica como "misma reunion
e inicio; posible duplicado"). Unifica la clase manual con la de Zoom:

```powershell
.\.venv\Scripts\python.exe conocimiento.py --course $cursoId --merge-class ID_CLASE_MANUAL --into ID_CLASE_ZOOM --dry-run
.\.venv\Scripts\python.exe conocimiento.py --course $cursoId --merge-class ID_CLASE_MANUAL --into ID_CLASE_ZOOM
```

Solo se admite si ambas comparten reunion e inicio y el origen no tiene metadatos
de Zoom. Los originales y resultados procesados no se mueven: la clase manual se
retira como `clase.unificada.json` y la clase de Zoom referencia sus archivos, asi
que el procesamiento se reanuda donde quedo. Una captura o cURL con el mismo
contenido que una transcripcion ya vinculada queda como `duplicate` y solo agrega
su referencia de visor como verificacion.

`--transcript-key principal` identifica de forma estable una transcripcion dentro
de esa clase. Para una segunda parte usa otra clave, por ejemplo `parte-2`.
Reimportar la misma clave no duplica el archivo; si cambia el contenido, conserva
la version anterior. La importacion verifica formato y tiempos antes de escribir,
conserva los bytes UTF-8 originales y registra SHA-256.

Para un lote, copia `transcripciones.example.json` a `transcripciones.json` y
configura cada entrada. Puedes usar `class_id` en lugar de `started_at` y
`meeting_id`. Las rutas parten del JSON, no del directorio de ejecucion.

```powershell
.\.venv\Scripts\python.exe conocimiento.py --course $cursoId --import-manifest .\transcripciones.json --dry-run
.\.venv\Scripts\python.exe conocimiento.py --course $cursoId --import-manifest .\transcripciones.json
```

El lote completo se valida antes de importar la primera transcripcion.

Los VTT descargados que aun no tienen fecha verificable se conservan en
`transcripciones-sin-fecha/<id>/fuentes`, con un manifest `fuente.json` y sus
resultados separados. El indice los muestra como pendientes; se pueden corregir
sin inventar una semana o un horario.
Si el origen incluye una referencia verificada al visor, el manifest puede guardar
`recording_reference` con `method: observed_viewer_path_sha256` y el SHA-256 de su
path. Esto permite comparar los metadatos posteriores sin conservar el enlace
autenticado. La coincidencia identifica la grabacion; la fecha debe provenir de
los metadatos de Zoom.

## Capturar metadatos desde Zoom

```powershell
# Descubre todas las paginas; captura metadatos de una clase para validar el acceso:
.\.venv\Scripts\python.exe conocimiento.py --course $cursoId --sync-zoom --limit 1
# Carga inicial completa; en ejecuciones posteriores omite capturas verificadas:
.\.venv\Scripts\python.exe conocimiento.py --course $cursoId --sync-zoom
# Volver a comprobar los metadatos de grabaciones conocidas:
.\.venv\Scripts\python.exe conocimiento.py --course $cursoId --sync-zoom --refresh
```

El adaptador abre el perfil configurado y puede requerir login/MFA. La sesion de
Canvas debe estar activa; el perfil puede no conservar cookies de sesion al cerrar
Chrome. No ejecutes dos procesos con ese perfil.

Canvas proporciona la pestaña Zoom. El adaptador recorre «Grabaciones en la nube»
y hace clic en cada fila. Captura la respuesta `recording/file` emitida por la
plataforma, verifica la ocurrencia de la reunion y conserva solo sus metadatos.
No pulsa los controles de video o audio ni abre reproductores de Zoom.

El ID de ocurrencia distingue clases de reuniones recurrentes. Los timestamps
UTC del listado y las fechas `recordingStart`/`recordingEnd` de los archivos se
convierten usando sus zonas verificadas. `createTime` no es la fecha de la clase.
La zona mostrada por Zoom puede ser diferente de `knowledge_timezone`.
Las referencias a `playUrl` se guardan mediante hash; URLs autenticadas,
cabeceras y cookies no se guardan.

`grabaciones.json` distingue `metadata_captured`, `pending` y `deferred` (limite de piloto).
La estructura de una clase se crea aunque su VTT quede pendiente, cuando se pudo
leer el listado. `ultima-sincronizacion-zoom.json` resume la ejecucion. Si la sesion,
el panel o la solicitud cambian, registra pendientes.
Este adaptador corresponde a la interfaz observada de Zoom LTI, no a su API publica.

## Capturar transcripciones desde el navegador (recomendado)

Zoom detecta la automatizacion con Selenium y cierra la sesion, por eso el
adaptador anterior no abre reproductores. La captura se hace en el navegador
habitual del usuario, con su sesion, mediante
[`herramientas/zoom-vtt-capture.js`](../herramientas/zoom-vtt-capture.js):

### Configuracion unica de la bandeja

```powershell
.\.venv\Scripts\python.exe conocimiento.py --init-capture-inbox
```

Crea `conocimiento/entrada-capturas/` con el marcador `.usil-zoom-capture-inbox`.
La primera vez que ejecutes el script en un reproductor aparece el boton
**Elegir bandeja de capturas**: pulsalo y elige esa carpeta (no otra; el script
rechaza carpetas sin el marcador). En una visita posterior Chrome mostrara
**Autorizar bandeja de capturas**; pulsalo y elige **Permitir en cada visita**.
Desde entonces el script escribe sin dialogos ni clics, tambien cuando lo
ejecuta un agente. Si Chrome retira el permiso (por ejemplo al borrar los datos
del sitio zoom.us) el script se detiene con un motivo `..._requiere_clic_del_usuario`
y vuelve a mostrar el boton.

Solo esa carpeta queda accesible para la pagina de Zoom: no elijas la carpeta del
proyecto ni Descargas. Python trata su contenido como datos no confiables.

### Captura e importacion

1. Abre la grabacion en Zoom (Canvas > Zoom > Grabaciones en la nube) y espera
   a que cargue el reproductor. Si el panel de transcripcion esta oculto, abrelo.
2. Ejecuta el script en esa pestaña:
   - **Marcador**: crea un marcador cuya URL sea `javascript:` seguido del
     contenido completo del archivo, y pulsalo en cada grabacion.
   - **Agente con tu navegador** (por ejemplo Claude in Chrome): el agente abre
     el reproductor y ejecuta el mismo script. Solo recibe el resumen (id y
     numero de segmentos), no el texto ni credenciales.
3. El script guarda `zoom-vtt-<id>.json` en la bandeja.
4. Importa la bandeja. No hace falta indicar el curso: cada captura se asigna al
   curso cuyo visor verificado coincide.

```powershell
.\.venv\Scripts\python.exe conocimiento.py --import-inbox --dry-run
.\.venv\Scripts\python.exe conocimiento.py --import-inbox
```

Las importadas pasan a `importadas/` y las invalidas a `rechazadas/`. Para un
archivo suelto sigue disponible `--course ID --import-capture RUTA`.

Las capturas v2 incluyen los metadatos que el reproductor ya obtuvo (`play/info`):
hashes de la ocurrencia y del archivo, inicio UTC, duracion y tema de la reunion.
No hace falta `--sync-zoom`: si la clase no existe se crea con la misma identidad
que usa la sincronizacion por listado (hash de la ocurrencia), y si existe se
completa sin duplicarla. El curso se determina por la referencia del visor o, si
aun no se conoce, porque el tema coincide exactamente con el nombre del curso en
`biblioteca/cursos.json`; si no coincide queda `sin_metadatos_zoom` en la bandeja.
Una grabacion sin transcripcion (por ejemplo un examen) responde
`grabacion_sin_transcripcion` y no escribe nada.

Para actualizar solo lo reciente, abre unicamente las grabaciones de las ultimas
dos semanas cuya transcripcion aun no figure como `imported` en `grabaciones.json`.
Repetir una captura no duplica nada: se reporta `sin_cambios`.

El script lee el VTT preferentemente de la cache del navegador (`force-cache`),
asi que normalmente no repite la solicitud que ya hizo el reproductor.

El script solo reutiliza la solicitud VTT que el reproductor ya hizo: no
construye URLs ni tokens y no lee cookies; el navegador agrega su propia sesion.
La captura contiene el VTT original, el SHA-256 del path del visor y un id
derivado del `fid`; no contiene URLs, cookies ni cabeceras. Usa el mismo id y la
misma referencia que el import por cURL, asi que repetir una captura no duplica
la transcripcion y la asociacion con la clase funciona igual. El lote se valida
completo antes de escribir.

Si el script informa `vtt_no_observado...`, abre el panel de transcripcion,
espera a que cargue y vuelve a ejecutarlo. Si Zoom cierra la sesion o pide una
verificacion, deten la captura: no intentes evadir sus controles.

## Importar los cURL de transcripciones que copia el usuario

Alternativa manual a la captura del navegador.
El usuario abre los reproductores manualmente y copia las solicitudes VTT reales
desde Network. Python recibe esos cURL como datos y envia solamente sus GET de
transcripcion. No ejecuta comandos cURL ni reconstruye URLs o tokens.

Para importar directamente del portapapeles sin guardar solicitudes en el proyecto:

```powershell
Get-Clipboard -Raw | .\.venv\Scripts\python.exe conocimiento.py --course $cursoId --import-curl -
```

Puede recibir varios cURL Windows o POSIX. `--dry-run` valida su estructura sin
enviar solicitudes ni escribir archivos. Tambien admite la ruta de un adjunto
ya proporcionado por el usuario; no crear archivos de trabajo con credenciales.
Errores HTTP, HTML o redirects quedan pendientes; no se sigue un redirect con
las cookies de la solicitud original.

El hash del `referer` identifica el visor y se cruza con los metadatos capturados.
Una coincidencia unica asocia la transcripcion a su clase. Varias coincidencias,
fechas contradictorias o asociaciones manuales incompatibles quedan pendientes
para revision. Tambien se hace este cruce al actualizar los metadatos, por si el
VTT fue recibido primero.

Los originales y resultados ya procesados mantienen sus rutas de ingreso para
conservar enlaces y cache. `clase.json` referencia esos archivos y su manifest de
procesamiento; el indice pasa a mostrarlos bajo la fecha verificada. Un VTT sin
coincidencia permanece pendiente de fecha y puede procesarse igualmente.

## Procesar con Gemini Flash High y agy

Instala/autentica `agy` por separado y comprueba `agy models`. El modelo configurado
por defecto es `gemini-3.8-flash-high`, verificado en el CLI local. Puedes cambiarlo
en `agents.model`. Los parametros estan en `biblioteca.config.example.json`; no
subas tu configuracion local.

Para PDFs necesitas `pdftoppm` en PATH (Poppler u otra distribucion compatible),
o una ruta en `agents.pdf_renderer`. Python renderiza **cada pagina** y extrae su
texto; el agente debe abrir la imagen para interpretar texto incrustado, diagramas,
tablas y formulas. Una pagina sin revision visual valida queda pendiente.

```powershell
.\.venv\Scripts\python.exe procesar_conocimiento.py --course $cursoId --kind pdf --dry-run
.\.venv\Scripts\python.exe procesar_conocimiento.py --course $cursoId --kind transcript --dry-run
# Procesar un archivo completo como piloto:
.\.venv\Scripts\python.exe procesar_conocimiento.py --course $cursoId --kind pdf --limit 1
.\.venv\Scripts\python.exe procesar_conocimiento.py --course $cursoId --kind transcript --limit 1
# Piloto parcial de una sola pagina: el estado del PDF queda partial:
.\.venv\Scripts\python.exe procesar_conocimiento.py --course $cursoId --kind pdf --limit 1 --page-limit 1
# Piloto de un bloque de transcripcion; conserva estado partial:
.\.venv\Scripts\python.exe procesar_conocimiento.py --course $cursoId --kind transcript --limit 1 --block-limit 1
# Procesar solo las transcripciones pendientes de fecha, con tres tareas paralelas:
.\.venv\Scripts\python.exe procesar_conocimiento.py --course $cursoId --kind transcript --unassigned --workers 3
```

`--source-id` selecciona un recurso PDF (ID del elemento Canvas) o transcripcion
(el `id` de la transcripcion dentro de `transcripts` en `clase.json`, no el ID de la clase). `--model` prevalece sobre la configuracion. Repetir el comando
reanuda tareas por bloque/pagina; `--force` vuelve a ejecutarlas.
`--chunk-size` cambia el numero de segmentos por tarea. En transcripciones,
`--workers` admite 1-8 tareas concurrentes (una por defecto); las salidas se
ordenan por sus IDs originales y se guardan despues de cada bloque validado.

- `correct-transcript` conserva todos los segmentos, hablantes y tiempos.
  Clasifica texto academico, administrativo, social o incierto. `corregida.md`
  contiene todo; `estudio.md` selecciona los segmentos academicos. Las dudas y
  razones estan en `segmentos.json`; las dudas tambien aparecen junto al segmento
  correspondiente en los dos archivos Markdown.
- `interpret-pdf` genera una interpretacion por pagina y `interpretacion.md`.
  `estado.json` registra cuantas paginas se completaron; un piloto incompleto
  nunca se presenta como un PDF procesado completamente.

Python valida las salidas antes de almacenarlas como completas. Las tareas se
separan por fuente, SHA-256, modelo y version del skill. Los originales nunca se
reescriben por el agente. Los datos se envian al proveedor del modelo configurado.
El indice enlaza los resultados actuales y muestra su cobertura y estado.
En transcripciones, las carpetas usan un prefijo corto del hash para que los
ejecutables de Windows admitan la ruta; el hash completo se verifica en el manifest.
Las rutas, la adquisicion, las asociaciones y las validaciones son reglas Python;
la redaccion generativa puede variar entre ejecuciones y por eso se conserva su
procedencia. No hay un proceso de IA automatico en cada descarga.

## Actualizacion semanal

Actualiza Canvas, repite la organizacion/importacion o `--sync-zoom`, y ejecuta
los agentes cuando quieras incorporar fuentes nuevas. No hace falta reconstruir
la biblioteca. Los registros locales, materiales, transcripciones y resultados
estan ignorados por Git; si eliges rutas distintas, agrega sus exclusiones.
