from __future__ import annotations

from datetime import timedelta

from apps.documents.models import Document, DocumentVersion
from apps.identity.models import Membership, Organization
from apps.retention_disposition.models import DocumentType, RetentionClass
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone


class IdentityModelTests(TestCase):
    def setUp(self) -> None:
        User = get_user_model()
        self.user = User.objects.create_user(
            username="auditor",
            email="auditor@example.com",
            keycloak_sub="kc-sub-auditor",
        )
        self.organization = Organization.objects.create(
            name="Org Auditoria",
            slug="org-auditoria",
        )

    def test_active_membership_authorizes_by_surrogate_user_and_org(
        self,
    ) -> None:
        Membership.objects.create(
            organization=self.organization,
            user=self.user,
            status=Membership.Status.ACTIVE,
        )

        self.assertTrue(self.user.has_organization_membership(self.organization.id))

    def test_membership_keeps_invitation_scope_and_expiration(self) -> None:
        expires_at = timezone.now() + timedelta(days=7)

        membership = Membership.objects.create(
            organization=self.organization,
            user=self.user,
            status=Membership.Status.INVITED,
            scope={"audit_process_ids": ["proc_123"], "access": "read_only"},
            expires_at=expires_at,
        )

        self.assertEqual(membership.scope["access"], "read_only")
        self.assertEqual(membership.expires_at, expires_at)
        self.assertEqual(Membership.objects.invited().count(), 1)

    def test_inactive_organization_does_not_authorize_tenant_access(self) -> None:
        Membership.objects.create(
            organization=self.organization,
            user=self.user,
            status=Membership.Status.ACTIVE,
        )
        self.organization.is_active = False
        self.organization.save(update_fields=["is_active"])

        self.assertFalse(self.user.has_organization_membership(self.organization.id))

    def test_expired_membership_does_not_authorize_tenant_access(self) -> None:
        Membership.objects.create(
            organization=self.organization,
            user=self.user,
            status=Membership.Status.ACTIVE,
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        self.assertFalse(self.user.has_organization_membership(self.organization.id))


class DocumentModelTests(TestCase):
    def setUp(self) -> None:
        User = get_user_model()
        self.user = User.objects.create_user(
            username="gestor",
            email="gestor@example.com",
            keycloak_sub="kc-sub-gestor",
        )
        self.organization = Organization.objects.create(
            name="Org Documental",
            slug="org-documental",
        )
        self.retention_class = RetentionClass.objects.create(
            organization=self.organization,
            nombre="Fiscal sensible",
            periodo_retencion_meses=120,
            worm_habilitado=True,
            es_sensible=True,
            aprobadores_disposicion_requeridos=2,
        )
        self.document_type = DocumentType.objects.create(
            organization=self.organization,
            retention_class=self.retention_class,
            nombre="Factura",
            descripcion="Comprobante fiscal",
        )

    def test_document_type_links_documents_to_retention_class(self) -> None:
        document = Document.objects.create(
            organization=self.organization,
            document_type=self.document_type,
            creado_por=self.user,
            titulo="Factura 2026-001",
        )

        self.assertEqual(document.estado_ciclo_vida, Document.EstadoCicloVida.BORRADOR)
        self.assertEqual(
            document.document_type.retention_class.aprobadores_disposicion_requeridos,
            2,
        )
        self.assertTrue(document.document_type.retention_class.es_sensible)

    def test_document_rejects_document_type_from_other_organization(self) -> None:
        other_organization = Organization.objects.create(
            name="Otra Org",
            slug="otra-org",
        )
        document = Document(
            organization=other_organization,
            document_type=self.document_type,
            creado_por=self.user,
            titulo="Documento cruzado",
        )

        with self.assertRaises(ValidationError):
            document.full_clean()

    def test_document_version_hashes_content_with_sha256(self) -> None:
        document = Document.objects.create(
            organization=self.organization,
            document_type=self.document_type,
            creado_por=self.user,
            titulo="Factura 2026-002",
        )
        version = DocumentVersion(
            document=document,
            creado_por=self.user,
            numero_version=1,
            uri_almacenamiento="minio://sgdypa/org/document/v1.pdf",
        )

        version.set_content_hash(b"contenido fiscal")
        version.save()

        self.assertEqual(
            version.hash_contenido,
            "ca9f79c13a86678ee93b5fa900ee62afdb4a5264c63d20a7e7c38a54db7f7d50",
        )
        self.assertEqual(
            version.hash_contenido,
            DocumentVersion.calculate_content_hash("contenido fiscal"),
        )
