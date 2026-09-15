# Pendientes: mapeo de impresoras por identidad (MAC / serial)

Proyecto: `"/Users/ecxviigrg/Local Storage/zebra-print-bridge-win"` (la ruta tiene un
espacio, cítala siempre entre comillas). Venv en `.venv`, usa `.venv/bin/python`.

Rama de trabajo: `feature/mac-identity-resolution`. Fecha de este documento: 2026-09-15.

## Estado actual

Commits ya hechos en la rama (solo en local, no subidos):

| Commit | Contenido |
|--------|-----------|
| `1187e94` | M1: caché indexada por identidad con migración v1 a v2 |
| `5106e33` | M2: MAC de la impresora vía SNMP `ifPhysAddress` |
| `35eabb6` | M3, M4, M5: `ResolvedTarget`, `resolve_target`, verificación de identidad, `printer_ip` como pista |

Estado verificado: `pytest` pasa 22 tests, `py_compile` limpio. Hay un cambio sin
commitear en `app/server.py` (import `Body` sin usar).

Lo que sigue está ordenado por prioridad. P0 va antes que cualquier otra cosa.

---

## P0. Aislar los tests del entorno real

**Problema.** `PrinterManager.__init__` fija `self.cache_dir` en
`~/.config/zebra-print-bridge/` (`app/printer_manager.py:370`) y los tests de
`tests/test_resolution.py` disparan `_save_cache()`. Cada ejecución de pytest
sobreescribe la caché real del usuario con impresoras falsas. Ya ocurrió: el
archivo real contiene solo `00:11:22:33:44:55` con serial `ZEB123`.

**Qué hacer.**
- Añadir parámetro `cache_dir: Optional[Path] = None` a `PrinterManager.__init__`.
  Si viene, usarlo; si no, el default actual.
- `PrintBridge.__init__` pasa `self.config.config_dir` como `cache_dir`, así la
  caché vive junto al `config.json` y respeta `--config`.
- Crear `tests/conftest.py` con un fixture `autouse` que:
  - fije `HOME` a un directorio temporal con `monkeypatch.setenv`, y
  - parchee `Path.home` para que devuelva ese directorio.
- En `tests/test_resolution.py`, `TestPrintBridgeResolution.setUp` construye
  `PrintBridge` con un `MagicMock(spec=Config)`. `config.scan_network` es un
  MagicMock, o sea truthy, y arranca el hilo de escaneo real. Definir
  explícitamente `config.scan_network = False`, `config.network_timeout = 0.1`,
  `config.log_level = "INFO"` y `config.config_dir = <tmp>`.
- Test nuevo: tras correr toda la suite, el directorio real
  `~/.config/zebra-print-bridge/` no cambia (comparar mtime o contenido antes y
  después dentro del fixture).

**Nota para el usuario.** La caché real se reconstruye sola con el escaneo de
fondo al abrir la app. No hace falta restaurarla a mano.

---

## P1. Correcciones a lo ya implementado

### P1.1 SNMP: cortar tras el primer timeout

`_query_snmp_mac` (`app/printer_manager.py:148`) hace GETNEXT y luego cuatro GET
en secuencia, cada uno con timeout de 0.3 s. Un dispositivo sin SNMP cuesta 1.5 s,
más 0.3 s del serial en `verify_device_identity`.

- Si la primera consulta SNMP a una IP agota el timeout sin respuesta alguna,
  marcar esa IP como "sin SNMP" y no lanzar las siguientes consultas en esa
  misma verificación.
- Cachear en memoria por IP el resultado "sin SNMP" con TTL de 10 minutos para
  no repetir el costo en cada job.
- Test: con un socket mockeado que siempre lanza `socket.timeout`, una
  verificación de identidad hace exactamente una consulta SNMP.

### P1.2 Eliminar la doble verificación en `/connection`

`PrintBridge.check_connection` llama `resolve_target` (que ya hace
`_check_port_open` y `verify_device_identity`) y después
`_test_network_connection(..., expected_mac, expected_serial)`
(`app/main.py:605`), que repite connect y verificación.

- Si `resolved.reachable and resolved.verified`, no volver a probar: construir la
  respuesta con los datos de `resolved`.
- Solo llamar `_test_network_connection` cuando la resolución no verificó
  (fuentes `manual`, `alias`, `dns`, `mdns`).
- Test: para una MAC en caché verificada, `_check_port_open` y
  `verify_device_identity` se invocan una sola vez en todo `check_connection`.

### P1.3 Hacer explícita la verificación "fail-open"

`verify_device_identity` (`app/printer_manager.py:833`) devuelve True cuando el
dispositivo no contesta SNMP ni aparece en ARP (docstring en la línea 845), y
`resolve_target` marca `verified=True` en ese caso.

