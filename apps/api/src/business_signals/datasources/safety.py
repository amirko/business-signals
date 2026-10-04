from __future__ import annotations

import re

import sqlglot
from sqlglot import expressions as exp


class UnsafeQueryError(ValueError):
    pass


_FORBIDDEN = {
    exp.Alter,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Grant,
    exp.Insert,
    exp.Merge,
    exp.TruncateTable,
    exp.Update,
}

# The lookup result is application data rather than a user-facing query result.
# A stable neutral alias means traversal never relies on another datasource's
# table or column spelling.
CROSS_DATASOURCE_LOOKUP_KEY = "relationship_key"


def validate_read_query(sql: str, max_rows: int = 500) -> str:
    """Return a normalized, row-limited SELECT or raise before touching a database."""
    if not sql or not sql.strip():
        raise UnsafeQueryError("Query cannot be empty")
    if re.search(r"--|/\*|\*/", sql):
        raise UnsafeQueryError("SQL comments are not allowed")
    # SQLAlchemy's PostgreSQL pyformat dialect reserves this syntax for bound
    # parameters. Generated SQL must use ordinary PostgreSQL expressions, not
    # DB-API interpolation tokens; otherwise compilation can raise a raw
    # KeyError before the database sees the read-only query.
    if re.search(r"%\([A-Za-z_][A-Za-z0-9_]*\)[A-Za-z]", sql):
        raise UnsafeQueryError("DB-API percent-style parameters are not allowed in SQL")

    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise UnsafeQueryError(f"Invalid SQL: {exc}") from exc

    if len(statements) != 1:
        raise UnsafeQueryError("Exactly one SQL statement is allowed")

    statement = statements[0]
    if any(statement.find(node_type) is not None for node_type in _FORBIDDEN):
        raise UnsafeQueryError("Only read-only SELECT statements are allowed")
    if not isinstance(statement, (exp.Select, exp.Union)):
        raise UnsafeQueryError("Query must be SELECT or WITH ... SELECT")
    if statement.find(exp.Into) is not None:
        raise UnsafeQueryError("SELECT INTO is not allowed")
    if statement.args.get("locks"):
        raise UnsafeQueryError("Locking reads are not allowed")

    limit = statement.args.get("limit")
    if limit is None:
        statement = statement.limit(max_rows)
    else:
        value = limit.expression
        if not isinstance(value, exp.Literal) or not value.is_int:
            raise UnsafeQueryError("LIMIT must be an integer literal")
        if int(value.this) > max_rows:
            statement.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    return statement.sql(dialect="postgres")


def validate_query_tables(sql: str, allowed_tables: set[str]) -> str:
    """Ensure a query only references discovered physical tables for one datasource."""
    try:
        statement = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise UnsafeQueryError(f"Invalid SQL: {exc}") from exc

    cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
    allowed_unqualified = {name.rsplit(".", 1)[-1] for name in allowed_tables}
    for table in statement.find_all(exp.Table):
        table_name = table.name
        if table_name in cte_names:
            continue
        # PostgreSQL accepts ``catalog.schema.table`` syntactically, but the
        # datasource ID is not a PostgreSQL catalog.  Treating it as though it
        # were a schema used to let a generated query bypass this guardrail and
        # fail at execution with a misleading cross-database error.
        if table.catalog:
            raise UnsafeQueryError(
                "Query must use schema.table names only; datasource IDs cannot appear in SQL"
            )
        qualified_name = f"{table.db}.{table_name}" if table.db else table_name
        if qualified_name not in allowed_tables and table_name not in allowed_unqualified:
            raise UnsafeQueryError(f"Query references a table outside the selected datasource: {qualified_name}")
    return sql


def validate_query_columns(sql: str, table_columns: dict[str, set[str]]) -> str:
    """Reject qualified references to columns absent from discovered metadata.

    This is deliberately narrower than a SQL type checker.  CTE output columns
    and unqualified expressions can be valid only after SQL scope resolution,
    while qualified physical-table references are deterministic.  Catching
    those before execution converts the most common malformed model query into
    a repairable guardrail failure instead of a database error.
    """
    try:
        statement = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise UnsafeQueryError(f"Invalid SQL: {exc}") from exc

    aliases: dict[str, set[str]] = {}
    cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
    for table in statement.find_all(exp.Table):
        if table.name in cte_names:
            continue
        qualified = f"{table.db}.{table.name}" if table.db else table.name
        columns = table_columns.get(qualified) or table_columns.get(table.name)
        if columns is None:
            # Table validation owns this error. Keeping this function focused
            # makes it useful as a second validation after filters are bound.
            continue
        aliases[table.alias_or_name] = columns
        aliases[table.name] = columns

    for column in statement.find_all(exp.Column):
        # ``*`` and CTE projections are not physical source columns.
        if column.is_star or not column.table:
            continue
        columns = aliases.get(column.table)
        if columns is not None and column.name not in columns:
            raise UnsafeQueryError(
                f"Query references a column outside the discovered schema: {column.table}.{column.name}"
            )
    return sql


def query_references_table(sql: str, table_name: str) -> bool:
    """Return whether a query reads a physical table, allowing qualified or bare names."""
    statement = sqlglot.parse_one(sql, read="postgres")
    bare_name = table_name.rsplit(".", 1)[-1]
    return any(table.name == bare_name for table in statement.find_all(exp.Table))


def validate_cross_datasource_lookup_projection(
    sql: str,
    projection_name: str = CROSS_DATASOURCE_LOOKUP_KEY,
    relationship_field: str | None = None,
) -> None:
    """Require one lookup to return one approved relationship key.

    A cross-datasource lookup is deliberately not a general-purpose query. Its
    only job is to retrieve values for one approved relationship, which the
    application then binds as a filter in the primary datasource.
    """
    try:
        statement = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise UnsafeQueryError(f"Invalid SQL: {exc}") from exc

    if isinstance(statement, exp.Union):
        raise UnsafeQueryError(
            "Each cross-datasource lookup must serve one approved relationship; "
            "do not combine different keys with UNION"
        )
    if not isinstance(statement, exp.Select) or len(statement.expressions) != 1:
        raise UnsafeQueryError(
            "Cross-datasource lookup must return exactly one approved relationship field"
        )
    if statement.expressions[0].alias_or_name != projection_name:
        raise UnsafeQueryError(
            "Cross-datasource lookup must return its one approved relationship key "
            f"as {projection_name!r}"
        )
    if relationship_field is None:
        return

    relationship_table, relationship_column = relationship_field.rsplit(".", 1)
    projected = statement.expressions[0]
    expression = projected.this if isinstance(projected, exp.Alias) else projected
    if not isinstance(expression, exp.Column) or expression.name != relationship_column:
        raise UnsafeQueryError("Cross-datasource lookup must project the approved relationship field")
    matching_tables = [
        table
        for table in statement.find_all(exp.Table)
        if (f"{table.db}.{table.name}" if table.db else table.name) == relationship_table
    ]
    if not matching_tables:
        raise UnsafeQueryError("Cross-datasource lookup must read the approved relationship table")
    if expression.table and expression.table not in {table.alias_or_name for table in matching_tables}:
        raise UnsafeQueryError("Cross-datasource lookup projects a key from the wrong table")
    if not expression.table and len(list(statement.find_all(exp.Table))) != 1:
        raise UnsafeQueryError("Cross-datasource lookup must qualify the approved relationship field")
