"""ADQL geometry translated for skycell instead of pg_sphere.

skycell (https://github.com/jesusjuansalgado/skycell) is a PostgreSQL
extension whose geometry functions carry ADQL's own names and units, so the
translation is close to the identity:

- POINT, CIRCLE and POLYGON become ``skycell_point``, ``skycell_circle`` and
  ``skycell_polygon`` -- the prefixed aliases, because PostgreSQL's own
  ``point``/``circle``/``polygon`` would otherwise win the name lookup --
  with coordinates in degrees, as written;
- BOX becomes the polygon with corners at the centre plus and minus half the
  width in RA and half the height in Dec: the coordinate range the pg_sphere
  translation gives it (an ``sbox``), so a query means the same under either
  backend. The polygon's Dec edges are great circles where the sbox's are
  lines of constant Dec, so the two differ in slivers along them (a couple of
  arcseconds for a 1-degree box at Dec -70). skycell's own ``box()`` reads the
  width as an angle on the sky instead, which opens out in RA by 1/cos(dec);
- CONTAINS and INTERSECTS stay the ADQL functions, compared with 1 or 0 as
  the query compares them. skycell plans ``contains(...) = 1`` as its
  indexed operator, whichever argument is the column and whichever way
  round the query is written, so no operator choice is made here. One
  exception: skycell defines ``intersects(skypos, skyregion)`` but not the
  reverse, so an INTERSECTS whose second argument is a POINT constructor is
  written point first (ADQL's INTERSECTS is symmetric). As in the pg_sphere
  translation, a constructor is the only argument whose type is known here;
  a point-typed column (ADQL 2.1) keeps the order it was written in;
- DISTANCE becomes ``distance()``, in degrees, in both of its forms. Compared
  with a radius in a WHERE or JOIN ON it is planned as the same indexed cone;
- AREA becomes ``area()``, already in square degrees;
- COORD1, COORD2 and CENTROID pass through untranslated, as they do for
  pg_sphere -- but under skycell they name its own functions and work. A
  column they take is recorded as a geometry column, like one in CONTAINS or
  INTERSECTS, so a text column there is refused as a usage error before
  the query reaches the database.

The values are skycell's ``skypos``/``skyregion`` types, so a geometry column
(ADQL 2.1) is one of those, and a stored footprint is a ``skyregion`` (see
metadata/regions.py, ``stcs_to_skyregion``).

skycell's polygons must be convex; a non-convex ADQL POLYGON is an error in
the database rather than here.
"""

from queryparser.exceptions import QueryError

from .ADQLParser import ADQLParser
from .translator import (
    ADQLFunctionsTranslationVisitor,
    ADQLGeometryTranslationVisitor,
    _convert_values,
    _get_ancestor_class_node,
    _remove_children,
)


def _alias_in_select(ctx, text, alias, depth):
    """``text AS alias`` when ctx is a bare item of the select list."""
    derived_column = _get_ancestor_class_node(ctx, ADQLParser.Derived_columnContext, depth=depth)
    if derived_column is not None and not any(
        isinstance(child, ADQLParser.As_clauseContext) for child in derived_column.children
    ):
        return f"{text} AS {alias}"
    return text


# ADQL functions that reach skycell unchanged and take a geometry
_PASSTHROUGH_GEOMETRY_FUNCTIONS = frozenset({"COORD1", "COORD2", "CENTROID"})


def _is_point(sql):
    """Whether this translated geometry argument is a POINT constructor."""
    return sql.lstrip().startswith("skycell_point(")


def _bare_column(ctx):
    """The column reference ctx consists of, or None if it is anything else."""
    while ctx.getChildCount() == 1 and not isinstance(ctx, ADQLParser.Column_referenceContext):
        ctx = ctx.getChild(0)
    return ctx if isinstance(ctx, ADQLParser.Column_referenceContext) else None


def _point_coords(ctx):
    """The (ra, dec) of a POINT constructor, skipping a coordinate system."""
    coords = []
    if len(ctx.children) > 4:
        for j in (2, 4):
            coords.extend(_convert_values(ctx, j))
    else:
        coords.extend(_convert_values(ctx, 2))
    if len(coords) == 3:
        coords = coords[1:]
    return coords[0], coords[1]


