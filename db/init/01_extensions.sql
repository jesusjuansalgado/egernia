-- Spherical-geometry extension used by ADQL geometry translations
-- (POINT/CIRCLE/CONTAINS -> spoint/scircle/@ operators).
DO $do$
BEGIN
    BEGIN
        CREATE EXTENSION IF NOT EXISTS pg_sphere;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'pg_sphere not available: ADQL geometry functions will fail (%).', SQLERRM;
    END;
END
$do$;

-- skycell, the alternative geometry backend (TAP_GEOMETRY_BACKEND=skycell);
-- only present when the image was built with it (db/Dockerfile, SKYCELL_REF).
DO $do$
BEGIN
    BEGIN
        CREATE EXTENSION IF NOT EXISTS skycell;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'skycell not available: TAP_GEOMETRY_BACKEND=skycell will fail (%).', SQLERRM;
    END;
END
$do$;
