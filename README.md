# Biblioteca de cursos Canvas

Cliente local en Python: SeleniumBase permite iniciar sesión; `requests` consulta
la API de Canvas y descarga los materiales. Cada persona utiliza su propia
cuenta, perfil de Chrome y configuración. No requiere claves o rutas del autor.

## Requisitos

- Python 3.12 y Google Chrome instalado.
- Acceso a los cursos mediante una cuenta de la institución.
- Validado en Windows. Otras plataformas requieren su propia validación.

## Instalación y configuración (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe configurar.py
```

El último comando copia los ejemplos **sin sobrescribir** configuraciones
existentes. Edita `biblioteca.config.json` para indicar:

| Campo | Uso |
|---|---|
| `canvas_url` | Origen HTTPS de Canvas de tu institución |
| `root` | Carpeta para la biblioteca |
| `browser_profile` | Carpeta exclusiva del perfil de automatización |
| `courses_file` | JSON local con los IDs de los cursos seleccionados |
| `pilot_file` | Recurso local opcional para las pruebas piloto |
| `sharepoint_hosts` | Hosts exactos de SharePoint usados por tus materiales |
| `download_hosts` | Hosts adicionales de descarga/CDN autorizados |

Las rutas relativas se resuelven respecto al archivo de configuración, incluso
si ejecutas Python desde otra carpeta. También se admiten rutas absolutas
locales. Usa `--config RUTA` para elegir otra configuración. Si cambias las
carpetas dentro del repositorio, agrégalas a `.gitignore`; también puedes
ubicarlas fuera del repositorio.

## Elegir y descargar cursos

```powershell
# Iniciar sesión y consultar todos los cursos, incluyendo paginación:
.\.venv\Scripts\python.exe canvas_sync.py --list-courses
```

La lista se guarda en `biblioteca/cursos.json`. Copia los IDs deseados al campo
`course_ids` de `cursos_seleccionados.json`. El ejemplo tiene una lista vacía
para evitar descargar cursos por accidente.

```powershell
# Consultar estructura y guardar páginas e instrucciones:
.\.venv\Scripts\python.exe canvas_sync.py
# También descargar los materiales compatibles:
.\.venv\Scripts\python.exe canvas_sync.py --download
# Limitar una ejecución a un ID de tu lista:
.\.venv\Scripts\python.exe canvas_sync.py --course 123 --download
```

ChromeDriver se descarga de su fuente oficial si hace falta. El navegador usa
un perfil exclusivo y puede pedir inicio de sesión/MFA; no se conecta al Chrome
habitual. Las cookies aplicables se transfieren a `requests` en memoria. No se
escriben archivos de cookies ni se imprimen credenciales. La sesión permanece
en el perfil local. No ejecutes dos procesos simultáneos con ese perfil.

## Organización y actualización

Los archivos se guardan directamente por período / curso con ID / módulo /
semana. Los IDs de los recursos evitan colisiones. La descarga escribe primero
un `.part` en la carpeta final, valida el contenido y lo renombra en esa misma
carpeta. No utiliza Descargas como paso intermedio.

Repite el comando semanalmente. Los archivos idénticos se comparan con SHA-256
tras descargarlos; los cambios se conservan en `versiones`. Las páginas e
instrucciones JSON se actualizan y todavía no tienen historial de versiones.
El botón de actualización y la programación automática no están implementados.

`recursos.json` describe el resultado por recurso. `ultima-sincronizacion.json`
registra el lote más reciente y `sync.sqlite3` conserva los informes de ejecución.
Un estado `inventariado` del curso no significa que todos sus archivos se hayan
descargado; revisa los estados individuales y los contadores del informe.

## Monitoreo y diagnóstico

Para supervisar el avance de las descargas e identificar qué cursos tienen pocos PDFs o incidencias de acceso:

```powershell
# Diagnóstico general con umbral predeterminado (<= 5 PDFs):
.\.venv\Scripts\python.exe detectar_pocos_pdfs.py

# Ajustar el umbral de alerta (ejemplo: cursos con <= 10 PDFs):
.\.venv\Scripts\python.exe detectar_pocos_pdfs.py --umbral 10

