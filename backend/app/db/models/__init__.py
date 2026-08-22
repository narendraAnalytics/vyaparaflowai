"""Import every model module so Base.metadata is fully populated before
Alembic autogenerate or Base.metadata.create_all runs.
"""

from . import (  # noqa: F401
    catalog,
    enums,
    finance,
    inventory,
    numbering,
    org,
    partners,
    purchase,
    sales,
    workflow,
)
