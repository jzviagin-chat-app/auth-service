"""Database layer: the only place that knows about SQL and tables.

- engine.py  connection pool + session factory (created once at startup)
- models.py  table definitions (SQLAlchemy ORM classes); Alembic migrations are generated from them
- repos.py   small async functions that read/write rows; services call these, never SQL directly
"""