- Cambiar el retorno a un tri-estado: `"match"`, `"mismatch"`, `"unverifiable"`.
- `ResolvedTarget.verified` solo es True con `"match"`. Añadir campo
  `verification: str` con el tri-estado.
- Con `"unverifiable"` se permite imprimir (comportamiento actual) pero se
  registra un `logger.warning` una vez por IP y `/connection` devuelve
  `"identity": "unverifiable"` en la respuesta.
- Nueva opción de config `strict_identity` (default False): si es True,
  `"unverifiable"` rechaza el job con 503.
- Tests para los tres estados y para `strict_identity=True`.

### P1.4 Eliminar el fallback muerto

`fallback_ip` (`app/main.py:493`, consumido en `app/main.py:339`) siempre es None:
`resolve_target` solo devuelve `use_local=True` cuando no venía `printer_ip`.

- Borrar `fallback_ip` y el bloque de fallback en `_process_queue`.
- Borrar `_build_network_printer` (`app/main.py:203`) y `_build_printer_target`
  (`app/main.py:217`), que ya no tienen llamadores, y los imports que queden sin
  uso en `app/main.py` (`is_valid_target`, `get_mac_for_ip`, otros).
- Quitar el import `Body` sin usar de `app/server.py:14`.

### P1.5 Hostname no resoluble: un solo escaneo

En la ruta de hostname (`app/printer_manager.py:1137` a `1176`) `resolve_target`
devuelve el hostname dentro de `ip` con `reachable=False`. `on_job_received` lo
toma como resuelto y `_test_network_connection` vuelve a llamar `resolve_target`,
lo que puede disparar un segundo `scan_subnet`.

- Añadir a `ResolvedTarget` el campo `resolved: bool`. En la ruta de hostname,
  si no se pudo resolver, `resolved=False` y `ip=None`, conservando el hostname
  original en un campo `host`.
- `on_job_received` y `check_connection` rechazan de inmediato cuando
  `resolved` es False, con el `message` de `ResolvedTarget`.
- `_test_network_connection` deja de llamar `resolve_target`; recibe siempre
  una IP o un hostname ya validado por DNS.
- Test: hostname inexistente con `scan_network=True` invoca `scan_subnet` una
  sola vez en todo el flujo de `on_job_received`, y `getaddrinfo` se invoca
  como máximo tres veces (nombre, `.local`, `.localdomain`).

### P1.6 GUI: entradas sin IP

Con la caché v2, una impresora conocida puede persistir con `ip=None`.
`_create_network_printer_card` (`app/gui.py:419`) muestra "● Online" en verde
siempre.

- Si `p["status"] == "offline"` o no hay IP: badge "● Offline" en `RED`,
  texto de IP "última IP desconocida" y botón Test deshabilitado.
- Mostrar `mac_source` junto a la MAC cuando exista, por ejemplo
  `MAC: 00:07:4D:6F:C2:14 (snmp)`.

---

## P2. Metas del plan original que no se hicieron

### M6. No bloquear el event loop

Todos los handlers de `app/server.py` siguen siendo `async def` y llaman código
bloqueante (subprocess, sockets, SNMP, `scan_subnet`). Con M2 a M5 el bloqueo por
request creció.

- Convertir a `def` (sin `async`) los handlers que llaman callbacks del bridge:
  `/status`, `/info`, `/connection`, `/print`, `/print/raw`, `/logs`,
  `/logs/clear`, `/printers`, `/printers/refresh`, `/printers/clear`.
  FastAPI los ejecuta en su threadpool. `/health`, `/`, `/dashboard` y
  `/test-client` pueden seguir `async`.
- `/print/raw` lee el body con `await request.body()`; al pasarlo a `def`, usar
  `Body(...)` con `media_type="text/plain"` o mantenerlo `async` y delegar el
  trabajo con `starlette.concurrency.run_in_threadpool`.
- Revisar que el estado compartido siga protegido por los locks existentes
  (`_jobs_lock`, `runtime_lock`, `usage_lock`, `_lock`, `_scan_lock`).
- Test con `httpx` y `TestClient`: mientras un `/connection` mockeado tarda
  2 s, un `/health` concurrente responde en menos de 200 ms.

### M7. MAC local cacheada y subprocess limpio

`app/utils.py` no cambió.

- `get_local_mac` (`app/utils.py:137`): caché en memoria con TTL de 60 s.
  `get_mac_for_ip` (`:242`) y `get_ip_for_mac` (`:265`) usan la versión cacheada.
- `/status` llama `get_local_mac` dos veces: `app/main.py:694` y
  `app/server.py:174`. Dejar solo la de `server.py`.
