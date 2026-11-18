"""
Colaboradores vistos desde Preboletas: quién es quien hace la petición, y la administración
de sus permisos por unidad y sus canales de aviso (solo Admin).

Los colaboradores y sus roles son de `core` (los escribe CoreAPI): aquí solo se leen.
"""

from __future__ import annotations

from typing import Any

from app.bd import Conexion
from app.config import CLAVE_APP
from app.errores import AccesoDenegado, NoEncontrado, SolicitudInvalida
from app.seguridad.token import IdentidadToken
from app.servicios.comun import Operacion, nombre_colaborador, nombre_unidad
from app.usuario import PERMISOS, ROLES_APP, Usuario


def cargar_usuario(con: Conexion, identidad: IdentidadToken) -> Usuario | None:
    """
    Colaborador de la cuenta de Microsoft, con sus permisos. SOLO LEE `core` (no depende de CoreAPI).

    1. Por entra_oid: la cuenta ya está vinculada.
    2. Si no, por el correo del token entre los colaboradores AÚN SIN cuenta ligada: es alguien dado de
       alta que todavía no se vincula (la misma regla que usa CoreAPI la primera vez). El vínculo lo
       guarda CoreAPI después, en segundo plano.
    Si el correo es de un colaborador ligado a OTRA cuenta de Microsoft, no entra (la cuenta se
    reasignó y un administrador debe desvincularla). None = sin vinculación.
    """
    fila = con.execute(
        "SELECT id, nombre, correo, unidad_negocio_id FROM core.colaboradores WHERE entra_oid = %s",
        (identidad.entra_oid,),
    ).fetchone()
    if fila is None and identidad.correo:
        fila = con.execute(
            "SELECT id, nombre, correo, unidad_negocio_id FROM core.colaboradores "
            "WHERE correo = %s AND entra_oid IS NULL",
            (identidad.correo.strip().lower(),),
        ).fetchone()
    if fila is None:
        return None
    permisos = con.execute(
        "SELECT permiso, unidad_negocio_id FROM preboletas.permisos_colaborador WHERE colaborador_id = %s",
        (fila["id"],),
    ).fetchall()
    return Usuario(
        colaborador_id=int(fila["id"]),
        nombre=str(fila["nombre"] or identidad.nombre),
        correo=str(fila["correo"]),
        unidad_negocio_id=int(fila["unidad_negocio_id"]),
        # Los roles salen del token (Azure manda); la copia en core solo sirve para avisar.
        roles=frozenset(r for r in identidad.roles if r in ROLES_APP),
        permisos=frozenset((str(p["permiso"]), int(p["unidad_negocio_id"])) for p in permisos),
    )


def perfil(con: Conexion, usuario: Usuario) -> dict[str, Any]:
    """Respuesta de GET /yo."""
    canales = con.execute(
        "SELECT canal FROM preboletas.canales_colaborador WHERE colaborador_id = %s ORDER BY canal",
        (usuario.colaborador_id,),
    ).fetchall()
    return {
        "colaborador_id": usuario.colaborador_id,
        "nombre": usuario.nombre,
        "correo": usuario.correo,
        "unidad_negocio_id": usuario.unidad_negocio_id,
        "unidad": nombre_unidad(con, usuario.unidad_negocio_id),
        "roles": sorted(usuario.roles),
        "permisos": [{"permiso": p, "unidad_negocio_id": u} for p, u in sorted(usuario.permisos)],
        "canales": ["app", *[str(c["canal"]) for c in canales]],
    }


def listar(con: Conexion, *, rol: str | None, buscar: str | None) -> list[dict[str, Any]]:
    """Colaboradores con algún rol de Preboletas (para elegir comprador o filtrar la bitácora)."""
    condiciones = ["true"]
    params: list[Any] = [CLAVE_APP]
    if rol:
        condiciones.append("%s = ANY(roles)")
        params.append(rol)
    if buscar:
        condiciones.append("(nombre ILIKE %s OR correo ILIKE %s)")
        params += [f"%{buscar}%", f"%{buscar}%"]
    filas = con.execute(
        f"""
        SELECT * FROM (
            SELECT c.id, COALESCE(c.nombre, c.correo) AS nombre, c.correo, c.unidad_negocio_id,
                   u.nombre AS unidad, array_agg(ca.rol ORDER BY ca.rol) AS roles
              FROM core.colaboradores c
              JOIN core.colaborador_acceso ca ON ca.colaborador_id = c.id
              JOIN core.apps a ON a.id = ca.app_id AND a.clave = %s
              JOIN core.unidades_negocio u ON u.id = c.unidad_negocio_id
             GROUP BY c.id, u.nombre
        ) x
         WHERE {" AND ".join(condiciones)}
         ORDER BY nombre
        """,
        params,
    ).fetchall()
    return [dict(f) for f in filas]


