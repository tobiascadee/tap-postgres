"""SQL client handling.

This includes PostgresStream and PostgresConnector.
"""

from __future__ import annotations

import datetime
import json
import select
import typing as t
from functools import cached_property
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterable, Mapping

import psycopg2
import singer_sdk.helpers._typing
import sqlalchemy as sa
from psycopg2 import extras
from singer_sdk import SQLConnector, SQLStream
from singer_sdk import typing as th
from singer_sdk._singerlib import CatalogEntry, MetadataMapping, Schema
from singer_sdk.helpers._state import increment_state
from singer_sdk.helpers._typing import TypeConformanceLevel
from singer_sdk.streams.core import REPLICATION_INCREMENTAL
from sqlalchemy.engine import Engine
from sqlalchemy.engine.reflection import Inspector

if TYPE_CHECKING:
    from singer_sdk.helpers.types import Context
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.engine import Engine
    from sqlalchemy.engine.reflection import Inspector
    from sqlalchemy.types import TypeEngine


def patched_conform(
    elem: Any,
    property_schema: dict,
) -> Any:
    """Overrides Singer SDK type conformance.

    Most logic here is from singer_sdk.helpers._typing._conform_primitive_property, as
    marked by "# copied". This is a full override rather than calling the "super"
    because the final piece of logic in the super `if is_boolean_type(property_schema):`
    is flawed. is_boolean_type will return True if the schema contains a boolean
    anywhere. Therefore, a jsonschema type like ["boolean", "integer"] will return true
    and will have its values coerced to either True or False. In practice, this occurs
    for columns with JSONB type: no guarantees can be made about their data, so the
    schema has every possible data type, including boolean. Without this override, all
    JSONB columns would be coerced to True or False.

    Modifications:
     - prevent dates from turning into datetimes.
     - prevent collapsing values to booleans. (discussed above)

    Converts a primitive (i.e. not object or array) to a json compatible type.

    Returns:
        The appropriate json compatible type.
    """
    if isinstance(elem, datetime.date):  # not copied, original logic
        return elem.isoformat()
    if isinstance(elem, (datetime.datetime,)):  # copied
        return singer_sdk.helpers._typing.to_json_compatible(elem)
    if isinstance(elem, datetime.timedelta):  # copied
        epoch = datetime.datetime.fromtimestamp(0, datetime.timezone.utc)
        timedelta_from_epoch = epoch + elem
        if timedelta_from_epoch.tzinfo is None:
            timedelta_from_epoch = timedelta_from_epoch.replace(
                tzinfo=datetime.timezone.utc
            )
        return timedelta_from_epoch.isoformat()
    if isinstance(elem, datetime.time):  # copied
        return str(elem)
    if isinstance(elem, bytes):  # copied, modified to import is_boolean_type
        # for BIT value, treat 0 as False and anything else as True
        # Will only due this for booleans, not `bytea` data.
        return (
            elem != b"\x00"
            if singer_sdk.helpers._typing.is_boolean_type(property_schema)
            else elem.hex()
        )
    return elem


singer_sdk.helpers._typing._conform_primitive_property = patched_conform


