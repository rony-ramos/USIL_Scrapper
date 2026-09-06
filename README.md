# Biblioteca de cursos USIL

## Configuración acordada

La carpeta raíz está definida en `biblioteca.config.json`:

`A:\USIL CS\USIL Scrapper\biblioteca`

Organización: período / curso / unidad / semana / archivo.
La actualización será manual mediante «Actualizar biblioteca», con uso semanal.
No hay una tarea programada.

## Estado

Se verificó el inventario de módulos de Canvas y la descarga de un PDF de
SharePoint a la carpeta predeterminada del navegador. `piloto.py` implementa
la prueba de descarga directa. Solo se considera validada cuando produce
`biblioteca/validacion-piloto.json`. No cambia el perfil habitual de Chrome.

El descargador deberá determinar la ruta antes de transferir cada archivo,
sanear los nombres para Windows y validar que el destino permanezca dentro
de la raíz. Los archivos temporales de descarga deben permanecer junto al
destino final. El catálogo deberá conservar los IDs de Canvas, fuente,
ruta, estado y versiones para evitar duplicados y actualizar pendientes.

## Ejecutar el piloto

Requiere Windows, Chrome instalado y Python 3.12. Desde esta carpeta:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe piloto.py --plan
.\.venv\Scripts\python.exe piloto.py
```

Microsoft puede pedir inicio de sesión/MFA en la ventana visible del perfil
exclusivo `.browser-profile`. El programa espera hasta cinco minutos. Este
perfil puede conservar la sesión y está excluido de Git. No se exportan cookies.

Chrome recibe la ruta final antes del clic mediante `Browser.setDownloadBehavior`
con `allowAndName`. Escribe el temporal de la descarga y el archivo completado
en esa carpeta. Python valida el PDF y lo renombra dentro del mismo directorio.
No usa `download.save_as()` ni mueve un archivo desde Descargas.

El piloto exige 39 páginas. Opcionalmente acepta `--reference RUTA_PDF` para
comparar SHA-256. Una segunda ejecución verifica la huella local y omite la
descarga registrada. Este comportamiento no comprueba cambios remotos: la
sincronización semanal, versiones, reintentos y el botón todavía no están
implementados. Un archivo existente no registrado no se sobrescribe. Los
errores dejan el piloto incompleto y pueden dejar temporales para diagnóstico.

Pruebas locales: `.\.venv\Scripts\python.exe -m unittest -v`.

Referencias: https://playwright.dev/python/docs/api/class-browsertype y
https://chromedevtools.github.io/devtools-protocol/tot/Browser/#method-setDownloadBehavior

## Piloto con requests

`piloto_requests.py` usa el enlace de descarga observado para este PDF concreto.
Reutiliza cookies del perfil exclusivo en memoria y cierra Chrome antes de
descargar por bloques con requests. No exporta cookies ni controla clics de
descarga. Solo admite HTTPS y redirecciones dentro del host de SharePoint.

```powershell
.\.venv\Scripts\python.exe piloto_requests.py
# Si aparece AUTH_REQUIRED, renovar acceso en el perfil exclusivo:
.\.venv\Scripts\python.exe piloto_requests.py --login
```

El archivo `.pdf.part` se escribe junto al PDF final. Se valida antes de
renombrar y registrar. `validacion-requests.json` con estado
`downloaded_requests` demuestra el éxito de esta modalidad; una redirección
de autenticación o HTML no se considera descarga válida. Las cookies
disponibles no implican que la sesión siga autenticada.

## SeleniumBase + API Canvas

```powershell
.\.venv\Scripts\python.exe canvas_sync.py --course 79493 --download
# Inventario y contenidos de todos los IDs seleccionados:
.\.venv\Scripts\python.exe canvas_sync.py
# También descargar archivos compatibles:
.\.venv\Scripts\python.exe canvas_sync.py --download
```

Usa SeleniumBase estándar y el perfil exclusivo del piloto. No se conecta a
Chrome habitual si no fue iniciado con una interfaz de automatización. No
cierra ese navegador ni lee su base de cookies. La primera ejecución puede
descargar ChromeDriver oficial y pedir inicio de sesión USIL/Microsoft.
Las cookies relevantes se transfieren a requests exclusivamente en memoria.

`cursos_seleccionados.json` contiene la selección explícita de TI/software.
Se siguen los encabezados Link de Canvas y se consultan todos los elementos
de cada módulo. Se guardan páginas e instrucciones de tareas como JSON;
los enlaces internos de esos cuerpos todavía no se recorren. Cuestionarios
y foros se inventarían sin consultar respuestas o participaciones.

La estructura actual usa período / curso con ID / módulo / encabezado de
semana. Los IDs en archivos evitan colisiones. Los archivos que cambian se
conservan en `versiones`; los idénticos se detectan por SHA-256 después de
consultar su contenido. Los PDFs SharePoint se resuelven desde el endpoint
observado en su visor y se descargan por requests. Otros tipos o destinos
requieren conectores adicionales y quedan pendientes. Una descarga fallida
no se declara exitosa. `recursos.json` describe el resultado por recurso.

La implementación está en validación; no garantiza todavía cubrir archivos
fuera de módulos ni todos los enlaces externos. No hay botón ni programación
semanal instalados. `ultima-sincronizacion.json` y `sync.sqlite3` registran
las ejecuciones que pudieron realizarse.
