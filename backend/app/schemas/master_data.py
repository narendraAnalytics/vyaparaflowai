"""Pydantic contracts for master-data CRUD (customers, suppliers, products,
warehouses). Field constraints mirror the DB CHECK constraints in
app/db/models/partners.py and catalog.py so invalid input is rejected with
a 422 before it ever reaches Postgres.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


_GSTIN_PATTERN = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"


class CustomerBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    gstin: str | None = Field(default=None, pattern=_GSTIN_PATTERN)
    state_code: str | None = Field(default=None, max_length=2)
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    credit_limit: float = Field(default=0, ge=0)
    payment_terms_days: int = Field(default=30, ge=0)


class CustomerCreate(CustomerBase):
    pass


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    gstin: str | None = Field(default=None, pattern=_GSTIN_PATTERN)
    state_code: str | None = Field(default=None, max_length=2)
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    credit_limit: float | None = Field(default=None, ge=0)
    payment_terms_days: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class CustomerOut(CustomerBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    is_active: bool


class SupplierBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    gstin: str | None = Field(default=None, pattern=_GSTIN_PATTERN)
    state_code: str | None = Field(default=None, max_length=2)
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    lead_time_days: int = Field(default=5, ge=0)
    bank_account_number: str | None = Field(default=None, max_length=30)
    bank_ifsc: str | None = Field(default=None, max_length=11)


class SupplierCreate(SupplierBase):
    pass


class SupplierUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    gstin: str | None = Field(default=None, pattern=_GSTIN_PATTERN)
    state_code: str | None = Field(default=None, max_length=2)
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    lead_time_days: int | None = Field(default=None, ge=0)
    bank_account_number: str | None = Field(default=None, max_length=30)
    bank_ifsc: str | None = Field(default=None, max_length=11)
    is_active: bool | None = None


class SupplierOut(SupplierBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    reliability_score: float
    is_active: bool


class ProductBase(BaseModel):
    sku: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    brand: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=100)
    hsn_code: str = Field(min_length=1, max_length=8)
    uom: str = Field(min_length=1, max_length=20)
    gst_rate: float = Field(ge=0, le=100)


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    sku: str | None = Field(default=None, min_length=1, max_length=50)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    brand: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=100)
    hsn_code: str | None = Field(default=None, min_length=1, max_length=8)
    uom: str | None = Field(default=None, min_length=1, max_length=20)
    gst_rate: float | None = Field(default=None, ge=0, le=100)
    is_active: bool | None = None


class ProductOut(ProductBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    is_active: bool


class WarehouseBase(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=150)
    address: str | None = Field(default=None, max_length=500)


class WarehouseCreate(WarehouseBase):
    pass


class WarehouseUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=20)
    name: str | None = Field(default=None, min_length=1, max_length=150)
    address: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None


class WarehouseOut(WarehouseBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    is_active: bool
