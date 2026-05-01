BEGIN;

DO $body$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT quote_ident(schemaname) AS schemaname,
               quote_ident(tablename) AS tablename,
               tablename AS raw_table
        FROM pg_tables
        WHERE schemaname = 'public'
    LOOP
        EXECUTE format('REVOKE ALL ON TABLE %s.%s FROM anon, authenticated', r.schemaname, r.tablename);
        EXECUTE format('ALTER TABLE %s.%s ENABLE ROW LEVEL SECURITY', r.schemaname, r.tablename);
        EXECUTE format('ALTER TABLE %s.%s FORCE ROW LEVEL SECURITY', r.schemaname, r.tablename);
        BEGIN
            EXECUTE format(
                'CREATE POLICY deny_anon_authenticated_%s ON %s.%s FOR ALL TO anon, authenticated USING (false) WITH CHECK (false)',
                replace(r.raw_table, '-', '_'),
                r.schemaname,
                r.tablename
            );
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END;
    END LOOP;
END $body$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated;

COMMIT;
