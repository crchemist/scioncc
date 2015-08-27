#!/usr/bin/env python

"""Datastore query mapping for Postgres"""

__author__ = 'Michael Meisinger'

from pyon.core.exception import BadRequest
from pyon.datastore.datastore import DataStore
from pyon.datastore.datastore_query import DQ, DatastoreQueryBuilder


class PostgresQueryBuilder(object):

    # Maps operator constants to postgres operators
    OP_STR = {DQ.OP_EQ: "=",
              DQ.OP_NEQ: "<>",
              DQ.OP_LT: "<",
              DQ.OP_LTE: "<=",
              DQ.OP_GT: ">",
              DQ.OP_GTE: ">=",
              DQ.OP_LIKE: " LIKE ",
              DQ.OP_ILIKE: " ILIKE ",
              DQ.OP_FUZZY: " %% ",
              DQ.OP_REGEX: " ~ ",
              DQ.OP_IREGEX: " ~* ",
              DQ.GOP_OVERLAPS_BBOX: "&&",
              DQ.GOP_CONTAINS_BBOX: "~",
              DQ.GOP_WITHIN_BBOX: "@",
              DQ.GOP_OVERLAPS_GEOM: "ST_Intersects(%s,%s)",
              DQ.GOP_CROSSES_GEOM: "ST_Crosses(%s,%s)",
              DQ.GOP_CONTAINS_GEOM: "ST_Contains(%s,%s)",
              DQ.GOP_TOUCHES_GEOM: "ST_Touches(%s,%s)",
              DQ.GOP_WITHIN_GEOM: "ST_Within(%s,%s)",
              DQ.ROP_OVERLAPS_RANGE: "&&",
              DQ.ROP_CONTAINS_RANGE: "@>",
              DQ.ROP_WITHIN_RANGE: "<@",
              DQ.XOP_ATTLIKE: "LIKE",
              DQ.XOP_ATTILIKE: "ILIKE",
              }

    def __init__(self, query, basetable):
        DatastoreQueryBuilder.check_query(query)
        self.query = query
        self.basetable = basetable
        self.from_tables = basetable
        self._valcnt = 0
        self.values = {}
        self.query_params = query.get("query_params", {})
        self.ds_sub = self.query["query_args"].get("ds_sub", "")
        self.query_format = self.query["query_args"].get("format", "")
        self.table_aliases = [self.basetable]
        self.has_basic_cols = True

        if self.query_format == "sql":
            self.basic_cols = False
            self.cols = self.query["returns"]
            self.from_tables = self.query["from"]
            self.table_aliases = []
            self.where = self.query.get("where", None)
            self.group_by = self.query.get("group_by", None)
            self.having = self.query.get("having", None)
            self.order_by = self.query.get("order_by", None)

        elif self.query_format == "complex":
            # Build list of return values
            self.cols = ["base.id"]
            if not self.query["query_args"].get("id_only", True):
                self.cols.append("base.doc")
            if self.query.get("returns", None):
                if self.query["returns"][0] is True:
                    self.cols.extend(self.query["returns"][1:])
                else:
                    self.has_basic_cols = False
                    self.cols = self.query["returns"][1:]

            # Build FROM fragment with aliases using base table and provided list of other tables
            # Convention for table aliases:
            #   base table (resources, associations, events) as base
            #   subsequent from tables t0, t1 etc
            self.table_aliases = ["base"]
            self.from_tables += " AS base"
            if self.query.get("from", None):
                for i, from_table in enumerate(self.query["from"]):
                    if " as " in from_table.lower():
                        self.table_aliases.append(from_table[from_table.lower().find(" as ") + 4:])
                        self.from_tables += ",%s" % from_table
                    else:
                        f_alias = "t%s" % i
                        self.table_aliases.append(f_alias)
                        self.from_tables += ",%s as %s" % (from_table, f_alias)

            if self.query.get("where_join", None):
                self.where = "(" + self._build_where(self.query["where"]) + ") AND ((" + ") AND (".join(self.query["where_join"]) + "))"
            else:
                self.where = self._build_where(self.query["where"])

            self.order_by = self._build_order_by(self.query["order_by"])

            self.group_by = self.query.get("group_by", None)
            self.having = self.query.get("having", None)

        else:
            self.cols = ["id"]
            if not self.query["query_args"].get("id_only", True):
                self.cols.append("doc")

            if self.ds_sub:
                self.basetable += "_" + self.query["query_args"]["ds_sub"]
                self.from_tables = self.basetable
                self.table_aliases = [self.basetable]

            self.where = self._build_where(self.query["where"])
            self.order_by = self._build_order_by(self.query["order_by"])
            self.group_by = None
            self.having = None

    def _value(self, value, flatten_list=True):
        """Saves a value for later type conformant insertion into the query"""
        if value and type(value) in (list, tuple) and flatten_list:
            valstr = ",".join(self._value(val) for val in value)
            return valstr
        else:
            self._valcnt += 1
            valname = "v" + str(self._valcnt)
            self.values[valname] = value
            return "%(" + valname + ")s"

    def _sub_param(self, value):
        if not self.query_params or not isinstance(value, basestring):
            return value
        if value and value.startswith("$(") and value.endswith(")"):
            paramname = value[2:-1]
            # Fail silently if paramname not in dict
            return self.query_params.get(paramname, None)
        return value

    def _build_where(self, expr, table_prefix=None):
        """
        Builds a SQL filter expression string from given query expression
        @param expr  A query expression clause
        @param params  A dict holding values for parametric substitution
        @param table_prefix  Table prefix for column names to next clause in subqueries, e.g. "MYTABLE."
        """
        if not expr:
            return ""
        table_prefix = table_prefix or ""
        op, args = expr
        if op.startswith(DQ.OP_PREFIX):
            attname, value = args
            if self._is_standard_col(attname):
                return "%s%s%s%s" % (table_prefix, attname, self.OP_STR[op], self._value(self._sub_param(value)))
            else:
                return "json_string(%sdoc,%s)%s%s" % (table_prefix, self._value(attname), self.OP_STR[op],
                                                      self._value(str(self._sub_param(value))))
        elif op == DQ.XOP_IN:
            attname = args[0]
            values = args[1:]
            if self._is_standard_col(attname):
                in_exp = ",".join(["%s" % self._value(self._sub_param(val)) for val in values])
                return table_prefix + attname + " IN (" + in_exp + ")"
            else:
                in_exp = ",".join(["%s" % self._value(str(self._sub_param(val))) for val in values])
                return "json_string(%sdoc,%s) IN (%s)" % (table_prefix, self._value(attname), in_exp)
        elif op == DQ.XOP_BETWEEN:
            attname, value1, value2 = args
            if self._is_standard_col(attname):
                return "%s%s BETWEEN %s AND %s" % (table_prefix, attname,
                                                   self._value(self._sub_param(value1)),
                                                   self._value(self._sub_param(value2)))
            else:
                return "json_string(%sdoc,%s) BETWEEN %s AND %s" % (table_prefix, self._value(attname),
                                                                    self._value(self._sub_param(value1)),
                                                                    self._value(self._sub_param(value2)))
        elif op == DQ.XOP_ATTLIKE or op == DQ.XOP_ATTILIKE:
            attname, value = args
            return "json_string(%sdoc,%s) %s %s" % (table_prefix, self._value(attname), self.OP_STR[op],
                                                    self._value(self._sub_param(value)))
        elif op == DQ.XOP_ALLMATCH:
            value, cmpop = args
            if cmpop == DQ.TXT_CONTAINS:
                return "json_allattr(%sdoc) LIKE %s" % (table_prefix, self._value("%" + str(self._sub_param(value)) + "%"))
            else:   # default/others: ICONTAINS
                return "json_allattr(%sdoc) ILIKE %s" % (table_prefix, self._value("%" + str(self._sub_param(value)) + "%"))
        elif op == DQ.XOP_KEYWORD:
            value = args[0]
            kw_values = value if type(value) in (list, tuple) else [value]
            return "%s <@ json_keywords(%sdoc)" % (self._value(kw_values, flatten_list=False), table_prefix)
        elif op == DQ.XOP_ALTID:
            alt_id_ns, alt_id = args
            if type(alt_id_ns) in (list, tuple):
                alt_id_ns_value = self._value(alt_id_ns, flatten_list=False)
            else:
                alt_id_ns_value = self._value([self._sub_param(alt_id_ns)], flatten_list=False)
            if type(alt_id) in (list, tuple):
                alt_id_value = self._value(alt_id, flatten_list=False)
            else:
                alt_id_value = self._value([self._sub_param(alt_id)], flatten_list=False)
            if not alt_id and not alt_id_ns:
                return "json_altids_ns(%sdoc) IS NOT null" % table_prefix
            elif alt_id and not alt_id_ns:
                return "%s <@ json_altids_id(%sdoc)" % (alt_id_value, table_prefix)
            elif alt_id_ns and not alt_id:
                return "%s <@ json_altids_ns(%sdoc)" % (alt_id_ns_value, table_prefix)
            else:
                return "%s <@ json_altids_id(%sdoc) AND %s <@ json_altids_ns(%sdoc)" % (
                        alt_id_value, table_prefix, alt_id_ns_value, table_prefix)

        elif op.startswith(DQ.ROP_PREFIX):
            colname, x1, y1 = args
            return "%s%s %s %s::numrange" % (table_prefix, colname, self.OP_STR[op],
                                             self._value("[%s,%s]" % (self._sub_param(x1),
                                                                      self._sub_param(y1))))
        elif op.startswith(DQ.GOP_PREFIX):
            if op == DQ.GOP_DISTANCE:
                colname, point_x, point_y, dist, cmpop = args
                return "ST_Distance_Sphere(%s, ST_SetSRID(ST_Point(%s, %s),4326)) %s %s" % (
                    table_prefix+colname, self._value(self._sub_param(point_x)), self._value(self._sub_param(point_y)),
                    self.OP_STR[cmpop], self._value(self._sub_param(dist)))
            elif op.endswith('_geom'):
                colname, wkt, buf = args
                # PostGIS geometry from WKT http://postgis.net/docs/ST_GeomFromEWKT.html
                geom_from_wkt = 'ST_GeomFromEWKT(\'SRID=4326;%s\')' % (wkt)
                # if buffer specified, wrap geometry in buffer http://postgis.net/docs/ST_Buffer.html
                if buf:
                    postgis_cast = '' # we may need to cast PostGIS geography back to PostGIS geometry
                    if isinstance(buf,str):
                        if buf.lower().endswith('m'):
                            geom_from_wkt = '%s::geography' % geom_from_wkt # in meters instead of CRS units
                            buf = buf[:-1] # remove trailing 'm'
                            postgis_cast = '::geometry' # must be converted to PostGIS geometry for search/comparison
                    geom_from_wkt = 'ST_Buffer(%s, %f)%s' % (geom_from_wkt,float(buf),postgis_cast)
                return self.OP_STR[op] % (table_prefix+colname, geom_from_wkt)
            else:
               colname, x1, y1, x2, y2 = args
               return "%s %s ST_MakeEnvelope(%s,%s,%s,%s,4326)" % (table_prefix+colname, self.OP_STR[op],
                    self._value(x1), self._value(y1), self._value(x2), self._value(y2))
        elif op == DQ.EXP_AND:
            return "(%s)" % " AND ".join(self._build_where(ex, table_prefix=table_prefix) for ex in args)
        elif op == DQ.EXP_OR:
            return "(%s)" % " OR ".join(self._build_where(ex, table_prefix=table_prefix) for ex in args)
        elif op == DQ.EXP_NOT:
            return "NOT (%s)" % self._build_where(args[0], table_prefix=table_prefix)
        elif op == DQ.ASSOP_ASSOCIATED:
            # Find resources associated with an n-th degree resource
            target, target_type, predicate, direction, target_filter = args
            def assoc_level(lvnum, idcol):
                lvdir = direction[lvnum]
                lvpred = predicate[lvnum] if predicate and len(direction) > 1 else predicate
                ltab = "A" + str(lvnum)   # Alias name for this nesting level assoc table
                if lvdir == "S" or lvdir == "O":
                    idatt, aatt = ("s", "o") if lvdir == "S" else ("o", "s")
                    lvxpr = idcol + " IN (SELECT " + ltab + "." + idatt + " FROM " + self.basetable + "_assoc AS " + ltab + " WHERE "

                    if len(direction) <= lvnum + 1:
                        # Recursion end
                        if target and type(target) in (list, tuple):
                            lvxpr += ltab + "." + aatt + " IN ("
                            lvxpr += ",".join("%s" % self._value(self._sub_param(targ)) for targ in target) + ")"
                        elif target:
                            lvxpr += ltab + "." + aatt + "=%s" % self._value(self._sub_param(target))
                        elif target_type and type(target_type) in (list, tuple):
                            lvxpr += ltab + "." + aatt + "t IN ("
                            lvxpr += ",".join("%s" % self._value(self._sub_param(targ)) for targ in target_type) + ")"
                        elif target_type:
                            lvxpr += ltab + "." + aatt + "t=%s" % self._value(self._sub_param(target_type))
                            if target_filter:
                                lvxpr += " AND " + ltab + "." + aatt + " IN (SELECT id from " + self.basetable + " AS ART WHERE "
                                lvxpr += self._build_where(target_filter, table_prefix="ART.")
                                lvxpr += ")"
                        elif target_filter:
                            lvxpr += ltab + "." + aatt + " IN (SELECT id from " + self.basetable + " AS ART WHERE "
                            lvxpr += self._build_where(target_filter, table_prefix="ART.")
                            lvxpr += ")"
                        else:
                            raise BadRequest("Must provide target or target_type")

                    else:
                        # Inside recursion
                        lvxpr += assoc_level(lvnum + 1, ltab + "." + aatt)

                    # Add predicate clause
                    if lvpred and type(lvpred) in (list, tuple):
                        lvxpr += " AND " + ltab + ".p IN ("
                        lvxpr += ",".join("%s" % self._value(self._sub_param(pr)) for pr in lvpred) + ")"
                    elif lvpred:
                        lvxpr += " AND " + ltab + ".p=%s" % self._value(self._sub_param(lvpred))

                    lvxpr += ")"

                elif lvdir == "A":
                    raise NotImplementedError()
                else:
                    raise BadRequest("Illegal association direction: %s", lvdir)
                return lvxpr

            if target and target_type:
                raise BadRequest("Cannot provide both target and target_type")
            if target and target_filter:
                raise BadRequest("Cannot provide both target and target_filter")
            direction = direction or "A"
            if predicate and len(direction) > 1 and len(direction) != len(predicate):
                raise BadRequest("Number of predicate expressions must match level of nested associations")

            # id in (select from assoc where xxx)
            xpr = assoc_level(0, "id")
            return xpr
        elif op == DQ.ASSOP_DESCEND_O or op == DQ.ASSOP_DESCEND_S:
            # Find resources that are child of a resource.
            # Can limit search depth, predicate, child type and does not follow cycles.
            target, target_type, predicate, max_depth = args
            assoc_table = self.basetable if self.basetable.endswith("_assoc") else self.basetable + "_assoc"
            if predicate and type(predicate) not in (list, tuple):
                predicate = [predicate]
            if predicate:
                predval = ",".join("%s" % self._value(self._sub_param(p)) for p in predicate)
            if target_type and type(target_type) not in (list, tuple):
                target_type = [target_type]
            if target_type:
                ttypeval = ",".join("%s" % self._value(self._sub_param(targ)) for targ in target_type)
            idatt, aatt = ("s", "o") if op == DQ.ASSOP_DESCEND_O else ("o", "s")
            xpr = "id IN ("
            xpr += "WITH RECURSIVE ch_res(chid, path, depth, cycle) AS ("
            xpr += "SELECT " + aatt + ", ARRAY[id::text], 1, false FROM " + assoc_table
            xpr += " WHERE " + idatt + "=%s" % self._value(self._sub_param(target))
            if predicate:
                xpr += " AND p IN (%s)" % predval
            if target_type:
                xpr += " AND " + aatt + "t IN (%s)" % ttypeval
            xpr += " UNION ALL "
            xpr += "SELECT ass." + aatt + ", ARRAY[ass.id::text] || ch.path, ch.depth + 1, ass.id=ANY(ch.path) FROM ch_res ch, " + assoc_table + " ass"
            xpr += " WHERE ass." + idatt + " = ch.chid AND NOT ch.cycle"
            if max_depth > 0:
                xpr += " AND ch.depth<%s" % self._value(max_depth)
            if predicate:
                xpr += " AND ass.p IN (%s)" % predval
            if target_type:
                xpr += " AND " + aatt + "t IN (%s)" % ttypeval
            if self.basetable.endswith("_assoc"):
                xpr += ") SELECT path[1] FROM ch_res)"
            else:
                xpr += ") SELECT chid FROM ch_res)"
            return xpr
        else:
            raise BadRequest("Unknown op: %s" % op)

    def _build_order_by(self, expr):
        if not expr:
            return ""
        order_by_list = []
        for col, colsort in expr:
            order_by_list.append("%s %s" % (col, "DESC" if colsort.lower() == "desc" else "ASC"))
        order_by = ",".join(order_by_list)
        return order_by

    def get_query(self):
        qargs = self.query["query_args"]
        frags = []
        frags.append("SELECT ")
        frags.append(",".join(self.cols))
        frags.append(" FROM ")
        frags.append(self.from_tables)
        if self.where:
            frags.append(" WHERE ")
            frags.append(self.where)
        if self.group_by:
            frags.append(" GROUP BY ")
            frags.append(self.group_by)
            if self.having:
                frags.append(" HAVING ")
                frags.append(self.having)
        if self.order_by:
            frags.append(" ORDER BY ")
            frags.append(self.order_by)
        if qargs.get("limit", 0) > 0:
            frags.append(" LIMIT ")
            frags.append(str(qargs["limit"]))
        if qargs.get("skip", 0) > 0:
            frags.append(" OFFSET ")
            frags.append(str(qargs["skip"]))

        query_str = "".join(frags)
        #print "###SQL:", query_str
        return query_str

    def get_values(self):
        return self.values

    def _is_standard_col(self, col):
        datastore = self.query["query_args"].get("datastore", "")
        profile = self.query["query_args"].get("profile", "")
        ds_sub = self.query["query_args"].get("ds_sub", "")
        if profile == DataStore.DS_PROFILE.RESOURCES and ds_sub == "assoc":
            return col in {"id", "s", "st", "p", "o", "ot"}
        elif profile == DataStore.DS_PROFILE.RESOURCES:
            return col in {"id", "type_", "name", "lcstate", "availability", "ts_created", "ts_updated"}
        elif profile == DataStore.DS_PROFILE.EVENTS:
            return col in {"id", "type_", "origin", "origin_type", "sub_type", "actor_id"}
        raise BadRequest("Unknown query profile")

    def get_base_alias(self):
        return "base"
