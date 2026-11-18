"""
La persona que hace la petición, con sus roles (de Azure) y sus permisos por unidad.

Reglas de rol (CLAUDE.md §6 y el prototipo):
  * Admin: ve y hace todo; tiene todos los permisos sin filas en permisos_colaborador.
  * Comprador: crea preboletas y ve solo las suyas; resuelve incidencias de cantidad y concilia.
  * Basculista: opera la báscula de SU unidad y ve las preboletas de su unidad.
Si alguien tiene varios roles, sus capacidades se suman.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ROL_ADMIN = "Admin"
ROL_COMPRADOR = "Comprador"
ROL_BASCULISTA = "Basculista"
ROLES_APP = frozenset({ROL_ADMIN, ROL_COMPRADOR, ROL_BASCULISTA})

PERMISO_PRECIOS = "precios_diarios"
PERMISO_AUTORIZAR = "autorizar_precios"
PERMISOS = (PERMISO_PRECIOS, PERMISO_AUTORIZAR)


@dataclass(frozen=True)
class Usuario:
    colaborador_id: int
    nombre: str
    correo: str
    unidad_negocio_id: int
    roles: frozenset[str]
    # Pares (permiso, unidad) de permisos_colaborador.
    permisos: frozenset[tuple[str, int]] = field(default_factory=frozenset)

    @property
    def es_admin(self) -> bool:
        return ROL_ADMIN in self.roles

    @property
    def es_comprador(self) -> bool:
        return ROL_COMPRADOR in self.roles

    @property
    def es_basculista(self) -> bool:
        return ROL_BASCULISTA in self.roles

    def tiene_permiso(self, permiso: str, unidad_negocio_id: int) -> bool:
        """Admin tiene todos los permisos en todas las unidades sin necesidad de filas."""
        return self.es_admin or (permiso, unidad_negocio_id) in self.permisos

    def tiene_permiso_en_alguna(self, permiso: str) -> bool:
        return self.es_admin or any(p == permiso for p, _ in self.permisos)

    def puede_operar_bascula(self, unidad_negocio_id: int) -> bool:
        """Leer QR, indicar peso, cancelar entrada y corregir cajas."""
        return self.es_admin or (self.es_basculista and self.unidad_negocio_id == unidad_negocio_id)

    def es_dueno(self, comprador_id: int) -> bool:
        """Dueño de una preboleta: su comprador o un Admin (proponer precio, cerrar, múltiples cargas)."""
        return self.es_admin or comprador_id == self.colaborador_id

    def puede_ver_preboleta(self, comprador_id: int, unidad_negocio_id: int) -> bool:
        """Admin: todas. Comprador: las suyas. Basculista: las de su unidad."""
        return (
            self.es_admin
            or (self.es_comprador and comprador_id == self.colaborador_id)
            or (self.es_basculista and unidad_negocio_id == self.unidad_negocio_id)
        )

    def puede_conciliar(self, tipo: str, unidad_negocio_id: int) -> bool:
        """
        Conciliación de boletas: el cambio de proveedor solo lo resuelve el basculista de la
        unidad (o un Admin), porque implica volver a revisar el camión; lo demás, también
        cualquier comprador.
        """
        if self.es_admin:
            return True
        basculista_de_la_unidad = self.es_basculista and self.unidad_negocio_id == unidad_negocio_id
        if tipo == "cambio_proveedor":
            return basculista_de_la_unidad
        return basculista_de_la_unidad or self.es_comprador
