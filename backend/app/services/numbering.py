"""Gapless, concurrency-safe document numbers: PO-2026-00452 style.

A plain Postgres SEQUENCE can't do this — a rolled-back transaction still
burns a value, leaving a gap (see docs/er-diagram.md). Instead, each
(org, doc_type, financial_year) has one counter row in document_sequences,
and allocation is a single atomic `UPDATE ... SET last_value = last_value +
1 RETURNING last_value`. Postgres takes the row lock as part of the UPDATE
itself, so concurrent callers serialize on that row without any explicit
SELECT ... FOR UPDATE — the second caller simply waits for the first
UPDATE's transaction to commit or roll back before its own UPDATE proceeds.
"""

import uuid
from datetime import date

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.numbering import DocumentSequence

# doc_type -> number prefix
PREFIXES = {
    "purchase_requisition": "PR",
    "purchase_order": "PO",
    "goods_receipt": "GRN",
    "sales_order": "SO",
    "delivery": "DL",
    "customer_invoice": "INV",
    "credit_note": "CN",
    "debit_note": "DN",
}


def financial_year(on: date | None = None) -> str:
    """Indian financial year (Apr-Mar): 2026-06-15 -> '2026-2027',
    2026-02-15 -> '2025-2026'.
    """
    d = on or date.today()
    start_year = d.year if d.month >= 4 else d.year - 1
    return f"{start_year}-{start_year + 1}"


async def next_document_number(
    session: AsyncSession, *, org_id: uuid.UUID, doc_type: str, on: date | None = None
) -> str:
    if doc_type not in PREFIXES:
        raise ValueError(f"unknown doc_type: {doc_type!r}")

    fy = financial_year(on)

    # Ensure the counter row exists — races here are harmless: at most one
    # INSERT wins, the other(s) no-op, and every caller proceeds to the
    # UPDATE below regardless of who inserted it.
    await session.execute(
        pg_insert(DocumentSequence)
        .values(org_id=org_id, doc_type=doc_type, financial_year=fy, last_value=0)
        .on_conflict_do_nothing(
            index_elements=["org_id", "doc_type", "financial_year"],
        )
    )

    result = await session.execute(
        update(DocumentSequence)
        .where(
            DocumentSequence.org_id == org_id,
            DocumentSequence.doc_type == doc_type,
            DocumentSequence.financial_year == fy,
        )
        .values(last_value=DocumentSequence.last_value + 1)
        .returning(DocumentSequence.last_value)
    )
    next_value = result.scalar_one()

    fy_short = fy.split("-")[0]
    return f"{PREFIXES[doc_type]}-{fy_short}-{next_value:05d}"