- Helper `run_command(cmd, timeout=5) -> str` en `app/utils.py`:
  - en Windows, `creationflags=subprocess.CREATE_NO_WINDOW`;
  - captura bytes y decodifica con `errors="ignore"` (los campos parseados son
    ASCII);
  - `stderr=DEVNULL`, timeout, y devuelve `""` ante cualquier excepción.
  Usarlo en las llamadas de `app/utils.py:150`, `:184`, `:201`, `:252`, `:277`,
  en `get_local_subnets` y en las llamadas a `lpstat` de `printer_manager.py`.
  `lp` recibe stdin, tratarlo aparte pero con el mismo `creationflags`.
- Fallback `uuid.getnode()` (`app/utils.py:232`): descartar si
  `(node >> 40) & 1 == 1` (bit multicast, número aleatorio).
- Tests con salidas reales grabadas: `ifconfig` de macOS, `ipconfig /all` en
  inglés y en español con acentos, `arp -an` y `arp -a`. Test de que N llamadas
  a `get_local_mac` ejecutan un solo subprocess. Test del bit multicast.

### M9. Documentación alineada con el código

- README, sección "Resolution & Fallback Hierarchy" (`README.md:317`) y tabla de
  parámetros de `POST /print`: jerarquía real `printer_mac` > `printer_ip` /
  `printer_host` > `printer_name` > impresora predeterminada del SO. Explicar el
  rol de `printer_ip` como pista cuando acompaña a `printer_mac`, la
  verificación de identidad y sus tres estados, `verify_identity` y
  `strict_identity`.
- Documentar el formato de caché v2 y su ubicación.
- Campos de config (`README.md:98`): quitar `web_interface` y `auto_start`;
  listar los reales: `port`, `scan_network`, `network_timeout`, `log_level`,
  `saved_printers`, `printer_aliases`, `custom_subnets`, `verify_identity`,
  `strict_identity`.
- Renumerar "Option A" / "Option C" (`README.md:36`, `:42`).
- `/info` en `app/server.py:230`: `required_fields` y `supported_targets`
  coherentes con la jerarquía real. Añadir `"identity_verification"` con los
  valores de config activos.
- Ejemplo curl de `POST /print` con `printer_mac` y `printer_ip` como pista.

### M8. Descubrimiento por broadcast Zebra (opcional)

Sin cambios respecto al plan original. Solo si se confirma el formato del
paquete UDP 4201 con documentación fiable (Link-OS SDK, `NetworkDiscoverer`,
`DiscoveryPacketDecoder`). Detrás de `discovery_broadcast` (default False). Si no
se confirma, dejar `discover_broadcast()` como stub con TODO y la razón.

---

## Restricciones

- **INVARIANTE:** nunca enviar bytes al puerto 9100 en descubrimiento ni
  verificación. Solo `connect_ex`. Ningún `send`, `sendall`, SGD ni `~HS`/`~HI`.
- **CERO IMPRESIONES REALES:** nada de `POST /print` ni `/print/raw` con un
  destino distinto de `"test"`. `send_zpl`, `_send_network`, `_send_win32`,
  `_send_cups` y `lp` van mockeados en tests. Si crees que necesitas una
  impresora real, detente y repórtalo como pendiente de validación manual.
- Compatibilidad de API: no renombrar ni quitar campos existentes. Solo añadir.
- No modificar `app/__init__.py` ni `version.json`. No hacer push.
- Commits pequeños por ítem en `feature/mac-identity-resolution`, empezando por
  P0 y el commit de limpieza del `Body` sin usar.
- Mantener macOS (CUPS, `ifconfig`, `arp -an`) y Windows (`win32print`,
  `ipconfig`, `arp -a`). Linux best effort.

## Verificación

1. `.venv/bin/python -m pip install -r requirements-dev.txt`
2. `.venv/bin/python -m pytest -q` en verde, y verificar que
   `~/.config/zebra-print-bridge/network_printers.json` no cambió de mtime.
3. `.venv/bin/python -m py_compile app/*.py build.py run_gui.py`
4. Smoke test con la red desactivada: crear un directorio temporal con
   `config.json` que contenga `{"scan_network": false}` y arrancar
   `.venv/bin/python app/main.py --port 5055 --config <dir>`. Con curl:
   - `POST /print` con `printer_ip: "test"` encola;
   - `GET /connection?printer_mac=00:11:22:33:44:55` responde 503 en menos de
     3 s con mensaje claro;
   - `GET /health` responde en menos de 200 ms mientras corre el anterior
     (segunda terminal);
   - `GET /info` muestra la jerarquía nueva y `identity_verification`.
   Detener el servidor al terminar. Reportar la prueba de concurrencia bajo
   escaneo real como no ejecutada.

## Definición de terminado

- P0, P1.1 a P1.6, M6, M7 y M9 implementados. M8 implementado o stub con razón.
- pytest y py_compile en verde; smoke test ejecutado y reportado tal cual.
- Commits en la rama, sin push.
- Resumen final: cambios por archivo, decisiones tomadas, qué queda pendiente de
  validar con una impresora Zebra real y qué probar en Windows antes de publicar.
