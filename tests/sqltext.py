"""
Read SQL as SQL, not as the English written around it.

These queries and migrations carry far more explanation than code, and the
explanation names the very things that must not be in the SQL — the columns
left out, the constraint deliberately not applied, the sentence just
deleted. A test that greps the raw text finds all of them in the paragraph
saying why they are absent.

It has happened four times:

  test_collect_actually_moves_the_money   found "ORDER BY" inside a comment
                                          and read the prose as the clause

  test_a_trade_in_motion_is_not_interrupted
                                          found the default sentence it had
                                          just removed, quoted in the
                                          comment recording its removal

  test_a_vendor_statement_shows_no_economics
                                          found "client", "export_rows" and
                                          every forbidden rate column in the
                                          docstring listing them as excluded

  test_a_deposit_divides_three_ways       found "rates_buy_between" in the
                                          comment explaining that trades
                                          deliberately does not have it

Three were caught because the assertion was the wrong way round and failed
loudly. That is luck: the same mistake pointed the other way passes a test
that is checking a sentence instead of a query, and nothing says so.
"""

from __future__ import annotations

import inspect
import re


def strip_sql_comments(sql: str) -> str:
    """Drop -- comments, keeping the code and the line structure."""
    return "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())


def strip_python_comments(src: str) -> str:
    """Drop # comments. For asserting on handler source rather than SQL."""
    return "\n".join(re.sub(r"#.*$", "", line) for line in src.splitlines())


def query_of(method) -> str:
    """
    The SQL literal a repo method sends, with its comments removed.

    Takes the triple-quoted string that follows the first conn.fetch /
    conn.execute, which is the query — NOT the method's docstring, which
    comes first and is the thing that keeps getting matched by accident.
    """
    src = inspect.getsource(method)
    for marker in ("conn.fetch", "conn.execute"):
        if marker in src:
            start = src.index('"""', src.index(marker))
            end = src.index('"""', start + 3)
            return strip_sql_comments(src[start + 3:end])
    raise AssertionError(f"{method.__name__} sends no query")


def table_of(schema: str, name: str) -> str:
    """One CREATE TABLE body from schema.sql, comments removed."""
    start = schema.index(f"CREATE TABLE {name}")
    body = schema[start:]
    return strip_sql_comments(body[:body.index(");")])