class PostgresConnector(SQLConnector):
    """Connects to the Postgres SQL source."""

    def __init__(
        self,
        config: dict | None = None,
        sqlalchemy_url: str | None = None,
    ) -> None:
        """Initialize the SQL connector.

        Args:
          config: The parent tap or target object's config.
          sqlalchemy_url: Optional URL for the connection.

        """
        # Dates in postgres don't all convert to python datetime objects, so we
        # need to register a custom type caster to convert these to a string
        # See https://www.psycopg.org/psycopg3/docs/advanced/adapt.html#example-handling-infinity-date # noqa: E501
        # For more information
        if config is not None and config["dates_as_string"] is True:
            string_dates = psycopg2.extensions.new_type(
                (1082, 1114, 1184), "STRING_DATES", psycopg2.STRING
            )
            string_date_arrays = psycopg2.extensions.new_array_type(
                (1182, 1115, 1188), "STRING_DATE_ARRAYS[]", psycopg2.STRING
            )
            psycopg2.extensions.register_type(string_dates)
            psycopg2.extensions.register_type(string_date_arrays)

        super().__init__(config=config, sqlalchemy_url=sqlalchemy_url)

    # Note super is static, we can get away with this because this is called once
    # and is luckily referenced via the instance of the class
    def to_jsonschema_type(  # type: ignore[override]
        self,
        sql_type: str | TypeEngine | type[TypeEngine] | postgresql.ARRAY | Any,
    ) -> dict:
        """Return a JSON Schema representation of the provided type.

        Overridden from SQLConnector to correctly handle JSONB and Arrays.

        Also Overridden in order to call our instance method `sdk_typing_object()`
        instead of the static version

        By default will call `typing.to_jsonschema_type()` for strings and SQLAlchemy
        types.

        Args:
            sql_type: The string representation of the SQL type, a SQLAlchemy
                TypeEngine class or object, or a custom-specified object.

        Raises:
            ValueError: If the type received could not be translated to jsonschema.

        Returns:
            The JSON Schema representation of the provided type.

        """
        type_name = None
        if isinstance(sql_type, str):
            type_name = sql_type
        elif isinstance(sql_type, sa.types.TypeEngine):
            type_name = type(sql_type).__name__

        if (
            type_name is not None
            and isinstance(sql_type, sa.dialects.postgresql.ARRAY)
            and type_name == "ARRAY"
        ):
            array_type = self.sdk_typing_object(sql_type.item_type)
            return th.ArrayType(array_type).type_dict
        return self.sdk_typing_object(sql_type).type_dict

    def sdk_typing_object(
        self,
        from_type: str | TypeEngine | type[TypeEngine],
    ) -> (
        th.DateTimeType
        | th.NumberType
        | th.IntegerType
        | th.DateType
        | th.StringType
        | th.BooleanType
        | th.CustomType
    ):
        """Return the JSON Schema dict that describes the sql type.

        Args:
            from_type: The SQL type as a string or as a TypeEngine. If a TypeEngine is
                provided, it may be provided as a class or a specific object instance.

        Raises:
            ValueError: If the `from_type` value is not of type `str` or `TypeEngine`.

        Returns:
            A compatible JSON Schema type definition.
        """
        # NOTE: This is an ordered mapping, with earlier mappings taking precedence. If
        # the SQL-provided type contains the type name on the left, the mapping will
        # return the respective singer type.
        # NOTE: jsonb and json should theoretically be th.AnyType().type_dict but that
        # causes problems down the line with an error like:
        # singer_sdk.helpers._typing.EmptySchemaTypeError: Could not detect type from
        # empty type_dict. Did you forget to define a property in the stream schema?
        sqltype_lookup: dict[
            str,
            th.DateTimeType
            | th.NumberType
            | th.IntegerType
            | th.DateType
            | th.StringType
            | th.BooleanType
            | th.CustomType,
        ] = {
            "jsonb": th.CustomType(
                {"type": ["string", "number", "integer", "array", "object", "boolean"]}
            ),
            "json": th.CustomType(
                {"type": ["string", "number", "integer", "array", "object", "boolean"]}
            ),
            "timestamp": th.DateTimeType(),
            "datetime": th.DateTimeType(),
            "date": th.DateType(),
            "int": th.IntegerType(),
            "numeric": th.NumberType(),
            "decimal": th.NumberType(),
            "double": th.NumberType(),
            "float": th.NumberType(),
            "real": th.NumberType(),
            "float4": th.NumberType(),
            "string": th.StringType(),
            "text": th.StringType(),
            "char": th.StringType(),
            "bool": th.BooleanType(),
            "variant": th.StringType(),
        }
        if self.config["dates_as_string"] is True:
            sqltype_lookup["date"] = th.StringType()
            sqltype_lookup["datetime"] = th.StringType()
        if isinstance(from_type, str):
            type_name = from_type
        elif isinstance(from_type, sa.types.TypeEngine):
            type_name = type(from_type).__name__
        elif isinstance(from_type, type) and issubclass(from_type, sa.types.TypeEngine):
            type_name = from_type.__name__
        else:
            raise ValueError(
                "Expected `str` or a SQLAlchemy `TypeEngine` object or type."
            )

        # Look for the type name within the known SQL type names:
        for sqltype, jsonschema_type in sqltype_lookup.items():
            if sqltype.lower() in type_name.lower():
                return jsonschema_type

        return sqltype_lookup["string"]  # safe failover to str

    def get_schema_names(self, engine: Engine, inspected: Inspector) -> list[str]:
        """Return a list of schema names in DB, or overrides with user-provided values.

        Args:
            engine: SQLAlchemy engine
            inspected: SQLAlchemy inspector instance for engine

        Returns:
            List of schema names
        """
        if "filter_schemas" in self.config and len(self.config["filter_schemas"]) != 0:
            return self.config["filter_schemas"]
        return super().get_schema_names(engine, inspected)

    def discover_catalog_entry(
        self,
        engine: Engine,
        inspected: Inspector,
        schema_name: str,
        table_name: str,
        is_view: bool,
    ) -> CatalogEntry:
        """Create `CatalogEntry` object for the given table or a view.

        Args:
            engine: SQLAlchemy engine
            inspected: SQLAlchemy inspector instance for engine
            schema_name: Schema name to inspect
            table_name: Name of the table or a view
            is_view: Flag whether this object is a view, returned by `get_object_names`

        Returns:
            `CatalogEntry` object for the given table or a view
        """
        # Initialize unique stream name
        unique_stream_id = f"{schema_name}-{table_name}"

        # Detect key properties
        possible_primary_keys: list[list[str]] = []
        pk_def = inspected.get_pk_constraint(table_name, schema=schema_name)
        if pk_def and "constrained_columns" in pk_def:
            possible_primary_keys.append(pk_def["constrained_columns"])

        # An element of the columns list is ``None`` if it's an expression and is
        # returned in the ``expressions`` list of the reflected index.
        possible_primary_keys.extend(
            index_def["column_names"]  # type: ignore[misc]
            for index_def in inspected.get_indexes(table_name, schema=schema_name)
            if index_def.get("unique", False)
        )

        key_properties = next(iter(possible_primary_keys), None)

        # Initialize columns list
        table_schema = th.PropertiesList()
        for column_def in inspected.get_columns(table_name, schema=schema_name):
            column_name = column_def["name"]
            is_nullable = column_def.get("nullable", False)
            jsonschema_type: dict = self.to_jsonschema_type(column_def["type"])
            if hasattr(column_def["type"], "length"):
                jsonschema_type["maxLength"] = column_def["type"].length
            table_schema.append(
                th.Property(
                    name=column_name,
                    wrapped=th.CustomType(jsonschema_type),
                    nullable=is_nullable,
                    required=column_name in key_properties if key_properties else False,
                ),
            )
        schema = table_schema.to_dict()

        # Initialize available replication methods
        addl_replication_methods: list[str] = [""]  # By default an empty list.
        # Notes regarding replication methods:
        # - 'INCREMENTAL' replication must be enabled by the user by specifying
        #   a replication_key value.
        # - 'LOG_BASED' replication must be enabled by the developer, according
        #   to source-specific implementation capabilities.
        replication_method = next(reversed(["FULL_TABLE", *addl_replication_methods]))

        # Create the catalog entry object
        return CatalogEntry(
            tap_stream_id=unique_stream_id,
            stream=unique_stream_id,
            table=table_name,
            key_properties=key_properties,
            schema=Schema.from_dict(schema),
            is_view=is_view,
            replication_method=replication_method,
            metadata=MetadataMapping.get_standard_metadata(
                schema_name=schema_name,
                schema=schema,
                replication_method=replication_method,
                key_properties=key_properties,
                valid_replication_keys=None,  # Must be defined by user
            ),
            database=None,  # Expects single-database context
            row_count=None,
            stream_alias=None,
            replication_key=None,  # Must be defined by user
        )


