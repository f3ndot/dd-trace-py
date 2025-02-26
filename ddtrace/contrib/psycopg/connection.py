"""
Tracing utilities for the psycopg potgres client library.
"""
import functools

from psycopg2.extensions import connection
from psycopg2.extensions import cursor

from ddtrace import config

from ...constants import SPAN_MEASURED_KEY
from ...ext import SpanTypes
from ...ext import db
from ...ext import net
from ...ext import sql


class TracedCursor(cursor):
    """Wrapper around cursor creating one span per query"""

    def __init__(self, *args, **kwargs):
        self._datadog_tracer = kwargs.pop("datadog_tracer", None)
        self._datadog_service = kwargs.pop("datadog_service", None)
        self._datadog_tags = kwargs.pop("datadog_tags", None)
        super(TracedCursor, self).__init__(*args, **kwargs)

    def execute(self, query, vars=None):  # noqa: A002
        """just wrap the cursor execution in a span"""
        if not self._datadog_tracer:
            return cursor.execute(self, query, vars)

        with self._datadog_tracer.trace("postgres.query", service=self._datadog_service, span_type=SpanTypes.SQL) as s:
            # set component tag equal to name of integration
            s.set_tag_str("component", config.psycopg.integration_name)

            s.set_tag(SPAN_MEASURED_KEY)
            if not s.sampled:
                return super(TracedCursor, self).execute(query, vars)

            s.resource = query
            s.set_tags(self._datadog_tags)
            try:
                return super(TracedCursor, self).execute(query, vars)
            finally:
                s.set_metric("db.rowcount", self.rowcount)

    def callproc(self, procname, vars=None):  # noqa: A002
        """just wrap the execution in a span"""
        return cursor.callproc(self, procname, vars)


class TracedConnection(connection):
    """Wrapper around psycopg2  for tracing"""

    def __init__(self, *args, **kwargs):

        self._datadog_tracer = kwargs.pop("datadog_tracer", None)
        self._datadog_service = kwargs.pop("datadog_service", None)

        super(TracedConnection, self).__init__(*args, **kwargs)

        # add metadata (from the connection, string, etc)
        dsn = sql.parse_pg_dsn(self.dsn)
        self._datadog_tags = {
            net.TARGET_HOST: dsn.get("host"),
            net.TARGET_PORT: dsn.get("port"),
            db.NAME: dsn.get("dbname"),
            db.USER: dsn.get("user"),
            "db.application": dsn.get("application_name"),
        }

        self._datadog_cursor_class = functools.partial(
            TracedCursor,
            datadog_tracer=self._datadog_tracer,
            datadog_service=self._datadog_service,
            datadog_tags=self._datadog_tags,
        )

    def cursor(self, *args, **kwargs):
        """register our custom cursor factory"""
        kwargs.setdefault("cursor_factory", self._datadog_cursor_class)
        return super(TracedConnection, self).cursor(*args, **kwargs)
