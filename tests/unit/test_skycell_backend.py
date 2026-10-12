"""The skycell geometry backend (TAP_GEOMETRY_BACKEND=skycell).

ADQL geometry translated for skycell rather than pg_sphere, the footprint
companion column and the position index built for it, and the setting that
chooses. The SQL is pinned exactly; that skycell answers it the way pg_sphere
answers its own translation is a property of the database, checked against a
real server.
"""

import logging
import pathlib
import sys

import pytest
import yaml
from egernia_core.config import GEOMETRY_BACKENDS, _choose, settings
from egernia_core.metadata import ingest
from egernia_core.metadata.regions import (
    POSITION_RADIUS_DEG,
    UnsupportedRegion,
    stcs_to_skyregion,
)
from egernia_core.metadata.schema_gen import (
    build_tables,
    ddl_statements,
    position_index_statements,
)
from egernia_core.query._adql import ADQLQueryTranslator
from egernia_core.query.adql import translate


@pytest.fixture
def skycell():
    """Run one test with TAP_GEOMETRY_BACKEND=skycell (Settings is frozen)."""
    original = settings.geometry_backend
    object.__setattr__(settings, "geometry_backend", "skycell")
    yield
    object.__setattr__(settings, "geometry_backend", original)


def sql(adql: str) -> str:
    return ADQLQueryTranslator(adql).to_postgresql(backend="skycell")


# -- the setting ---------------------------------------------------------------


def test_backends_and_default():
    assert GEOMETRY_BACKENDS == ("pgsphere", "skycell")
    assert settings.geometry_backend == "pgsphere"


def test_backend_setting_refuses_anything_else(monkeypatch):
    monkeypatch.setenv("TAP_GEOMETRY_BACKEND", "SkyCell ")
    assert _choose("TAP_GEOMETRY_BACKEND", "pgsphere", GEOMETRY_BACKENDS) == "skycell"
    monkeypatch.setenv("TAP_GEOMETRY_BACKEND", "")
    assert _choose("TAP_GEOMETRY_BACKEND", "pgsphere", GEOMETRY_BACKENDS) == "pgsphere"
    monkeypatch.setenv("TAP_GEOMETRY_BACKEND", "q3c")
    with pytest.raises(ValueError, match="TAP_GEOMETRY_BACKEND"):
        _choose("TAP_GEOMETRY_BACKEND", "pgsphere", GEOMETRY_BACKENDS)


def test_unknown_backend_is_a_query_error():
    from queryparser.exceptions import QueryError

    with pytest.raises(QueryError, match="unknown geometry backend"):
        ADQLQueryTranslator("SELECT a FROM t").to_postgresql(backend="q3c")


# -- translation ---------------------------------------------------------------


def test_cone_is_contains_compared_with_one():
    assert sql(
        "SELECT * FROM t WHERE CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 10, 20, 0.5)) = 1"
    ) == (
        "SELECT * FROM t WHERE"
        " contains(skycell_point(ra, dec), skycell_circle(10.0, 20.0, 0.5)) = 1;"
    )


def test_reversed_cone_and_left_hand_comparison_are_kept_as_written():
    # a constant point inside a circle around each row, as the Gaia archive
    # writes cone searches: skycell plans it as the cone, so it stays as is
    assert sql(
        "SELECT * FROM t WHERE 1 = CONTAINS(POINT('ICRS', 1, 2), CIRCLE('ICRS', ra, dec, 0.1))"
    ) == (
        "SELECT * FROM t WHERE contains(skycell_point(1.0, 2.0), skycell_circle(ra, dec, 0.1)) = 1;"
    )


def test_not_contained_and_intersects():
    assert sql(
        "SELECT * FROM t WHERE CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 1, 2, 3)) = 0"
    ).endswith("contains(skycell_point(ra, dec), skycell_circle(1.0, 2.0, 3.0)) = 0;")
    assert sql(
        "SELECT * FROM t WHERE INTERSECTS(s_region_geom, CIRCLE('ICRS', 1, 2, 3)) = 1"
    ).endswith("intersects(s_region_geom, skycell_circle(1.0, 2.0, 3.0)) = 1;")


def test_cross_match():
    assert sql(
        "SELECT h.id FROM h JOIN g"
        " ON CONTAINS(POINT('ICRS', g.ra, g.dec), CIRCLE('ICRS', h.ra, h.dec, 0.001)) = 1"
    ) == (
        "SELECT h.id FROM h JOIN g ON"
        " contains(skycell_point(g.ra, g.dec), skycell_circle(h.ra, h.dec, 0.001)) = 1;"
    )