class PostgresStream(SQLStream):
    """Stream class for Postgres streams."""

    connector_class = PostgresConnector
    supports_nulls_first = True

    # JSONB Objects won't be selected without type_conformance_level to ROOT_ONLY
    TYPE_CONFORMANCE_LEVEL = TypeConformanceLevel.ROOT_ONLY

    def max_record_count(self) -> int | None:
        """Return the maximum number of records to fetch in a single query."""
        return self.config.get("max_record_count")

    # Get records from stream
    def get_records(self, context: Context | None) -> t.Iterable[dict[str, t.Any]]:
        """Return a generator of record-type dictionary objects.

        If the stream has a replication_key value defined, records will be sorted by the
        incremental key. If the stream also has an available starting bookmark, the
        records will be filtered for values greater than or equal to the bookmark value.

        Args:
            context: If partition context is provided, will read specifically from this
                data slice.

        Yields:
            One dict per record.

        Raises:
            NotImplementedError: If partition is passed in context and the stream does
                not support partitioning.
        """
        if context:
            msg = f"Stream '{self.name}' does not support partitioning."
            raise NotImplementedError(msg)

        selected_column_names = self.get_selected_schema()["properties"].keys()
        table = self.connector.get_table(
            full_table_name=self.fully_qualified_name,
            column_names=selected_column_names,
        )
        query = table.select()

        if self.replication_key:
            replication_key_col = table.columns[self.replication_key]
            order_by = (
                sa.nulls_first(replication_key_col.asc())
                if self.supports_nulls_first
                else replication_key_col.asc()
            )
            query = query.order_by(order_by)

            start_val = self.get_starting_replication_key_value(context)
            if start_val:
                query = query.where(replication_key_col >= start_val)

        if self.ABORT_AT_RECORD_COUNT is not None:
            # Limit record count to one greater than the abort threshold. This ensures
            # `MaxRecordsLimitException` exception is properly raised by caller
            # `Stream._sync_records()` if more records are available than can be
            # processed.
            query = query.limit(self.ABORT_AT_RECORD_COUNT + 1)

        if self.max_record_count():
            query = query.limit(self.max_record_count())

        with self.connector._connect() as conn:
            for record in conn.execute(query).mappings():
                # TODO: Standardize record mapping type
                # https://github.com/meltano/sdk/issues/2096
                transformed_record = self.post_process(dict(record))
                if transformed_record is None:
                    # Record filtered out during post_process()
                    continue
                yield transformed_record


