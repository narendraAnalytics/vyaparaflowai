"""Idempotent demo data for Sri Lakshmi Hardware & Electricals.

Run with `make seed` (`uv run python -m app.db.seed`). Re-running is safe —
existing rows are looked up by their natural key (org name, SKU, GSTIN...)
and left alone rather than duplicated.
"""

import asyncio
import random

from sqlalchemy import select

from app.db.models.catalog import Product, ProductSupplier, Warehouse
from app.db.models.inventory import InventoryItem
from app.db.models.org import Organization
from app.db.models.partners import Customer, Supplier
from app.db.session import AsyncSessionLocal

random.seed(20260822)  # reproducible seed data across runs

ORG_NAME = "Sri Lakshmi Hardware & Electricals"

# (name, gstin, state_code, lead_time_days, reliability_score, category)
SUPPLIERS: list[tuple[str, str, str, int, int, str]] = [
    ("Konnect Wires Pvt Ltd", "29AAKCK1234F1Z5", "29", 5, 94, "wire"),
    ("ABC Electricals Distributors", "29AAECA5678G1Z2", "29", 4, 91, "electrical"),
    ("UltraBuild Cement & Steel Traders", "29AAFCU9012H1Z8", "29", 7, 88, "construction"),
    ("Asian Colours Paint House", "29AAGCA3456J1Z1", "29", 3, 96, "paint"),
    ("Sundar Hardware & Fittings Co", "29AAHCS7890K1Z4", "29", 6, 85, "general"),
]

CUSTOMERS = [
    ("Ramesh Constructions", "29ABCPR1111L1Z9", 500_000, 30),
    ("Sri Balaji Builders", "29ABCPB2222M1Z6", 800_000, 45),
    ("Karthik Electricals", "29ABCPK3333N1Z3", 150_000, 15),
    ("Ganesh Plumbing Works", "29ABCPG4444P1Z0", 100_000, 15),
    ("Anand Interiors", "29ABCPA5555Q1Z7", 300_000, 30),
    ("Sai Constructions", "29ABCPS6666R1Z4", 600_000, 30),
    ("Prakash Hardware Retail", None, 25_000, 7),
    ("Sundar Contractors", "29ABCPS7777S1Z1", 400_000, 30),
    ("Lakshmi Electricals", "29ABCPL8888T1Z8", 200_000, 15),
    ("Ravi Home Solutions", None, 50_000, 15),
]

# (sku, name, brand, category, hsn_code, uom, gst_rate, supplier_category, unit_price)
PRODUCTS: list[tuple[str, str, str, str, str, str, float, str, int]] = []


def _add(sku, name, brand, category, hsn, uom, gst, supplier_cat, price):
    PRODUCTS.append((sku, name, brand, category, hsn, uom, gst, supplier_cat, price))


# Wires — HSN 8544, 18% GST
for size in ["1.0mm", "1.5mm", "2.5mm", "4mm", "6mm"]:
    _add(
        f"WIRE-{size.replace('mm', '').replace('.', '')}",
        f"Finolex {size} FR-LSH Copper Wire (90m coil)",
        "Finolex",
        "Wires & Cables",
        "8544",
        "coil",
        18.00,
        "wire",
        420 + int(size.replace("mm", "").replace(".", "")) * 90,
    )
_add(
    "WIRE-FLEX-3C15",
    "Polycab 3-Core Flexible Cable 1.5sqmm",
    "Polycab",
    "Wires & Cables",
    "8544",
    "meter",
    18.00,
    "wire",
    42,
)
_add(
    "WIRE-FLEX-3C25",
    "Polycab 3-Core Flexible Cable 2.5sqmm",
    "Polycab",
    "Wires & Cables",
    "8544",
    "meter",
    18.00,
    "wire",
    68,
)
_add(
    "WIRE-EARTH-8G",
    "Copper Earthing Wire 8 Gauge",
    "Finolex",
    "Wires & Cables",
    "8544",
    "meter",
    18.00,
    "wire",
    95,
)