def listar_admin(con: Conexion, usuario: Usuario, buscar: str | None) -> list[dict[str, Any]]:
    """Para la pantalla de Colaboradores: roles, canales, teléfonos activos y permisos por unidad."""
    if not usuario.es_admin:
        raise AccesoDenegado("Solo un Admin administra colaboradores.")
    base = listar(con, rol=None, buscar=buscar)
    if not base:
        return []
    ids = [c["id"] for c in base]
    canales: dict[int, list[str]] = {}
    for f in con.execute(
        "SELECT colaborador_id, canal FROM preboletas.canales_colaborador WHERE colaborador_id = ANY(%s)", (ids,)
    ).fetchall():
        canales.setdefault(int(f["colaborador_id"]), []).append(str(f["canal"]))
    dispositivos = {
        int(f["colaborador_id"]): int(f["n"])
        for f in con.execute(
            "SELECT colaborador_id, count(*) AS n FROM preboletas.dispositivos_push WHERE activo "
            "AND colaborador_id = ANY(%s) GROUP BY colaborador_id",
            (ids,),
        ).fetchall()
    }
    permisos: dict[int, list[dict[str, Any]]] = {}
    for f in con.execute(
        "SELECT colaborador_id, permiso, unidad_negocio_id FROM preboletas.permisos_colaborador "
        "WHERE colaborador_id = ANY(%s)",
        (ids,),
    ).fetchall():
        permisos.setdefault(int(f["colaborador_id"]), []).append(
            {"permiso": f["permiso"], "unidad_negocio_id": f["unidad_negocio_id"]}
        )
    return [
        {
            **c,
            "canales": sorted(canales.get(c["id"], [])),
            "dispositivos_activos": dispositivos.get(c["id"], 0),
            "permisos": permisos.get(c["id"], []),
        }
        for c in base
    ]


def _validar_colaborador(con: Conexion, colaborador_id: int) -> str:
    if con.execute("SELECT 1 FROM core.colaboradores WHERE id = %s", (colaborador_id,)).fetchone() is None:
        raise NoEncontrado("El colaborador no existe.")
    return nombre_colaborador(con, colaborador_id)


def cambiar_permiso(
    op: Operacion, usuario: Usuario, colaborador_id: int, permiso: str, unidad_negocio_id: int, otorgar: bool
) -> None:
    """Dar o quitar un permiso por unidad. Quitar = borrar la fila (queda en la bitácora)."""
    if not usuario.es_admin:
        raise AccesoDenegado("Solo un Admin da permisos.")
    if permiso not in PERMISOS:
        raise SolicitudInvalida("Permiso desconocido.")
    nombre = _validar_colaborador(op.con, colaborador_id)
    unidad = nombre_unidad(op.con, unidad_negocio_id)
    if otorgar:
        fila = op.con.execute(
            """
            INSERT INTO preboletas.permisos_colaborador (colaborador_id, permiso, unidad_negocio_id, otorgado_por_id)
            VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING 1
            """,
            (colaborador_id, permiso, unidad_negocio_id, usuario.colaborador_id),
        ).fetchone()
    else:
        fila = op.con.execute(
            "DELETE FROM preboletas.permisos_colaborador WHERE colaborador_id = %s AND permiso = %s "
            "AND unidad_negocio_id = %s RETURNING 1",
            (colaborador_id, permiso, unidad_negocio_id),
        ).fetchone()
    if fila is not None:  # Solo si de verdad cambió algo.
        op.mov(
            "permiso_colaborador",
            unidad_negocio_id,
            f"{'Da' if otorgar else 'Quita'} {permiso} a {nombre} en {unidad}",
        )
        op.evento("colaborador.actualizado", destinatario_id=colaborador_id, unidad_negocio_id=unidad_negocio_id)


def cambiar_canales(op: Operacion, usuario: Usuario, colaborador_id: int, correo: bool, push: bool) -> None:
    """Canales extra de aviso. `app` (la campana) lo tienen todos."""
    if not usuario.es_admin:
        raise AccesoDenegado("Solo un Admin define canales.")
    _validar_colaborador(op.con, colaborador_id)
    for canal, activo in (("correo", correo), ("push", push)):
        if activo:
            op.con.execute(
                "INSERT INTO preboletas.canales_colaborador (colaborador_id, canal, definido_por_id) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (colaborador_id, canal, usuario.colaborador_id),
            )
        else:
            op.con.execute(
                "DELETE FROM preboletas.canales_colaborador WHERE colaborador_id = %s AND canal = %s",
                (colaborador_id, canal),
            )