# Consultar un curso puntual por ID:
.\.venv\Scripts\python.exe detectar_pocos_pdfs.py --course 58245

# Evaluar todos los cursos del inventario (no solo los seleccionados):
.\.venv\Scripts\python.exe detectar_pocos_pdfs.py --all
```

El script cuenta los PDFs reales en disco (omitiendo temporales y versiones archivadas), cruza los registros de `recursos.json` y desglosa las causas: enlaces bloqueados por permisos de SharePoint (`sin_acceso_sharepoint`), archivos eliminados o rotos (`no_encontrado_sharepoint`), enlaces de Office 365 que requieren otros conectores (`pendiente_conector`), o cursos cuyo contenido principal son páginas/tareas digitales de Canvas.

## Alcance actual

- Cursos, módulos y todos sus elementos, siguiendo los enlaces de paginación.
- Contenido de páginas e instrucciones de tareas como JSON.
- Archivos de Canvas y PDFs enlazados de SharePoint (`/:b:/` o enlaces directos con extensión `.pdf`) cuyo visor permite
  resolver un endpoint de descarga.
- Pendientes explícitos para accesos fallidos, enlaces incompletos y destinos
  que requieren otros conectores.

Los archivos de Canvas también pueden usar un host de descarga devuelto por
su API autenticada. Redirecciones adicionales requieren un host permitido
en `download_hosts`. Para volver a intentar solo los archivos Canvas pendientes
del inventario local, usa `canvas_sync.py --retry-files`.

Las páginas de error de SharePoint (`ms-error-body` o respuestas directas en texto plano
como `404 NOT FOUND` y `403 FORBIDDEN`) se detectan de inmediato sin esperar a que venza
el tiempo de carga del visor. Si el archivo ya no existe, queda registrado como
`no_encontrado_sharepoint`. Si el error es de permisos, queda como `sin_acceso_sharepoint`,
con el mensaje visible y fecha de comprobación, conservando el enlace. La próxima
actualización completa vuelve a comprobarlos.

Para reanudar un lote interrumpido puedes añadir `--resume`: verifica los
archivos existentes por SHA-256 (no los descarga otra vez) y omite los enlaces
de SharePoint ya clasificados como `no_encontrado_sharepoint` o `sin_acceso_sharepoint`
para agilizar la reanudación. Usa la ejecución normal, sin `--resume`, en las actualizaciones
semanales para comprobar cambios remotos incluso cuando la URL siga siendo la misma.

No recorre todavía archivos fuera de módulos ni enlaces dentro de los cuerpos
de páginas/tareas. No descarga participaciones, respuestas de cuestionarios ni
otros formatos de SharePoint. Los hosts de CDN adicionales se configuran
localmente; no se siguen redirecciones a destinos arbitrarios.

## Pilotos opcionales

`piloto.py` y `piloto_requests.py` sirven para probar un PDF individual. Crea
`piloto.json` desde `piloto.example.json` y configura un enlace propio, el número
de páginas y (para requests) el endpoint realmente observado en el visor.

```powershell
.\.venv\Scripts\python.exe piloto.py --plan
.\.venv\Scripts\python.exe piloto_requests.py --login
```

Ambos usan la configuración local predeterminada. `--reference RUTA_PDF` compara
el archivo con una copia conocida. Estos pilotos no reemplazan al sincronizador.

## Git y pruebas

Se versionan código, dependencias y archivos `*.example.json`. Las configuraciones
locales, el perfil, los materiales y los registros están excluidos de Git.
Agregar algo a `.gitignore` no lo elimina de commits anteriores.

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

Las pruebas locales cubren paginación, aislamiento de hosts, HTML de login,
rutas, validación PDF y versiones. La autenticación real requiere tu cuenta.

Documentación: [SeleniumBase](https://seleniumbase.io/),
[Canvas API](https://developerdocs.instructure.com/services/canvas),
[Chrome Download Behavior](https://chromedevtools.github.io/devtools-protocol/tot/Browser/#method-setDownloadBehavior).
