-- Tenant isolation and append-only ledger guardrails for SGDyPA.
--
-- ADR-0007 requires tenant-scoped PostgreSQL Row-Level Security (RLS)
-- backed by a transaction-scoped GUC named app.current_org. Policies must
-- fail closed when the GUC is unset, so missing request tenancy returns zero
-- rows instead of all rows.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sgdypa_app') THEN
        CREATE ROLE sgdypa_app NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    ELSE
        ALTER ROLE sgdypa_app NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA sgdypa TO sgdypa_app;

CREATE OR REPLACE FUNCTION sgdypa.current_organization_id()
RETURNS uuid
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT NULLIF(current_setting('app.current_org', true), '')::uuid
$$;

COMMENT ON FUNCTION sgdypa.current_organization_id() IS
    'Returns the transaction-local tenant GUC app.current_org. NULL means fail-closed RLS.';

CREATE OR REPLACE FUNCTION sgdypa.set_current_organization(org_id uuid)
RETURNS text
LANGUAGE sql
AS $$
    SELECT set_config('app.current_org', org_id::text, true)
$$;

COMMENT ON FUNCTION sgdypa.set_current_organization(uuid) IS
    'Sets app.current_org with SET LOCAL semantics; call as the first statement inside each request transaction.';

CREATE OR REPLACE FUNCTION sgdypa.enable_organization_rls(target_table regclass)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    target_schema name;
    target_name name;
    has_org_column boolean;
BEGIN
    SELECT n.nspname, c.relname
      INTO target_schema, target_name
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.oid = target_table
       AND c.relkind IN ('r', 'p');

    IF target_schema IS NULL THEN
        RAISE EXCEPTION 'Table % does not exist or is not a regular/partitioned table', target_table;
    END IF;

    SELECT EXISTS (
        SELECT 1
          FROM pg_attribute a
         WHERE a.attrelid = target_table
           AND a.attname = 'organization_id'
           AND NOT a.attisdropped
    ) INTO has_org_column;

    IF NOT has_org_column THEN
        RAISE EXCEPTION 'Table %.% must have organization_id before RLS can be enabled', target_schema, target_name;
    END IF;

    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', target_table);
    EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', target_table);

    EXECUTE format(
        'DROP POLICY IF EXISTS %I ON %s',
        target_name || '_organization_isolation',
        target_table
    );

    EXECUTE format(
        'CREATE POLICY %I ON %s AS PERMISSIVE FOR ALL TO sgdypa_app USING (organization_id = sgdypa.current_organization_id()) WITH CHECK (organization_id = sgdypa.current_organization_id())',
        target_name || '_organization_isolation',
        target_table
    );

    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %s TO sgdypa_app', target_table);
END;
$$;

COMMENT ON FUNCTION sgdypa.enable_organization_rls(regclass) IS
    'Enables FORCE RLS and fail-closed organization_id policy for a domain table.';

CREATE OR REPLACE FUNCTION sgdypa.apply_existing_organization_rls()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    domain_table regclass;
BEGIN
    FOR domain_table IN
        SELECT c.oid::regclass
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_attribute a ON a.attrelid = c.oid
         WHERE n.nspname = 'sgdypa'
           AND c.relkind IN ('r', 'p')
           AND a.attname = 'organization_id'
           AND NOT a.attisdropped
    LOOP
        PERFORM sgdypa.enable_organization_rls(domain_table);
    END LOOP;
END;
$$;

COMMENT ON FUNCTION sgdypa.apply_existing_organization_rls() IS
    'Applies SGDyPA tenant RLS to existing sgdypa tables with organization_id.';

CREATE OR REPLACE FUNCTION sgdypa.grant_trail_entry_append_only()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF to_regclass('sgdypa.trail_entry') IS NULL THEN
        RETURN;
    END IF;

    REVOKE UPDATE, DELETE ON TABLE sgdypa.trail_entry FROM sgdypa_app;
    GRANT SELECT, INSERT ON TABLE sgdypa.trail_entry TO sgdypa_app;
END;
$$;

COMMENT ON FUNCTION sgdypa.grant_trail_entry_append_only() IS
    'Ensures the application role can append/read trail entries but cannot update or delete them.';