def test_polygon():
    assert sql(
        "SELECT * FROM t WHERE CONTAINS(POINT('ICRS', ra, dec),"
        " POLYGON('ICRS', 10, 20, 12, 20, 12, 22)) = 1"
    ).endswith("skycell_polygon(10.0, 20.0, 12.0, 20.0, 12.0, 22.0)) = 1;")


def test_box_is_the_coordinate_range_pg_sphere_gives_it():
    # the same RA/Dec corners as the pg_sphere translation's sbox, so a BOX
    # means the same under either backend
    assert sql(
        "SELECT * FROM t WHERE CONTAINS(POINT('ICRS', ra, dec), BOX('ICRS', 11, 21, 2, 4)) = 1"
    ).endswith(
        "skycell_polygon(10.000000000000, 19.000000000000, 12.000000000000, 19.000000000000,"
        " 12.000000000000, 23.000000000000, 10.000000000000, 23.000000000000)) = 1;"
    )


def test_distance_both_forms_in_degrees():
    assert sql(
        "SELECT * FROM t WHERE DISTANCE(POINT('ICRS', ra, dec), POINT('ICRS', 1, 2)) < 0.1"
    ) == ("SELECT * FROM t WHERE distance(skycell_point(ra, dec), skycell_point(1.0, 2.0)) < 0.1;")
    assert sql("SELECT DISTANCE(ra, dec, 1, 2) FROM t") == (
        "SELECT distance(ra, dec, 1.0, 2.0) AS distance FROM t;"
    )


def test_select_list_geometry_and_area():
    assert sql(
        "SELECT AREA(s_region_geom), POINT('ICRS', ra, dec), CIRCLE('ICRS', ra, dec, 1) FROM t"
    ) == (
        "SELECT area(s_region_geom) AS adql_area, skycell_point(ra, dec) AS adql_point,"
        " skycell_circle(ra, dec, 1.0) AS circle FROM t;"
    )


def test_geometry_columns_are_recorded():
    translator = ADQLQueryTranslator("SELECT * FROM t WHERE CONTAINS(pos, CIRCLE(centre, 0.1)) = 1")
    assert translator.to_postgresql(backend="skycell") == (
        "SELECT * FROM t WHERE"
        " contains(pos, skycell_circle(coord1(centre), coord2(centre), 0.1)) = 1;"
    )
    assert translator.geometry_columns == {"pos", "centre"}


def test_the_translation_cache_is_keyed_by_backend(skycell):
    query = "SELECT * FROM t WHERE CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 1, 2, 3)) = 1"
    assert "skycell_point" in translate(query).sql
    object.__setattr__(settings, "geometry_backend", "pgsphere")
    assert "spoint" in translate(query).sql


# -- footprints ----------------------------------------------------------------


def test_skyregion_circle_and_position():
    assert stcs_to_skyregion(None) is None
    assert stcs_to_skyregion("CIRCLE ICRS 10 20 0.5") == (
        "CIRCLE ICRS 10.000000000000 20.000000000000 0.500000000000"
    )
    # the frame is optional in the source and always written for skycell
    assert stcs_to_skyregion("CIRCLE 10 20 0.5") == stcs_to_skyregion("CIRCLE ICRS 10 20 0.5")
    assert stcs_to_skyregion("POSITION ICRS 10 20") == (
        f"CIRCLE ICRS 10.000000000000 20.000000000000 {POSITION_RADIUS_DEG:.12f}"
    )


def test_skyregion_polygon_either_winding_and_closing_vertex():
    square = "POLYGON ICRS 10.000000000000 20.000000000000 11.000000000000 20.000000000000"
    square += " 11.000000000000 21.000000000000 10.000000000000 21.000000000000"
    assert stcs_to_skyregion("POLYGON 10 20 11 20 11 21 10 21") == square
    assert stcs_to_skyregion("POLYGON 10 20 11 20 11 21 10 21 10 20") == square
    assert stcs_to_skyregion("POLYGON 10 21 11 21 11 20 10 20").startswith("POLYGON ICRS")


@pytest.mark.parametrize(
    "region",
    [
        "POLYGON ICRS 0 0 2 0 1 0.2 2 2 0 2",  # a notch
        "POLYGON ICRS 0 0 2 2 2 0 0 2",  # self-crossing
        "POLYGON ICRS 0 0 120 0 240 0",  # a great circle: a hemisphere
    ],
)
def test_skyregion_refuses_what_skycell_cannot_store(region):
    with pytest.raises(UnsupportedRegion, match="convex"):
        stcs_to_skyregion(region)


def test_skyregion_still_validates_like_the_spoly_conversion():
    with pytest.raises(ValueError, match="Dec"):
        stcs_to_skyregion("CIRCLE ICRS 10 95 1")