# PVC pipes & fittings — HSN 3917, 18% GST
for dia in ["0.5in", "0.75in", "1in", "1.5in", "2in"]:
    _add(
        f"PVC-PIPE-{dia.replace('in', '')}",
        f"Supreme PVC Pipe {dia} (3m length)",
        "Supreme",
        "Plumbing",
        "3917",
        "piece",
        18.00,
        "general",
        60 + int(float(dia.replace("in", "")) * 80),
    )
_add(
    "PVC-ELBOW-1",
    "PVC Elbow Fitting 1in",
    "Supreme",
    "Plumbing",
    "3917",
    "piece",
    18.00,
    "general",
    25,
)
_add("PVC-TEE-1", "PVC Tee Joint 1in", "Supreme", "Plumbing", "3917", "piece", 18.00, "general", 30)
_add(
    "PVC-COUPLER-1", "PVC Coupler 1in", "Supreme", "Plumbing", "3917", "piece", 18.00, "general", 18
)
_add(
    "CONDUIT-PIPE-20",
    "Electrical Conduit Pipe 20mm ISI (3m)",
    "AKG",
    "Plumbing",
    "3917",
    "piece",
    18.00,
    "electrical",
    55,
)

# Electrical accessories — HSN 8536, 18% GST
_add(
    "SW-1WAY-6A",
    "Anchor 1-Way Switch 6A",
    "Anchor",
    "Electrical Accessories",
    "8536",
    "piece",
    18.00,
    "electrical",
    35,
)
_add(
    "SW-2WAY-6A",
    "Anchor 2-Way Switch 6A",
    "Anchor",
    "Electrical Accessories",
    "8536",
    "piece",
    18.00,
    "electrical",
    45,
)
_add(
    "SOCKET-5A",
    "Anchor 5-Pin Socket 5A",
    "Anchor",
    "Electrical Accessories",
    "8536",
    "piece",
    18.00,
    "electrical",
    55,
)
_add(
    "SOCKET-15A",
    "Anchor 6-Pin Socket 15A",
    "Anchor",
    "Electrical Accessories",
    "8536",
    "piece",
    18.00,
    "electrical",
    95,
)
_add(
    "MCB-SP-16A",
    "Havells Single Pole MCB 16A",
    "Havells",
    "Electrical Accessories",
    "8536",
    "piece",
    18.00,
    "electrical",
    145,
)
_add(
    "MCB-SP-32A",
    "Havells Single Pole MCB 32A",
    "Havells",
    "Electrical Accessories",
    "8536",
    "piece",
    18.00,
    "electrical",
    165,
)
_add(
    "MCB-DP-40A",
    "Havells Double Pole MCB 40A",
    "Havells",
    "Electrical Accessories",
    "8536",
    "piece",
    18.00,
    "electrical",
    320,
)
_add(
    "MODULAR-PLATE-3M",
    "Anchor Modular Plate 3 Module",
    "Anchor",
    "Electrical Accessories",
    "8536",
    "piece",
    18.00,
    "electrical",
    40,
)
_add(
    "DOORBELL-ELEC",
    "Electric Door Bell with Chime",
    "Anchor",
    "Electrical Accessories",
    "8536",
    "piece",
    18.00,
    "electrical",
    220,
)
_add(
    "DISTBOX-8WAY",
    "Havells 8-Way Distribution Box",
    "Havells",
    "Electrical Accessories",
    "8536",
    "piece",
    18.00,
    "electrical",
    480,
)

# Cement — HSN 2523, 18% GST
_add(
    "CEMENT-UT-OPC43",
    "UltraTech OPC 43 Grade Cement (50kg)",
    "UltraTech",
    "Cement",
    "2523",
    "bag",
    18.00,
    "construction",
    390,
)
_add(
    "CEMENT-UT-OPC53",
    "UltraTech OPC 53 Grade Cement (50kg)",
    "UltraTech",
    "Cement",
    "2523",
    "bag",
    18.00,
    "construction",
    410,
)
_add(
    "CEMENT-ACC-PPC",
    "ACC PPC Cement (50kg)",
    "ACC",
    "Cement",
    "2523",
    "bag",
    18.00,
    "construction",
    385,
)

