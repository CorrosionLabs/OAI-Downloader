He revisado los **15 archivos Python de `oai_downloader`**. En conjunto forman una herramienta para respaldar conversaciones y recursos de ChatGPT, organizarlos por proyecto y comprobar descargas fallidas.

| Archivo | Qué hace |
|---|---|
| `__init__.py` | Está vacío. Identifica la carpeta como un paquete Python. |
| `config.py` | Centraliza nombre y versión de la aplicación, direcciones de los servicios, tiempos de espera entre descargas, límites de reintentos y criterios para detectar recursos. |
| `paths.py` | Define dónde se guardan las cookies, conversaciones, recursos, inventarios y estados. Crea rutas independientes por proyecto y limpia los caracteres no válidos de sus nombres. |
| `cli.py` | Define los argumentos de consola: descargar conversaciones, listar proyectos, generar inventarios, auditar, probar recursos y gestionar Library. También adapta la salida de consola para evitar errores con caracteres incompatibles. |
| `auth.py` | Lee la cookie guardada, obtiene el token de acceso de la sesión y reconoce errores de autenticación que requieren detener el proceso. |
| `http.py` | Proporciona las peticiones de red mediante `curl_cffi`: consultas JSON, descarga de binarios, peticiones POST y pruebas de acceso. También construye URLs e interpreta el encabezado `Retry-After`. |
| `errors.py` | Define las excepciones propias: errores generales, fallos de autenticación, cancelación global por autenticación y errores temporales que permiten reintentar. |
| `state.py` | Lee y guarda el progreso de conversaciones y recursos: completados, fallidos y correspondencias entre identificadores de Library y archivos. Permite continuar trabajos anteriores; guarda el estado de recursos mediante un archivo temporal y posterior reemplazo. |
| `conversations.py` | Enumera conversaciones activas, archivadas o de un proyecto y descarga su contenido completo como JSON. Permite descargar una muestra, omitir archivos existentes o actualizarlos forzosamente. |
| `projects.py` | Lista y busca proyectos, obtiene sus conversaciones y coordina el respaldo completo de cada uno: **conversaciones → inventario → recursos**. Puede procesar varios proyectos simultáneamente con `--workers`. |
| `inventory.py` | Analiza los JSON locales para detectar archivos adjuntos, imágenes y audio. Extrae identificadores y contexto, elimina referencias duplicadas y genera `resource_inventory.json`. Incluye una inspección exploratoria de las claves de los JSON. |
| `resources.py` | Implementa la descarga de recursos: obtiene URLs, resuelve `library_file_id` a `file_id`, determina extensiones y guarda los archivos. Gestiona reintentos, pausas, errores de autenticación y reanudación de archivos `.part` mediante peticiones por rango. |
| `audits.py` | Clasifica descargas fallidas y referencias pendientes. También comprueba si los recursos siguen siendo accesibles mediante una lectura mínima del binario. Genera `resource_failure_audit.json` y `resolution_audit.json`. |
| `probes.py` | Agrupa las pruebas de diagnóstico: comprobar sesión, contar conversaciones y proyectos, descargar un recurso de prueba, ensayar rutas de resolución y verificar archivos por identificador. Puede conservar una correspondencia recuperada entre Library y un archivo. |
| `library.py` | Busca y lista PDFs en los inventarios locales, localiza copias descargadas y resuelve identificadores de Library. Incluye la selección de un PDF para una prueba de borrado y una función de **borrado lógico remoto real**, que exige escribir `BORRAR`. |

El punto de entrada está fuera de esa carpeta: `main.py` interpreta las opciones definidas en `cli.py` y llama a la función correspondiente. Para entender el funcionamiento principal, el orden de lectura más útil es **`conversations.py` → `inventory.py` → `resources.py`**, seguido de `projects.py` para ver cómo se coordina todo.