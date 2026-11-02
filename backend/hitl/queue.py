"""The approval queue: what a human is being asked to look at.

Two things are tracked beyond the list itself, because they are the leading indicators
of the human-bottleneck failure mode: the depth of the queue and the age of its oldest
item. A queue that is deep but fresh is a burst; one whose oldest item has been waiting
for hours is a capacity problem, and the distinction matters more than the count.
"""

from __future__ import annotations

from backend.database.repository import ReviewRepository
from backend.database.session import session_scope
from backend.models.enums import ReviewStatus
from backend.models.review import QueueDepth

#: Statuses that mean "a person needs to act".
ACTIONABLE_STATUSES = [ReviewStatus.PENDING_APPROVAL]


async def pending_rows(limit: int = 50, offset: int = 0) -> list:
    async with session_scope() as session:
        return await ReviewRepository(session).list_rows(
            statuses=ACTIONABLE_STATUSES, limit=limit, offset=offset
        )


async def queue_depth() -> QueueDepth:
    async with session_scope() as session:
        reviews = ReviewRepository(session)
        counts = await reviews.status_counts()
        oldest = await reviews.oldest_pending_age_seconds()
    return QueueDepth(
        queued=counts.get(ReviewStatus.QUEUED.value, 0),
        running=counts.get(ReviewStatus.RUNNING.value, 0),
        pending_approval=counts.get(ReviewStatus.PENDING_APPROVAL.value, 0),
        oldest_pending_seconds=oldest,
    )
