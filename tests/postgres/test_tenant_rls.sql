\set ON_ERROR_STOP on

BEGIN;
CREATE TABLE sgdypa.rls_probe (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    payload text NOT NULL
);

SELECT sgdypa.enable_organization_rls('sgdypa.rls_probe'::regclass);
INSERT INTO sgdypa.rls_probe (id, organization_id, payload) VALUES
    ('00000000-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111', 'tenant-a'),
    ('00000000-0000-0000-0000-000000000002', '22222222-2222-2222-2222-222222222222', 'tenant-b');
COMMIT;

SET ROLE sgdypa_app;

BEGIN;
SELECT count(*) = 0 AS fail_closed_ok FROM sgdypa.rls_probe \gset
\if :fail_closed_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'RLS fail-closed invariant failed: rows visible without app.current_org'; END $$;
\endif
COMMIT;

BEGIN;
SELECT sgdypa.set_current_organization('11111111-1111-1111-1111-111111111111');
SELECT count(*) = 1 AS tenant_visibility_ok FROM sgdypa.rls_probe \gset
\if :tenant_visibility_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'RLS tenant invariant failed: org A should see exactly one row'; END $$;
\endif
COMMIT;

BEGIN;
SELECT count(*) = 0 AS guc_transactional_ok FROM sgdypa.rls_probe \gset
\if :guc_transactional_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'RLS transaction GUC invariant failed: app.current_org leaked after commit'; END $$;
\endif
COMMIT;

RESET ROLE;

BEGIN;
CREATE TABLE sgdypa.trail_entry (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    payload text NOT NULL
);
SELECT sgdypa.enable_organization_rls('sgdypa.trail_entry'::regclass);
SELECT sgdypa.grant_trail_entry_append_only();
SELECT NOT has_table_privilege('sgdypa_app', 'sgdypa.trail_entry', 'UPDATE') AS trail_no_update_ok \gset
SELECT NOT has_table_privilege('sgdypa_app', 'sgdypa.trail_entry', 'DELETE') AS trail_no_delete_ok \gset
\if :trail_no_update_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'Trail append-only invariant failed: sgdypa_app has UPDATE on trail_entry'; END $$;
\endif
\if :trail_no_delete_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'Trail append-only invariant failed: sgdypa_app has DELETE on trail_entry'; END $$;
\endif
ROLLBACK;

DROP TABLE sgdypa.rls_probe;
