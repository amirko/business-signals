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


def validate_read_query(sql: str, max_rows: int = 500) -> str:
    """Return a normalized, row-limited SELECT or raise before touching a database."""
    if not sql or not sql.strip():
        raise UnsafeQueryError("Query cannot be empty")
    if re.search(r"--|/\*|\*/", sql):
        raise UnsafeQueryError("SQL comments are not allowed")

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
        qualified_name = f"{table.db}.{table_name}" if table.db else table_name
        if qualified_name not in allowed_tables and table_name not in allowed_unqualified:
            raise UnsafeQueryError(f"Query references a table outside the selected datasource: {qualified_name}")
    return sql


def query_references_table(sql: str, table_name: str) -> bool:
    """Return whether a query reads a physical table, allowing qualified or bare names."""
    statement = sqlglot.parse_one(sql, read="postgres")
    bare_name = table_name.rsplit(".", 1)[-1]
    return any(table.name == bare_name for table in statement.find_all(exp.Table))