# TMT bars / MS rods — HSN 7214, 18% GST
for size in ["8mm", "10mm", "12mm", "16mm", "20mm", "25mm"]:
    _add(
        f"TMT-{size.replace('mm', '')}",
        f"JSW Fe500 TMT Bar {size} (12m length)",
        "JSW Steel",
        "Steel & TMT",
        "7214",
        "piece",
        18.00,
        "construction",
        180 + int(size.replace("mm", "")) * 25,
    )

# Paints — HSN 3208, 18% GST
_add(
    "PAINT-INT-EMU-1L",
    "Asian Paints Tractor Emulsion Interior (1L)",
    "Asian Paints",
    "Paints",
    "3208",
    "can",
    18.00,
    "paint",
    210,
)
_add(
    "PAINT-INT-EMU-4L",
    "Asian Paints Tractor Emulsion Interior (4L)",
    "Asian Paints",
    "Paints",
    "3208",
    "can",
    18.00,
    "paint",
    780,
)
_add(
    "PAINT-EXT-EMU-4L",
    "Asian Paints Apex Exterior Emulsion (4L)",
    "Asian Paints",
    "Paints",
    "3208",
    "can",
    18.00,
    "paint",
    1150,
)
_add(
    "PAINT-ENAMEL-1L",
    "Berger Enamel Paint Gloss (1L)",
    "Berger",
    "Paints",
    "3208",
    "can",
    18.00,
    "paint",
    340,
)
_add(
    "PAINT-PRIMER-4L",
    "Berger Wall Primer (4L)",
    "Berger",
    "Paints",
    "3208",
    "can",
    18.00,
    "paint",
    620,
)
_add(
    "PAINT-WOOD-1L",
    "Asian Paints Wood Finish Varnish (1L)",
    "Asian Paints",
    "Paints",
    "3208",
    "can",
    18.00,
    "paint",
    290,
)

# Nails & screws — HSN 7317/7318, 18% GST
_add(
    "NAIL-WIRE-2IN",
    "Wire Nails 2in (1kg)",
    "Local",
    "Fasteners",
    "7317",
    "kg",
    18.00,
    "general",
    130,
)
_add(
    "NAIL-WIRE-3IN",
    "Wire Nails 3in (1kg)",
    "Local",
    "Fasteners",
    "7317",
    "kg",
    18.00,
    "general",
    130,
)
_add(
    "SCREW-WOOD-1IN",
    "Wood Screws 1in (box of 100)",
    "GKW",
    "Fasteners",
    "7318",
    "box",
    18.00,
    "general",
    85,
)
_add(
    "SCREW-MACHINE-M6",
    "Machine Screws M6 (box of 100)",
    "GKW",
    "Fasteners",
    "7318",
    "box",
    18.00,
    "general",
    110,
)
_add(
    "BOLT-NUT-M8",
    "Bolt & Nut Set M8 (box of 50)",
    "GKW",
    "Fasteners",
    "7318",
    "box",
    18.00,
    "general",
    150,
)

# Hand tools — HSN 8203/8204/8205, 18% GST
_add(
    "TOOL-HAMMER-CLAW",
    "Claw Hammer 500g",
    "Stanley",
    "Hand Tools",
    "8205",
    "piece",
    18.00,
    "general",
    320,
)
_add(
    "TOOL-SCREWDRIVER-SET",
    "Screwdriver Set (6-piece)",
    "Stanley",
    "Hand Tools",
    "8205",
    "set",
    18.00,
    "general",
    450,
)
_add(
    "TOOL-PLIERS-8IN",
    "Combination Pliers 8in",
    "Taparia",
    "Hand Tools",
    "8203",
    "piece",
    18.00,
    "general",
    210,
)
_add(
    "TOOL-WRENCH-ADJ10",
    "Adjustable Wrench 10in",
    "Taparia",
    "Hand Tools",
    "8204",
    "piece",
    18.00,
    "general",
    380,
)
_add(
    "TOOL-TAPE-5M",
    "Measuring Tape 5m",
    "Freemans",
    "Hand Tools",
    "9017",
    "piece",
    18.00,
    "general",
    150,
)