class PostgresLogBasedStream(SQLStream):
    """Stream class for Postgres log-based streams."""

    connector_class = PostgresConnector

    # JSONB Objects won't be selected without type_confomance_level to ROOT_ONLY
    TYPE_CONFORMANCE_LEVEL = TypeConformanceLevel.ROOT_ONLY

    replication_key = "_sdc_lsn"

    @property
    def config(self) -> Mapping[str, Any]:
        """Return a read-only config dictionary."""
        return MappingProxyType(self._config)

    @cached_property
    def schema(self) -> dict:
        """Override schema for log-based replication adding _sdc columns."""
        schema_dict = t.cast(dict, self._singer_catalog_entry.schema.to_dict())
        for property in schema_dict["properties"].values():
            if isinstance(property["type"], list):
                property["type"].append("null")
            else:
                property["type"] = [property["type"], "null"]
        if "required" in schema_dict:
            schema_dict.pop("required")
        schema_dict["properties"].update({"_sdc_deleted_at": {"type": ["string"]}})
        schema_dict["properties"].update({"_sdc_lsn": {"type": ["integer"]}})
        return schema_dict

    def _increment_stream_state(
        self,
        latest_record: dict[str, Any],
        *,
        context: Context | None = None,
    ) -> None:
        """Update state of stream or partition with data from the provided record.

        The default implementation does not advance any bookmarks unless
        `self.replication_method == 'INCREMENTAL'`. For us, `self.replication_method ==
        'LOG_BASED'`, so an override is required.
        """
        # This also creates a state entry if one does not yet exist:
        state_dict = self.get_context_state(context)

        # Advance state bookmark values if applicable
        if latest_record:  # This is the only line that has been overridden.
            if not self.replication_key:
                msg = (
                    f"Could not detect replication key for '{self.name}' "
                    f"stream(replication method={self.replication_method})"
                )
                raise ValueError(msg)
            treat_as_sorted = self.is_sorted()
            if not treat_as_sorted and self.state_partitioning_keys is not None:
                # Streams with custom state partitioning are not resumable.
                treat_as_sorted = False
            increment_state(
                state_dict,
                replication_key=self.replication_key,
                latest_record=latest_record,
                is_sorted=treat_as_sorted,
                check_sorted=self.check_sorted,
            )

    def get_records(self, context: Context | None) -> Iterable[dict[str, Any]]:
        """Return a generator of row-type dictionary objects."""
        status_interval = 5.0  # if no records in 5 seconds the tap can exit
        start_lsn = self.get_starting_replication_key_value(context=context)
        if start_lsn is None:
            start_lsn = 0
        logical_replication_connection = self.logical_replication_connection()
        logical_replication_cursor = logical_replication_connection.cursor()

        # Flush logs from the previous sync. send_feedback() will only flush LSNs before
        # the value of flush_lsn, not including the value of flush_lsn, so this is safe
        # even though we still want logs with an LSN == start_lsn.
        logical_replication_cursor.send_feedback(flush_lsn=start_lsn)

        logical_replication_cursor.start_replication(
            slot_name="tappostgres",
            decode=True,
            start_lsn=start_lsn,
            status_interval=status_interval,
            options={
                "format-version": 2,
                "include-transaction": False,
                "add-tables": self.fully_qualified_name,
            },
        )

        # Using scaffolding layout from:
        # https://www.psycopg.org/docs/extras.html#psycopg2.extras.ReplicationCursor
        while True:
            message = logical_replication_cursor.read_message()
            if message:
                row = self.consume(message)
                if row:
                    yield row
            else:
                timeout = (
                    status_interval
                    - (
                        datetime.datetime.now()
                        - logical_replication_cursor.feedback_timestamp
                    ).total_seconds()
                )
                try:
                    # If the timeout has passed and the cursor still has no new
                    # messages, the sync has completed.
                    if (
                        select.select(
                            [logical_replication_cursor], [], [], max(0, timeout)
                        )[0]
                        == []
                    ):
                        break
                except InterruptedError:
                    pass

        logical_replication_cursor.close()
        logical_replication_connection.close()

    def consume(self, message) -> dict | None:
        """Ingest WAL message."""
        try:
            message_payload = json.loads(message.payload)
        except json.JSONDecodeError:
            self.logger.warning(
                "A message payload of %s could not be converted to JSON",
                message.payload,
            )
            return {}

        row = {}

        upsert_actions = {"I", "U"}
        delete_actions = {"D"}
        truncate_actions = {"T"}
        transaction_actions = {"B", "C"}

        if message_payload["action"] in upsert_actions:
            for column in message_payload["columns"]:
                row.update({column["name"]: column["value"]})
            row.update({"_sdc_deleted_at": None})
            row.update({"_sdc_lsn": message.data_start})
        elif message_payload["action"] in delete_actions:
            for column in message_payload["identity"]:
                row.update({column["name"]: column["value"]})
            row.update(
                {
                    "_sdc_deleted_at": datetime.datetime.utcnow().strftime(
                        r"%Y-%m-%dT%H:%M:%SZ"
                    )
                }
            )
            row.update({"_sdc_lsn": message.data_start})
        elif message_payload["action"] in truncate_actions:
            self.logger.debug(
                (
                    "A message payload of %s (corresponding to a truncate action) "
                    "could not be processed."
                ),
                message.payload,
            )
        elif message_payload["action"] in transaction_actions:
            self.logger.debug(
                (
                    "A message payload of %s (corresponding to a transaction beginning "
                    "or commit) could not be processed."
                ),
                message.payload,
            )
        else:
            raise RuntimeError(
                (
                    "A message payload of %s (corresponding to an unknown action type) "
                    "could not be processed."
                ),
                message.payload,
            )

        return row

    def logical_replication_connection(self):
        """A logical replication connection to the database.

        Uses a direct psycopg2 implementation rather than through sqlalchemy.
        """
        connection_string = (
            f"dbname={self.config['database']} user={self.config['user']} password="
            f"{self.config['password']} host={self.config['host']} port="
            f"{self.config['port']}"
        )
        return psycopg2.connect(
            connection_string,
            application_name="tap_postgres",
            connection_factory=extras.LogicalReplicationConnection,
        )

    # TODO: Make this change upstream in the SDK?
    # I'm not sure if in general SQL databases don't guarantee order of records log
    # replication, but at least Postgres does not.
    def is_sorted(self) -> bool:  # type: ignore[override]
        """Return True if the stream is sorted by the replication key."""
        return self.replication_method == REPLICATION_INCREMENTAL
