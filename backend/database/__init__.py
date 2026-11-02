"""Data layer: one Postgres spine, two access styles.

- `session.py` / `models.py`  -> SQLAlchemy, the relational truth lane
- `pg.py`                     -> raw asyncpg, the hot paths (vectors, events, idempotency)
- `repository.py`             -> repositories over both, the only API the app uses
"""

from backend.database.models import (
    Base,
    DedupNearMiss,
    FindingRecord,
    HitlFeedback,
    HitlReview,
    PRReviewRecord,
    RepoFileIndex,
    SystemCapability,
)
from backend.database.pg import (
    check_pool,
    close_pool,
    create_pool,
    get_pool,
    read_capabilities,
)
from backend.database.session import (
    check_database,
    dispose_engine,
    get_engine,
    get_sessionmaker,
    session_scope,
)

__all__ = [
    "Base",
    "DedupNearMiss",
    "FindingRecord",
    "HitlFeedback",
    "HitlReview",
    "PRReviewRecord",
    "RepoFileIndex",
    "SystemCapability",
    "check_database",
    "check_pool",
    "close_pool",
    "create_pool",
    "dispose_engine",
    "get_engine",
    "get_pool",
    "get_sessionmaker",
    "read_capabilities",
    "session_scope",
]