# Bathroom / tap fittings (brass) — HSN 8481, 18% GST
_add(
    "TAP-PILLAR-BRASS",
    "Brass Pillar Tap",
    "Jaquar",
    "Bath Fittings",
    "8481",
    "piece",
    18.00,
    "general",
    650,
)
_add(
    "TAP-ANGLE-VALVE",
    "Brass Angle Valve",
    "Jaquar",
    "Bath Fittings",
    "8481",
    "piece",
    18.00,
    "general",
    420,
)
_add(
    "TAP-BIBCOCK",
    "Brass Bib Cock",
    "Jaquar",
    "Bath Fittings",
    "8481",
    "piece",
    18.00,
    "general",
    380,
)
_add(
    "TAP-SHOWER-HEAD",
    "Overhead Shower with Arm",
    "Jaquar",
    "Bath Fittings",
    "8481",
    "piece",
    18.00,
    "general",
    890,
)

# Basic materials — 5% GST (post GST 2.0 reform)
_add(
    "SAND-RIVER-CFT",
    "River Sand (per cft)",
    "Local",
    "Basic Materials",
    "2505",
    "cft",
    5.00,
    "construction",
    55,
)
_add(
    "BRICK-RED-CLAY",
    "Red Clay Bricks (per 100)",
    "Local",
    "Basic Materials",
    "6901",
    "hundred",
    5.00,
    "construction",
    750,
)
_add(
    "FLYASH-BRICK",
    "Fly Ash Bricks (per 100)",
    "Local",
    "Basic Materials",
    "6901",
    "hundred",
    5.00,
    "construction",
    680,
)
_add(
    "MCB-DP-63A",
    "Havells Double Pole MCB 63A",
    "Havells",
    "Electrical Accessories",
    "8536",
    "piece",
    18.00,
    "electrical",
    350,
)

CATEGORY_TO_SUPPLIER_IDX = {
    "wire": 0,
    "electrical": 1,
    "construction": 2,
    "paint": 3,
    "general": 4,
}


