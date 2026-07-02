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
SELECT count(*) AS visible_without_org FROM sgdypa.rls_probe \gset
\if :visible_without_org != 0
    \echo 'RLS fail-closed invariant failed: rows visible without app.current_org'
    \quit 1
\endif
COMMIT;

BEGIN;
SELECT sgdypa.set_current_organization('11111111-1111-1111-1111-111111111111');
SELECT count(*) AS visible_for_org_a FROM sgdypa.rls_probe \gset
\if :visible_for_org_a != 1
    \echo 'RLS tenant invariant failed: org A should see exactly one row'
    \quit 1
\endif
COMMIT;

BEGIN;
SELECT count(*) AS visible_after_commit FROM sgdypa.rls_probe \gset
\if :visible_after_commit != 0
    \echo 'RLS transaction GUC invariant failed: app.current_org leaked after commit'
    \quit 1
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
SELECT has_table_privilege('sgdypa_app', 'sgdypa.trail_entry', 'UPDATE') AS app_can_update_trail \gset
SELECT has_table_privilege('sgdypa_app', 'sgdypa.trail_entry', 'DELETE') AS app_can_delete_trail \gset
\if :app_can_update_trail != false
    \echo 'Trail append-only invariant failed: sgdypa_app has UPDATE on trail_entry'
    \quit 1
\endif
\if :app_can_delete_trail != false
    \echo 'Trail append-only invariant failed: sgdypa_app has DELETE on trail_entry'
    \quit 1
\endif
ROLLBACK;

DROP TABLE sgdypa.rls_probe;
