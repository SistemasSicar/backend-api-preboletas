# API de Preboletas

API (FastAPI, Python 3.12+) de Preboletas de SICAR, en el puerto **7230**. Es la **única** pieza que habla con la base `sicar` y con Dataverse:

- **Escribe** en el esquema `preboletas`.
- **Lee** el esquema `core`.
- **Consulta** proveedores y frutas directo en Dataverse.

El panel (7301) y la app móvil solo hablan con esta API.

## Instalación

```cmd
cd backend-api-preboletas
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
```

## Configuración

Copia `.env.example` en dos archivos. Ninguno va a Git.

- **`.env`:** configuración sin secretos.
- **`.env.local`:** secretos (`BD_URL`, `CORE_API_KEY`, `DATAVERSE_CLIENT_ID`, `DATAVERSE_CLIENT_SECRET`).

El detalle de cada variable está en `CLAUDE.md` §8.

Revisa (solo lectura) que la base tenga lo necesario:

```cmd
.venv\Scripts\python -m scripts.revisar_catalogos
```

El script revisa:
- los tipos de movimiento y de pago;
- la app `preboletas` en `core.apps`;
- la fecha de implementación, la tolerancia y las cajas por unidad.

## Arrancar

```cmd
.venv\Scripts\uvicorn app.main:crear_app --factory --reload --port 7230
```

- **Swagger:** <http://localhost:7230/docs> (se apaga en producción).
- **Contrato:** `openapi.json`. Se regenera con `.venv\Scripts\python -m scripts.exportar_openapi`.
- **Salud:** `GET /salud` muestra la base y el estado de las escuchas.
- **Un solo worker:** corre con uno. Los tickets del WebSocket viven en memoria.

Al arrancar se levantan cuatro hilos:

| Hilo | Qué hace |
|---|---|
| `escucha-core_boletas` | `LISTEN core_boletas` → el motor liga cada boleta de D365 |
| `barrido-boletas` | Cada 5 min repasa las boletas de las últimas 48 h. Usa el candado `72300001`, así que corre una sola copia |
| `escucha-preboletas_eventos` | `LISTEN preboletas_eventos` → reparte los eventos por WebSocket según el rol |
| `envio-notificaciones` | Cada 30 s envía los avisos por push (Expo). El correo queda `fallida` / `correo_no_configurado` mientras no haya buzón |

## Pruebas, tipos y estilo

Las pruebas usan un **PostgreSQL real** temporal; nunca tocan `sicar`. Cada corrida:
1. crea `preboletas_pruebas_<azar>` a partir de `database/esquema_sicar.sql`;
2. copia los catálogos de `sicar` (solo lectura);
3. opera con el rol `preboletas_api`;
4. borra la base al terminar.

`PRUEBAS_BD_ADMIN_URL` (usuario `postgres` o uno que pueda crear bases) va en `.env.local` o en la terminal:

```cmd
.venv\Scripts\pytest
.venv\Scripts\mypy app tests scripts
.venv\Scripts\ruff check app tests scripts
```

Sin `PRUEBAS_BD_ADMIN_URL`, solo corren las pruebas que no usan la base.

## Probar el flujo completo a mano

1. En el panel, o con Swagger y un token del registro "Preboletas", prepara una unidad:
   - Admin: fecha de implementación (`PUT /unidades/{id}/implementacion`).
   - Admin: tolerancia del limón (`PUT /unidades/{id}/tolerancias/01`).
   - Admin: una caja permitida para el limón (`POST /tipos-caja` y `PUT /tipos-caja/{id}/frutas/01`).
2. Como Comprador, crea la preboleta (`POST /preboletas`).
3. Como Basculista, indica el peso (`POST /preboletas/{id}/recepciones`). La báscula queda OCUPADA.
4. Manda la boleta como lo haría Power Automate: `POST /boletas` de **CoreAPI** con la llave `LLAVE_POWERAUTOMATE`.
   - Primero con bruto > 0: el motor liga la boleta y libera la báscula.
   - Después con tara: la preboleta se cierra.

## Estructura

```
app/
  main.py            Fábrica, hilos de fondo y /salud
  config.py          Variables de entorno (.env y .env.local)
  bd.py              Pool de psycopg (síncrono) y conexiones dedicadas para LISTEN
  errores.py         Errores { codigo, mensaje, id_solicitud }
  dependencias.py    Servicios, IdentidadActual, UsuarioActual
  usuario.py         Roles y permisos (Admin, Comprador, Basculista)
  seguridad/         Token de Entra (local), tickets de WebSocket, límite de la página pública
  integraciones/     Dataverse (SDK), CoreAPI (/internas/acceso/verificar), Expo push
  servicios/         Reglas de negocio: preboletas, cantidad (incidencias y cierres), recepciones,
                     propuestas, conciliación, configuración, colaboradores, consultas
  motor/             Motor de boletas, escuchas de NOTIFY y barrido
  trabajos/          Envío de avisos por correo y push
  tiempo_real/       Hub de WebSocket (reparto por rol)
  rutas/             Endpoints por tema
scripts/             exportar_openapi, revisar_catalogos
tests/               pytest contra PostgreSQL temporal
```

## Reglas que conviene saber

- **Login sin depender de CoreAPI:** `GET /yo` reconoce al colaborador **leyendo** `core.colaboradores`.
  - Primero por su cuenta de Microsoft (`entra_oid`).
  - Si aún no se vincula, por su correo, entre los colaboradores sin cuenta ligada.
  - Si el correo pertenece a un colaborador ligado a otra cuenta de Microsoft, no entra.

  Después le pide a CoreAPI **en segundo plano** que guarde el vínculo, el nombre y la copia de roles. Si CoreAPI está caída, la gente igual entra. Solo se atrasa la copia de roles, que se usa para los avisos a Admin y basculistas, y se pone al día en el siguiente login. Dar de alta colaboradores sí requiere CoreAPI (panel de SICAR Core).

- **Visibilidad:**
  - Admin ve todo.
  - Comprador ve sus preboletas; las incidencias de cantidad y las conciliaciones, de todas las unidades.
  - Basculista ve su unidad.
  - Lo demás responde **404**.
- **Bitácora:** cada acción deja un `movimiento`, con quién la hizo o "Sistema". Los avisos de tiempo real salen con `pg_notify` en la misma transacción, así nunca se avisa algo que se revirtió.
- **Orden de candados:** siempre báscula → carga → preboleta. Así ni el motor ni las peticiones se bloquean entre sí.
- **Correcciones al prototipo** (acordadas en el plan):
  1. Una sola sobre-entrega pendiente a la vez.
  2. Al reabrir siempre se cancelan el faltante y el cierre a mano vigentes.
  3. Las cajas se corrigen mientras la boleta no esté cerrada en D365.
  4. Una propuesta igual al precio del día vuelve a seguirlo.
  5. La unidad es obligatoria al crear.
- **Boletas atrasadas:** el camión que está en la báscula solo se liga a una boleta si entró antes de la 1ra pesada de esa boleta (con 10 min de tolerancia de reloj).