async def seed() -> None:
    async with AsyncSessionLocal() as session:
        org = (
            await session.execute(select(Organization).where(Organization.name == ORG_NAME))
        ).scalar_one_or_none()
        if org is None:
            org = Organization(
                name=ORG_NAME,
                legal_name=f"{ORG_NAME} Pvt Ltd",
                gstin="29AABCS1234A1Z5",
                state_code="29",
                address="14 Industrial Layout, Peenya, Bengaluru, Karnataka 560058",
            )
            session.add(org)
            await session.flush()
        print(f"organization: {org.name} ({org.id})")

        warehouse = (
            await session.execute(
                select(Warehouse).where(Warehouse.org_id == org.id, Warehouse.code == "MAIN")
            )
        ).scalar_one_or_none()
        if warehouse is None:
            warehouse = Warehouse(
                org_id=org.id,
                code="MAIN",
                name="Main Warehouse — Peenya",
                address=org.address,
            )
            session.add(warehouse)
            await session.flush()
        print(f"warehouse: {warehouse.name} ({warehouse.id})")

        suppliers: list[Supplier] = []
        for sup_name, gstin, state_code, lead_time, reliability, _category in SUPPLIERS:
            existing_supplier = (
                await session.execute(
                    select(Supplier).where(Supplier.org_id == org.id, Supplier.gstin == gstin)
                )
            ).scalar_one_or_none()
            if existing_supplier is None:
                slug = sup_name.lower().replace(" ", ".").replace("&", "and")
                existing_supplier = Supplier(
                    org_id=org.id,
                    name=sup_name,
                    gstin=gstin,
                    state_code=state_code,
                    phone=f"+91-80-{random.randint(20000000, 29999999)}",
                    email=f"{slug}@example.com",
                    lead_time_days=lead_time,
                    reliability_score=reliability,
                )
                session.add(existing_supplier)
                await session.flush()
            suppliers.append(existing_supplier)
        print(f"suppliers: {len(suppliers)}")

        customers: list[Customer] = []
        for cust_name, cust_gstin, credit_limit, terms in CUSTOMERS:
            existing_customer = (
                await session.execute(
                    select(Customer).where(Customer.org_id == org.id, Customer.name == cust_name)
                )
            ).scalar_one_or_none()
            if existing_customer is None:
                existing_customer = Customer(
                    org_id=org.id,
                    name=cust_name,
                    gstin=cust_gstin,
                    state_code="29" if cust_gstin else None,
                    phone=f"+91-{random.randint(7000000000, 9999999999)}",
                    credit_limit=credit_limit,
                    payment_terms_days=terms,
                )
                session.add(existing_customer)
                await session.flush()
            customers.append(existing_customer)
        print(f"customers: {len(customers)}")

        products: list[tuple[Product, str, int]] = []
        for sku, prod_name, brand, category, hsn, uom, gst, supplier_cat, price in PRODUCTS:
            existing_product = (
                await session.execute(
                    select(Product).where(Product.org_id == org.id, Product.sku == sku)
                )
            ).scalar_one_or_none()
            if existing_product is None:
                existing_product = Product(
                    org_id=org.id,
                    sku=sku,
                    name=prod_name,
                    brand=brand,
                    category=category,
                    hsn_code=hsn,
                    uom=uom,
                    gst_rate=gst,
                )
                session.add(existing_product)
                await session.flush()
            products.append((existing_product, supplier_cat, price))
        print(f"products: {len(products)}")

        for product, supplier_cat, price in products:
            supplier = suppliers[CATEGORY_TO_SUPPLIER_IDX[supplier_cat]]
            existing_ps = (
                await session.execute(
                    select(ProductSupplier).where(
                        ProductSupplier.product_id == product.id,
                        ProductSupplier.supplier_id == supplier.id,
                    )
                )
            ).scalar_one_or_none()
            if existing_ps is None:
                session.add(
                    ProductSupplier(
                        product_id=product.id,
                        supplier_id=supplier.id,
                        unit_price=price,
                        moq=random.choice([5, 10, 20, 25, 50]),
                        lead_time_days=supplier.lead_time_days,
                        is_preferred=True,
                    )
                )

        for product, _supplier_cat, _price in products:
            existing_inv = (
                await session.execute(
                    select(InventoryItem).where(
                        InventoryItem.product_id == product.id,
                        InventoryItem.warehouse_id == warehouse.id,
                    )
                )
            ).scalar_one_or_none()
            if existing_inv is None:
                # WIRE-15 seeded to match the roadmap's worked example
                # exactly: 18 on hand, reorder level 30 — a sales order for
                # 40 immediately demonstrates the shortage/reorder flow.
                if product.sku == "WIRE-15":
                    on_hand, reorder_level, safety_stock = 18, 30, 10
                else:
                    on_hand = random.randint(15, 200)
                    reorder_level = round(on_hand * random.uniform(0.3, 0.6))
                    safety_stock = round(reorder_level * 0.4)
                session.add(
                    InventoryItem(
                        product_id=product.id,
                        warehouse_id=warehouse.id,
                        on_hand=on_hand,
                        reserved=0,
                        reorder_level=reorder_level,
                        safety_stock=safety_stock,
                    )
                )

        await session.commit()
        print("seed complete")


if __name__ == "__main__":
    asyncio.run(seed())
