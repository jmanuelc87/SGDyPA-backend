from __future__ import annotations

from apps.audit_process.fsm import (
    AUDIT_TYPES,
    CORE_STATES,
    allowed_transitions,
    get_transition_rule,
    transition,
)
from apps.audit_process.models import AuditProcess
from apps.identity.models import Organization
from django.core.exceptions import ValidationError
from django.test import TestCase


class AuditProcessFSMTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(
            name="Aurora", slug="aurora-audit-fsm"
        )

    def create_process(self, *, tipo: str = AuditProcess.Tipo.INTERNA) -> AuditProcess:
        return AuditProcess.objects.create(
            organization=self.organization,
            nombre="Auditoría ISO 19011",
            tipo=tipo,
        )

    def test_fsm_exposes_the_six_persistent_core_states(self) -> None:
        self.assertEqual(
            CORE_STATES,
            (
                AuditProcess.Estado.PLANIFICADA,
                AuditProcess.Estado.EN_EJECUCION,
                AuditProcess.Estado.EN_CIERRE,
                AuditProcess.Estado.INFORME_EMITIDO,
                AuditProcess.Estado.EN_SEGUIMIENTO,
                AuditProcess.Estado.CERRADA,
            ),
        )

    def test_linear_happy_path_reaches_cerrada(self) -> None:
        process = self.create_process()

        for destino in (
            AuditProcess.Estado.EN_EJECUCION,
            AuditProcess.Estado.EN_CIERRE,
            AuditProcess.Estado.INFORME_EMITIDO,
            AuditProcess.Estado.EN_SEGUIMIENTO,
            AuditProcess.Estado.CERRADA,
        ):
            transition(process, destino)

        process.refresh_from_db()
        self.assertEqual(process.estado, AuditProcess.Estado.CERRADA)

    def test_en_cierre_backlinks_are_authorized_and_marked(self) -> None:
        to_planificada = get_transition_rule(
            AuditProcess.Estado.EN_CIERRE, AuditProcess.Estado.PLANIFICADA
        )
        to_ejecucion = get_transition_rule(
            AuditProcess.Estado.EN_CIERRE, AuditProcess.Estado.EN_EJECUCION
        )

        self.assertIsNotNone(to_planificada)
        self.assertIsNotNone(to_ejecucion)
        self.assertTrue(to_planificada.es_backlink)  # type: ignore[union-attr]
        self.assertTrue(to_ejecucion.es_backlink)  # type: ignore[union-attr]

        process = self.create_process()
        transition(process, AuditProcess.Estado.EN_EJECUCION)
        transition(process, AuditProcess.Estado.EN_CIERRE)
        transition(
            process,
            AuditProcess.Estado.PLANIFICADA,
            motivo="Cambio de alcance y re-baseline",
        )

        self.assertEqual(process.estado, AuditProcess.Estado.PLANIFICADA)

    def test_cancelada_is_terminal_and_postergada_can_resume(self) -> None:
        cancelled = self.create_process()
        transition(cancelled, AuditProcess.Estado.CANCELADA, motivo="Cliente canceló")
        with self.assertRaises(ValidationError):
            transition(cancelled, AuditProcess.Estado.EN_EJECUCION)

        postponed = self.create_process()
        transition(postponed, AuditProcess.Estado.EN_EJECUCION)
        transition(
            postponed, AuditProcess.Estado.POSTERGADA, motivo="Ventana operativa"
        )
        self.assertEqual(
            postponed.estado_pre_postergacion, AuditProcess.Estado.EN_EJECUCION
        )
        transition(postponed, AuditProcess.Estado.EN_EJECUCION)
        self.assertEqual(postponed.estado, AuditProcess.Estado.EN_EJECUCION)

    def test_tipo_parametrizes_without_changing_available_transitions(self) -> None:
        self.assertEqual(
            AUDIT_TYPES,
            (
                AuditProcess.Tipo.INTERNA,
                AuditProcess.Tipo.SEGUNDA_PARTE,
                AuditProcess.Tipo.TERCERA_PARTE,
            ),
        )
        transitions_by_type = {
            tipo: allowed_transitions(self.create_process(tipo=tipo).estado)
            for tipo in AUDIT_TYPES
        }

        self.assertEqual(
            transitions_by_type[AuditProcess.Tipo.INTERNA],
            transitions_by_type[AuditProcess.Tipo.SEGUNDA_PARTE],
        )
        self.assertEqual(
            transitions_by_type[AuditProcess.Tipo.INTERNA],
            transitions_by_type[AuditProcess.Tipo.TERCERA_PARTE],
        )

    def test_invalid_transition_is_rejected_by_authoritative_fsm(self) -> None:
        process = self.create_process()

        with self.assertRaises(ValidationError):
            transition(process, AuditProcess.Estado.INFORME_EMITIDO)