def test_an_unsupported_footprint_is_ingested_as_null(caplog):
    column = next(
        c for c in _skycell_tables()["data_products"].columns if c.name == "s_region_geom"
    )
    with caplog.at_level(logging.WARNING, logger="egernia_core"):
        assert ingest._derive(column, "POLYGON ICRS 0 0 2 0 1 0.2 2 2 0 2") is None
    assert "s_region_geom left NULL" in caplog.text
    # anything malformed is still the caller's mistake
    from egernia_core.errors import UsageError

    with pytest.raises(UsageError):
        ingest._derive(column, "CIRCLE ICRS 10 95 1")


# -- schema --------------------------------------------------------------------


def _skycell_tables():
    from ska_src_mm_notification.models.schemas.srcnet_ingestion import (
        SRCIngestionNotification,
    )

    original = settings.geometry_backend
    object.__setattr__(settings, "geometry_backend", "skycell")
    try:
        tables = build_tables(SRCIngestionNotification, "srcnet", "projects")
    finally:
        object.__setattr__(settings, "geometry_backend", original)
    return {t.name: t for t in tables}


def test_footprint_companion_is_a_skyregion_with_a_gist_index():
    column = next(
        c for c in _skycell_tables()["data_products"].columns if c.name == "s_region_geom"
    )
    assert column.sql_type == "skyregion"
    assert column.index == "gist"
    assert column.description.startswith("skycell footprint")


def test_position_index_is_the_skycell_cell_btree(skycell):
    assert position_index_statements("data_products", "srcnet.data_products") == [
        "CREATE INDEX IF NOT EXISTS data_products_skycell_cell"
        " ON srcnet.data_products (skycell_ang2cell(s_ra, s_dec))",
        "ALTER INDEX srcnet.data_products_skycell_cell ALTER COLUMN 1 SET STATISTICS 1000",
    ]
    ddl = ddl_statements(list(_skycell_tables().values()), "tap_reader")
    assert not any("spoint" in s for s in ddl)
    assert any("srcnet.data_products (skycell_ang2cell(s_ra, s_dec))" in s for s in ddl)


def test_obscore_position_index_by_backend():
    from egernia_api.plugins import obscore

    assert obscore.POSITION_INDEXES["skycell"] == (
        "obscore_skycell_cell",
        "INDEX {name} ON {table} (skycell_ang2cell(s_ra, s_dec))",
    )
    # the default deployment keeps its pg_sphere index under its old name
    assert obscore.POSITION_INDEX == "obscore_spoint_gist"
    assert "obscore_spoint_gist" in obscore.INDEXES


def test_dataset_generator_writes_the_footprint_type_the_column_has():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "dataset"))
    from egernia_dataset import generate

    class Conn:
        def __init__(self, udt):
            self.udt = udt

        def execute(self, *_):
            return self

        def fetchone(self):
            return (self.udt,)

    data_products = dict(generate.LEVELS)["data_product"]
    assert generate._SPOLY_FOOTPRINT in data_products
    assert generate._levels(Conn("spoly")) == generate.LEVELS
    skycell_levels = dict(generate._levels(Conn("skyregion")))
    assert generate._SKYREGION_FOOTPRINT in skycell_levels["data_product"]
    assert generate._SPOLY_FOOTPRINT not in skycell_levels["data_product"]


def test_the_seeder_writes_the_footprint_type_the_column_has():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "dataset"))
    from egernia_dataset import seed

    class Conn:
        """Answers the column-type and resume-point lookups; records the rest."""

        def __init__(self):
            self.statements = []

        def execute(self, sql, *_):
            self.row = ("skyregion",) if "udt_name" in sql else (0,)
            return self

        def fetchone(self):
            return self.row

        def cursor(self):
            conn = self

            class Cursor:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

                def execute(self, sql, *_):
                    conn.statements.append(sql)

            return Cursor()

        def commit(self):
            pass

    conn = Conn()
    cfg = yaml.safe_load((seed.ROOT / "config" / "datasets.yaml").read_text())
    seed.grow_products_to(conn, cfg, seed.PRODUCTS_PER_PROJECT, 1)
    written = "\n".join(conn.statements)
    assert "skycell_circle(sky.ra, sky.dec" in written
    assert "circle_spoly" not in written


# -- capabilities --------------------------------------------------------------


def test_capabilities_name_the_backend_and_its_extra_functions(client, skycell):
    body = client.get("/tap/capabilities").text
    assert "ADQL translated to PostgreSQL/skycell" in body
    for form in ("CENTROID", "COORD1", "COORD2"):
        assert f"<feature><form>{form}</form></feature>" in body


def test_capabilities_under_pg_sphere_are_unchanged(client):
    body = client.get("/tap/capabilities").text
    assert "ADQL translated to PostgreSQL/pg_sphere" in body
    assert "<form>COORD1</form>" not in body
