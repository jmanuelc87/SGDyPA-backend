from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError

from apps.audit_process.models import AuditProcess

Estado = AuditProcess.Estado


@dataclass(frozen=True)
class TransitionRule:
    origen: str
    destino: str
    evento: str
    es_backlink: bool = False
    requiere_motivo: bool = False


TRANSITION_RULES: tuple[TransitionRule, ...] = (
    TransitionRule(Estado.PLANIFICADA, Estado.EN_EJECUCION, "reunion_apertura"),
    TransitionRule(Estado.EN_EJECUCION, Estado.EN_CIERRE, "evidencia_contrastada"),
    TransitionRule(Estado.EN_CIERRE, Estado.INFORME_EMITIDO, "hallazgos_validados"),
    TransitionRule(Estado.INFORME_EMITIDO, Estado.EN_SEGUIMIENTO, "no_conformidades"),
    TransitionRule(Estado.EN_SEGUIMIENTO, Estado.CERRADA, "eficacia_verificada"),
    TransitionRule(
        Estado.EN_CIERRE,
        Estado.PLANIFICADA,
        "cambio_alcance_rebaseline",
        es_backlink=True,
        requiere_motivo=True,
    ),
    TransitionRule(
        Estado.EN_CIERRE,
        Estado.EN_EJECUCION,
        "falta_evidencia",
        es_backlink=True,
        requiere_motivo=True,
    ),
    TransitionRule(Estado.EN_SEGUIMIENTO, Estado.EN_SEGUIMIENTO, "accion_ineficaz"),
    TransitionRule(
        Estado.PLANIFICADA, Estado.CANCELADA, "cancelar", requiere_motivo=True
    ),
    TransitionRule(
        Estado.EN_EJECUCION, Estado.CANCELADA, "cancelar", requiere_motivo=True
    ),
    TransitionRule(
        Estado.EN_CIERRE, Estado.CANCELADA, "cancelar", requiere_motivo=True
    ),
    TransitionRule(
        Estado.PLANIFICADA, Estado.POSTERGADA, "postergar", requiere_motivo=True
    ),
    TransitionRule(
        Estado.EN_EJECUCION, Estado.POSTERGADA, "postergar", requiere_motivo=True
    ),
    TransitionRule(Estado.POSTERGADA, Estado.EN_EJECUCION, "reanudar"),
)

_RULES_BY_EDGE = {(rule.origen, rule.destino): rule for rule in TRANSITION_RULES}
TERMINAL_STATES = frozenset({Estado.CERRADA, Estado.CANCELADA})
CORE_STATES = (
    Estado.PLANIFICADA,
    Estado.EN_EJECUCION,
    Estado.EN_CIERRE,
    Estado.INFORME_EMITIDO,
    Estado.EN_SEGUIMIENTO,
    Estado.CERRADA,
)
AUDIT_TYPES = tuple(choice.value for choice in AuditProcess.Tipo)


def get_transition_rule(origen: str, destino: str) -> TransitionRule | None:
    return _RULES_BY_EDGE.get((origen, destino))


def allowed_transitions(estado: str) -> tuple[TransitionRule, ...]:
    return tuple(rule for rule in TRANSITION_RULES if rule.origen == estado)


def transition(
    audit_process: AuditProcess,
    destino: str,
    *,
    motivo: str = "",
    save: bool = True,
) -> TransitionRule:
    rule = get_transition_rule(audit_process.estado, destino)
    if rule is None:
        raise ValidationError(
            {
                "estado": (
                    f"Transición no permitida: {audit_process.estado} → {destino}. "
                    "La API es la fuente de verdad de la FSM."
                )
            }
        )
    if rule.requiere_motivo and not motivo:
        raise ValidationError({"motivo": "Esta transición requiere motivo."})

    audit_process.estado_pre_postergacion = (
        audit_process.estado if destino == Estado.POSTERGADA else ""
    )
    audit_process.motivo_excepcion = (
        motivo if destino in {Estado.CANCELADA, Estado.POSTERGADA} else ""
    )
    audit_process.estado = destino
    if save:
        audit_process.save(
            update_fields=[
                "estado",
                "estado_pre_postergacion",
                "motivo_excepcion",
                "updated_at",
            ]
        )
    return rule
