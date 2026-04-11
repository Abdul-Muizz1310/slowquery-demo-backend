"""Data access layer.

One repository per model. Repositories own SQL and are the only layer
that imports from ``sqlalchemy``. Services and routers never see
SQLAlchemy primitives directly.
"""