class SkycellGeometryTranslationVisitor(ADQLGeometryTranslationVisitor):
    """The geometry constructors, as skycell functions in degrees."""

    def __init__(self):
        super().__init__(conunits="")

    def visitPoint(self, ctx):
        ra, dec = _point_coords(ctx)
        ctx_text = _alias_in_select(ctx, f"skycell_point({ra}, {dec})", "adql_point", 3)
        _remove_children(ctx)
        self.contexts[ctx] = ctx_text

    def visitBox(self, ctx):
        s = 4 if len(ctx.children) > 8 else 2
        pars = []
        for j in (s, s + 2, s + 4):
            pars.extend(self._convert_values(ctx, j))
        if len(pars) != 4:
            raise QueryError("BOX needs a centre, a width and a height")
        try:
            ra, dec, half_w, half_h = (
                float(pars[0]),
                float(pars[1]),
                float(pars[2]) / 2,
                float(pars[3]) / 2,
            )
        except ValueError:
            # the pg_sphere translation takes numbers only, too
            raise QueryError("sbox values incorrect") from None
        corners = (
            (ra - half_w, dec - half_h),
            (ra + half_w, dec - half_h),
            (ra + half_w, dec + half_h),
            (ra - half_w, dec + half_h),
        )
        vertices = ", ".join(f"{a:.12f}, {d:.12f}" for a, d in corners)
        ctx_text = f"skycell_polygon({vertices})"
        _remove_children(ctx)
        self.contexts[ctx] = ctx_text

    def visitCircle(self, ctx):
        s = 4 if isinstance(ctx.children[2], ADQLParser.Coord_sysContext) else 2
        radius = self._convert_values(ctx, s + 2)[0]
        circle_center = ctx.children[s]
        if isinstance(circle_center.children[0], ADQLParser.CoordinatesContext):
            ra, dec = self._convert_values(circle_center, 0)[:2]
        elif isinstance(circle_center.children[0].children[0], ADQLParser.Column_referenceContext):
            # ADQL 2.1: a POINT-typed (skypos) column as the circle centre.
            column = circle_center.children[0].getText()
            self.geometry_columns.add(column)
            ra, dec = f"coord1({column})", f"coord2({column})"
        else:
            point_ctx = circle_center.children[0].children[0].children[0]
            if not isinstance(point_ctx, ADQLParser.PointContext):
                raise QueryError(
                    "In the current implementation, circle allows only explicitly"
                    " defined point or a point column as the circle center. For"
                    " instance, CIRCLE(POINT(t.ra, t.dec), 0.1)"
                )
            ra, dec = _point_coords(point_ctx)
        ctx_text = _alias_in_select(ctx, f"skycell_circle({ra}, {dec}, {radius})", "circle", 3)
        _remove_children(ctx)
        self.contexts[ctx] = ctx_text

    def visitPolygon(self, ctx):
        coords = []
        for j in range(2, len(ctx.children), 2):
            par = self._convert_values(ctx, j)
            # only coordinate pairs (ctx.children[2] can be the deprecated
            # coordinate system)
            if len(par) > 1:
                coords.extend(par[:2])
        ctx_text = f"skycell_polygon({', '.join(str(c) for c in coords)})"
        ctx_text = _alias_in_select(ctx, ctx_text, "adql_polygon", 3)
        _remove_children(ctx)
        self.contexts[ctx] = ctx_text


class SkycellFunctionsTranslationVisitor(ADQLFunctionsTranslationVisitor):
    """The geometry functions and predicates, as skycell's ADQL-named ones."""

    def __init__(self, contexts):
        super().__init__(contexts, conunits="")

    def visitArea(self, ctx):
        arg = self._geometry_argument(ctx.children[2])
        ctx_text = _alias_in_select(ctx, f"area({arg})", "adql_area", 9)
        _remove_children(ctx)
        self.contexts[ctx] = ctx_text

    def _predicate(self, ctx, name):
        """``name(a, b) = 1`` or ``= 0``, from either side of the comparison."""
        left = ctx.children[0].getText()
        right = ctx.children[2].getText()
        if left in ("0", "1"):
            function, value, reverse = ctx.children[2], left, False
        elif right in ("0", "1"):
            function, value, reverse = ctx.children[0], right, True
        else:
            raise QueryError(f"The function {name.upper()} allows comparison to 1 or 0 only.")
        self._function(function, name)
        ctx_text = f"{self.contexts[function]} = {value}"
        _remove_children(ctx, reverse=reverse)
        self.contexts[ctx] = ctx_text

    def _function(self, ctx, name):
        left = self._geometry_argument(ctx.children[2])
        right = self._geometry_argument(ctx.children[4])
        if name == "intersects" and _is_point(right) and not _is_point(left):
            left, right = right, left
        _remove_children(ctx)
        self.contexts[ctx] = f"{name}({left}, {right})"

    def visitContains_predicate(self, ctx):
        self._predicate(ctx, "contains")

    def visitContains(self, ctx):
        self._function(ctx, "contains")

    def visitIntersects_predicate(self, ctx):
        self._predicate(ctx, "intersects")

    def visitIntersects(self, ctx):
        self._function(ctx, "intersects")

    def visitUser_defined_function(self, ctx):
        # COORD1/COORD2/CENTROID parse as user-defined functions; their
        # column argument is a geometry column all the same
        if ctx.getChild(0).getText().upper() in _PASSTHROUGH_GEOMETRY_FUNCTIONS:
            for param in ctx.getTypedRuleContexts(ADQLParser.User_defined_function_paramContext):
                column = _bare_column(param)
                if column is not None:
                    self.geometry_columns.add(column.getText())
        return self.visitChildren(ctx)

    def visitDistance(self, ctx):
        if isinstance(ctx.children[2], ADQLParser.Coord_valueContext):
            args = (
                self._point_argument(ctx.children[2]),
                self._point_argument(ctx.children[4]),
            )
        else:
            args = tuple(_convert_values(ctx, i)[0] for i in (2, 4, 6, 8))
        ctx_text = f"distance({', '.join(str(a) for a in args)})"
        ctx_text = _alias_in_select(ctx, ctx_text, "distance", 9)
        for _ in range(ctx.getChildCount() - 1):
            ctx.removeLastChild()
        self.contexts[ctx] = ctx_text
