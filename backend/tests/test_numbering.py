import asyncio
import uuid

import pytest
from sqlalchemy import delete

from app.db.models.numbering import DocumentSequence
from app.db.models.org import Organization
from app.db.session import AsyncSessionLocal
from app.services.numbering import financial_year, next_document_number


@pytest.fixture
async def throwaway_org():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-numbering-{uuid.uuid4()}")
        session.add(org)
        await session.commit()
        org_id = org.id

    yield org_id

    async with AsyncSessionLocal() as session:
        await session.execute(delete(DocumentSequence).where(DocumentSequence.org_id == org_id))
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()


def test_financial_year_boundary():
    from datetime import date

    assert financial_year(date(2026, 6, 15)) == "2026-2027"
    assert financial_year(date(2026, 3, 31)) == "2025-2026"
    assert financial_year(date(2026, 4, 1)) == "2026-2027"


@pytest.mark.asyncio
async def test_numbering_format(throwaway_org):
    async with AsyncSessionLocal() as session:
        number = await next_document_number(
            session, org_id=throwaway_org, doc_type="purchase_order"
        )
        await session.commit()

    fy = financial_year().split("-")[0]
    assert number == f"PO-{fy}-00001"


@pytest.mark.asyncio
async def test_numbering_gapless_under_concurrency(throwaway_org):
    """20 genuinely concurrent callers, each in its own session/transaction —
    proves the atomic UPDATE...RETURNING serializes correctly against a real
    Neon connection, not just within one transaction talking to itself.
    """

    async def allocate() -> str:
        async with AsyncSessionLocal() as session:
            number = await next_document_number(
                session, org_id=throwaway_org, doc_type="sales_order"
            )
            await session.commit()
            return number

    results = await asyncio.gather(*[allocate() for _ in range(20)])

    assert len(set(results)) == 20, "duplicate document numbers allocated"
    sequence_values = sorted(int(r.rsplit("-", 1)[-1]) for r in results)
    assert sequence_values == list(range(1, 21)), "gap in allocated sequence"
