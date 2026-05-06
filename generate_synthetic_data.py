"""
generate_synthetic_data.py
--------------------------
Generates synthetic data for all 4 department systems.
Produces ~300 real-world businesses across 2 Bengaluru pin codes (560058, 560100),
each appearing in 1–4 of the 4 department systems.

Key design goals:
- Introduce realistic name variation, address variation, and identifier sparsity
- Create intentional hard duplicates (same business, different spellings)
- Create intentional negatives (different businesses with similar names)
- Generate 12 months of activity events with realistic patterns

Run:
    python generate_synthetic_data.py
    -> writes 4 CSV files + 1 events CSV to ./synthetic_data/
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
import re

random.seed(42)  # deterministic

OUT_DIR = Path("synthetic_data")
OUT_DIR.mkdir(exist_ok=True)

PIN_CODES = ["560058", "560100"]

# ─────────────────────────────────────────────────────────────
# Name building blocks
# ─────────────────────────────────────────────────────────────

FIRST_WORDS = [
    "Shree", "Sri", "Lakshmi", "Mahalaxmi", "Balaji", "Venkateshwara",
    "Karnataka", "Bengaluru", "Bangalore", "KS", "JSW", "Titan",
    "Infosys", "Wipro", "Mphasis", "Global", "National", "Modern",
    "Premier", "Sunrise", "Srinivasa", "Raghavendra", "New", "Star",
    "Royal", "Janatha", "Peoples", "United", "Indian", "Southern",
]

INDUSTRY_WORDS = [
    "Textiles", "Garments", "Fabrics", "Engineering", "Electricals",
    "Electronics", "Foods", "Beverages", "Chemicals", "Pharmaceuticals",
    "Plastics", "Packaging", "Steel", "Metals", "Foundry", "Auto Parts",
    "Furniture", "Printing", "Paper", "Rubber", "Leather", "Ceramics",
    "Paints", "Pipes", "Cables", "Motors", "Pumps", "Valves",
]

SUFFIXES = [
    "Industries", "Pvt Ltd", "Private Limited", "Ltd", "Limited",
    "Enterprises", "Traders", "Manufacturers", "Works", "Factory",
    "Co", "& Co", "Group", "Corporation", "International",
]

ABBREVIATION_VARIANTS = {
    "Pvt Ltd":        ["Pvt. Ltd.", "Private Limited", "P Ltd", "Pvt.Ltd"],
    "Private Limited":["Pvt Ltd", "Pvt. Ltd.", "P Limited"],
    "Industries":     ["Indus", "Inds", "Ind"],
    "Enterprises":    ["Ent", "Entp", "Enterp"],
    "Manufacturers":  ["Mfrs", "Mfg", "Mfr"],
    "Engineering":    ["Engg", "Engg.", "Eng"],
}

LOCALITIES_560058 = [
    "Peenya Industrial Area", "Peenya", "Rajajinagar Industrial Estate",
    "BEL Road", "Yeshwanthpur", "Peenya 2nd Stage", "Peenya 1st Stage",
]
LOCALITIES_560100 = [
    "Bommanahalli", "Electronic City Phase 1", "Electronic City Phase 2",
    "Hosur Road", "Neeladri Nagar", "Kudlu Gate",
]

STREETS = [
    "Main Road", "Cross Road", "Industrial Road", "Factory Road",
    "1st Cross", "2nd Cross", "3rd Main", "Ring Road", "Service Road",
]

SECTORS = [
    "TEXTILE", "ENGINEERING", "FOOD_PROCESSING", "ELECTRONICS",
    "CHEMICALS", "AUTO_COMPONENTS", "PACKAGING", "PHARMACEUTICALS",
    "RUBBER_PLASTICS", "PAPER_PRINTING",
]

NIC_CODES = {
    "TEXTILE": "1311",
    "ENGINEERING": "2819",
    "FOOD_PROCESSING": "1079",
    "ELECTRONICS": "2630",
    "CHEMICALS": "2011",
    "AUTO_COMPONENTS": "2930",
    "PACKAGING": "1709",
    "PHARMACEUTICALS": "2100",
    "RUBBER_PLASTICS": "2219",
    "PAPER_PRINTING": "1701",
}

TRADE_CATEGORIES = {
    "TEXTILE": "GARMENT",
    "ENGINEERING": "FACTORY",
    "FOOD_PROCESSING": "FOOD",
    "ELECTRONICS": "FACTORY",
    "CHEMICALS": "CHEMICAL",
    "AUTO_COMPONENTS": "FACTORY",
    "PACKAGING": "FACTORY",
    "PHARMACEUTICALS": "PHARMA",
    "RUBBER_PLASTICS": "FACTORY",
    "PAPER_PRINTING": "PRINTING",
}

# ─────────────────────────────────────────────────────────────
# Identifier generation
# ─────────────────────────────────────────────────────────────

def _pan_checksum_char(pan9: str) -> str:
    """PAN has a specific check character — we fake it plausibly."""
    return random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def gen_pan() -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    pan_type = random.choice(["P", "C", "H", "F", "A"])  # P=individual, C=company
    p1 = "".join(random.choices(letters, k=3))
    p2 = pan_type
    p3 = random.choice(letters)
    digits = "".join(random.choices("0123456789", k=4))
    check = random.choice(letters)
    return f"{p1}{p2}{p3}{digits}{check}"


def gen_gstin(pan: str, state_code: str = "29") -> str:
    """GSTIN = state_code(2) + PAN(10) + entity(1) + Z + checksum(1)."""
    entity = random.choice("123456789")
    check = random.choice("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    return f"{state_code}{pan}{entity}Z{check}"


def scramble_pan(pan: str, seed: int) -> str:
    """Deterministically scramble a PAN while keeping format valid."""
    r = random.Random(seed)
    chars = list(pan)
    for i in [5, 6, 7, 8]:  # scramble the digit portion
        chars[i] = r.choice("0123456789")
    return "".join(chars)


def scramble_name(name: str, seed: int) -> str:
    """Deterministically return a name-length-preserving scramble."""
    r = random.Random(seed)
    # Just rotate a few characters — keeps the feel
    chars = list(name)
    positions = [i for i, c in enumerate(chars) if c.isalpha()]
    if len(positions) > 3:
        swap_count = max(1, len(positions) // 5)
        for _ in range(swap_count):
            i, j = r.sample(positions, 2)
            chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


# ─────────────────────────────────────────────────────────────
# Business entity class
# ─────────────────────────────────────────────────────────────

class Business:
    """Represents a real-world business entity before it gets split across systems."""

    def __init__(self, idx: int):
        self.idx = idx
        self.pin_code = random.choice(PIN_CODES)
        self.sector = random.choice(SECTORS)

        # Canonical identity
        fw = random.choice(FIRST_WORDS)
        iw = random.choice(INDUSTRY_WORDS)
        sf = random.choice(SUFFIXES)
        self.canonical_name = f"{fw} {iw} {sf}"
        self.reg_year = random.randint(1990, 2022)

        locality_pool = LOCALITIES_560058 if self.pin_code == "560058" else LOCALITIES_560100
        self.locality = random.choice(locality_pool)
        self.street = random.choice(STREETS)
        self.building_no = f"{random.randint(1, 999)}/{random.randint(1, 9)}"
        self.canonical_address = f"{self.building_no}, {self.street}, {self.locality}, Bengaluru"

        # Identifiers — not all businesses have all identifiers
        self.has_pan = random.random() > 0.15       # 85% coverage
        self.pan = gen_pan() if self.has_pan else None
        self.has_gstin = self.has_pan and random.random() > 0.25  # 75% of PAN holders
        self.gstin = gen_gstin(self.pan) if (self.has_pan and self.has_gstin) else None

        # Derive a CIN for about 30% of businesses
        self.cin = f"U{NIC_CODES.get(self.sector,'9999')}KA{self.reg_year}PTC{random.randint(100000,999999)}" \
                   if random.random() > 0.7 else None

        self.phone = f"9{random.randint(100000000, 999999999)}"
        self.email = None  # sparse
        self.num_employees = random.randint(5, 800)
        self.power_kw = round(random.uniform(5, 500), 1)

        # Which sources this business appears in (1–4 of the 4 systems)
        # All factories appear in FACTORIES; ~70% in BBMP; ~85% in ESCOM; ~60% in LABOUR
        sources = []
        if random.random() > 0.3: sources.append("BBMP")
        if random.random() > 0.15: sources.append("ESCOM")
        if random.random() > 0.4: sources.append("LABOUR")
        if self.sector in ("ENGINEERING", "AUTO_COMPONENTS", "CHEMICALS", "PHARMACEUTICALS"):
            if random.random() > 0.3: sources.append("FACTORIES")
        if not sources:
            sources = ["BBMP"]   # at minimum one source
        self.sources = sources

        # Business status for activity generation
        p = random.random()
        if p > 0.25:
            self.real_status = "ACTIVE"
        elif p > 0.1:
            self.real_status = "DORMANT"
        else:
            self.real_status = "CLOSED"

        self.closure_date = None
        if self.real_status == "CLOSED":
            self.closure_date = date.today() - timedelta(days=random.randint(100, 800))

    def get_name_variant(self, variant_seed: Optional[int] = None) -> str:
        """Return a name variant (abbreviations, typos) for a particular source."""
        if variant_seed is None:
            return self.canonical_name
        r = random.Random(variant_seed)
        name = self.canonical_name

        # Expand/contract abbreviations
        for full, abbrevs in ABBREVIATION_VARIANTS.items():
            if full in name and r.random() > 0.5:
                name = name.replace(full, r.choice(abbrevs), 1)

        # Occasional word-order swap
        parts = name.split()
        if len(parts) >= 3 and r.random() > 0.7:
            parts[0], parts[1] = parts[1], parts[0]
            name = " ".join(parts)

        return name

    def get_address_variant(self, variant_seed: Optional[int] = None) -> str:
        """Return an address variant (partial, different format) for a particular source."""
        if variant_seed is None:
            return self.canonical_address
        r = random.Random(variant_seed)
        parts = [self.building_no, self.street, self.locality, "Bengaluru", self.pin_code]
        # Randomly drop or abbreviate some parts
        if r.random() > 0.5:
            parts = parts[1:]  # drop building number
        if r.random() > 0.6:
            parts[-1] = "B'lore"
        return ", ".join(parts)


# ─────────────────────────────────────────────────────────────
# Record generators per department
# ─────────────────────────────────────────────────────────────

def make_bbmp_record(b: Business, i: int) -> dict:
    r = random.Random(b.idx * 100 + 10)
    name = b.get_name_variant(b.idx * 100 + 11)
    addr = b.get_address_variant(b.idx * 100 + 12)

    issue_date = date(b.reg_year, r.randint(1, 12), r.randint(1, 28))
    expiry_date = date(date.today().year + 1, issue_date.month, issue_date.day)
    status = "ACTIVE" if b.real_status == "ACTIVE" else \
             "SUSPENDED" if b.real_status == "DORMANT" else "CANCELLED"

    return {
        "trade_licence_no": f"BBMP/{b.pin_code}/{b.idx:04d}",
        "business_name":    name,
        "proprietor_name":  f"{random.choice(['Rajesh', 'Suresh', 'Ramesh', 'Priya', 'Anitha'])} "
                            f"{random.choice(['Kumar', 'Sharma', 'Reddy', 'Nair', 'Rao'])}",
        "trade_category":   TRADE_CATEGORIES.get(b.sector, "GENERAL"),
        "business_address": addr,
        "ward_name":        b.locality,
        "ward_no":          str(r.randint(1, 198)),
        "pin_code":         b.pin_code,
        "pan":              b.pan,
        "gstin":            b.gstin if r.random() > 0.2 else None,   # GSTIN sparser in BBMP
        "mobile":           b.phone if r.random() > 0.3 else None,
        "email":            None,
        "licence_issue_date": str(issue_date),
        "licence_expiry_date": str(expiry_date),
        "licence_status":   status,
        "num_employees":    b.num_employees,
        "annual_turnover":  round(r.uniform(5, 500) * 100000, 0),
    }


def make_escom_record(b: Business, i: int) -> dict:
    r = random.Random(b.idx * 100 + 20)
    name = b.get_name_variant(b.idx * 100 + 21)
    addr = b.get_address_variant(b.idx * 100 + 22)

    conn_type = "INDUSTRIAL" if b.num_employees > 50 else "COMMERCIAL"
    status = "ACTIVE" if b.real_status == "ACTIVE" else \
             "DISCONNECTED" if b.real_status == "DORMANT" else "SURRENDERED"

    return {
        "consumer_no":          f"ESCOM/{b.pin_code}/{b.idx:06d}",
        "consumer_name":        name,
        "service_address":      addr,
        "pin_code":             b.pin_code,
        "connection_type":      conn_type,
        "sanctioned_load_kw":   b.power_kw,
        "connected_load_kw":    round(b.power_kw * r.uniform(0.6, 0.95), 1),
        "tariff_category":      f"LT-{r.randint(1, 6)}",
        "pan":                  b.pan,
        "gstin":                b.gstin,
        "connection_date":      str(date(b.reg_year, r.randint(1, 12), r.randint(1, 28))),
        "connection_status":    status,
        "meter_no":             f"M{r.randint(10000000, 99999999)}",
        "mobile":               b.phone if r.random() > 0.4 else None,
    }


def make_labour_record(b: Business, i: int) -> dict:
    r = random.Random(b.idx * 100 + 30)
    name = b.get_name_variant(b.idx * 100 + 31)
    addr = b.get_address_variant(b.idx * 100 + 32)

    return {
        "pf_code":              f"KR/BN/{b.idx:05d}/000",
        "establishment_name":   name,
        "employer_name":        f"{r.choice(['M/s', 'Shri', 'Smt'])} {name}",
        "industry_class":       b.sector.replace("_", " ").title(),
        "address":              addr,
        "district":             "Bengaluru Urban",
        "pin_code":             b.pin_code,
        "pan":                  b.pan,
        "gstin":                b.gstin if r.random() > 0.35 else None,
        "esi_code":             f"ESI{b.idx:07d}" if r.random() > 0.4 else None,
        "coverage_date":        str(date(b.reg_year + 1, r.randint(1, 12), r.randint(1, 28))),
        "num_employees":        b.num_employees,
        "wages_month":          round(b.num_employees * r.uniform(8000, 25000), 0),
        "mobile":               b.phone if r.random() > 0.5 else None,
        "email":                None,
        "nic_code":             NIC_CODES.get(b.sector, "9999"),
    }


def make_factories_record(b: Business, i: int) -> dict:
    r = random.Random(b.idx * 100 + 40)
    name = b.get_name_variant(b.idx * 100 + 41)
    addr = b.get_address_variant(b.idx * 100 + 42)

    issue = date(b.reg_year, r.randint(1, 12), r.randint(1, 28))
    expiry = date(date.today().year + 1, issue.month, issue.day)

    # Last inspection: active plants have recent inspections
    if b.real_status == "ACTIVE":
        last_insp = date.today() - timedelta(days=r.randint(30, 540))
    elif b.real_status == "DORMANT":
        last_insp = date.today() - timedelta(days=r.randint(400, 900))
    else:
        last_insp = None

    return {
        "factory_licence_no":       f"KAR/FACT/{b.pin_code}/{b.idx:04d}",
        "factory_name":             name,
        "occupier_name":            f"M/s {name}",
        "manager_name":             f"{r.choice(['R', 'S', 'A', 'V'])}. "
                                    f"{r.choice(['Kumar', 'Sharma', 'Reddy'])}",
        "product_description":      f"{b.sector.replace('_', ' ').title()} products",
        "factory_address":          addr,
        "pin_code":                 b.pin_code,
        "district":                 "Bengaluru Urban",
        "pan":                      b.pan,
        "gstin":                    b.gstin,
        "cin":                      b.cin,
        "licence_issue_date":       str(issue),
        "licence_valid_upto":       str(expiry),
        "licence_status":           "VALID" if b.real_status == "ACTIVE" else
                                    "LAPSED" if b.real_status == "DORMANT" else "CANCELLED",
        "num_workers":              b.num_employees,
        "power_used":               True,
        "installed_capacity_hp":    round(b.power_kw * 1.341, 1),
        "nic_code":                 NIC_CODES.get(b.sector, "9999"),
        "last_inspection_date":     str(last_insp) if last_insp else None,
        "inspection_result":        r.choice(["SATISFACTORY", "NOTICE_ISSUED", "SATISFACTORY"])
                                    if last_insp else None,
        "mobile":                   b.phone if r.random() > 0.4 else None,
    }


# ─────────────────────────────────────────────────────────────
# Activity event generator
# ─────────────────────────────────────────────────────────────

def generate_events(businesses: list[Business]) -> list[dict]:
    """Generate 12 months of activity events for all businesses."""
    events = []
    today = date.today()
    year_ago = today - timedelta(days=365)

    for b in businesses:
        source_record_ids = {
            "BBMP":      f"BBMP/{b.pin_code}/{b.idx:04d}",
            "ESCOM":     f"ESCOM/{b.pin_code}/{b.idx:06d}",
            "LABOUR":    f"KR/BN/{b.idx:05d}/000",
            "FACTORIES": f"KAR/FACT/{b.pin_code}/{b.idx:04d}",
        }

        # Terminal event if closed
        if b.real_status == "CLOSED" and b.closure_date:
            if "BBMP" in b.sources:
                events.append({
                    "source":           "BBMP",
                    "source_record_id": source_record_ids["BBMP"],
                    "event_category":   "LICENCE_SURRENDER",
                    "event_date":       str(b.closure_date),
                    "is_terminal":      True,
                    "is_high_reliability": False,
                    "payload":          json.dumps({"reason": "Business closed"}),
                })

        r = random.Random(b.idx * 7)
        current = year_ago

        # Monthly electricity readings
        if "ESCOM" in b.sources:
            d = year_ago
            while d <= today:
                if b.real_status == "ACTIVE":
                    consumption = round(b.power_kw * r.uniform(150, 200), 1)
                elif b.real_status == "DORMANT" and d < (today - timedelta(days=400)):
                    consumption = round(b.power_kw * r.uniform(5, 30), 1)
                elif b.real_status == "CLOSED" and (b.closure_date and d < b.closure_date):
                    consumption = round(b.power_kw * r.uniform(150, 200), 1)
                else:
                    d += timedelta(days=30)
                    continue  # no reading for closed/dormant after closure

                events.append({
                    "source":           "ESCOM",
                    "source_record_id": source_record_ids["ESCOM"],
                    "event_category":   "ELECTRICITY_READING",
                    "event_date":       str(d),
                    "is_terminal":      False,
                    "is_high_reliability": True,
                    "payload":          json.dumps({"consumption_kwh": consumption}),
                })
                d += timedelta(days=30)

        # Annual licence renewal
        if "BBMP" in b.sources and b.real_status == "ACTIVE":
            events.append({
                "source":           "BBMP",
                "source_record_id": source_record_ids["BBMP"],
                "event_category":   "LICENCE_RENEWAL",
                "event_date":       str(today - timedelta(days=r.randint(1, 180))),
                "is_terminal":      False,
                "is_high_reliability": False,
                "payload":          json.dumps({"fee_paid": round(r.uniform(500, 5000), 0)}),
            })

        # Quarterly PF filings
        if "LABOUR" in b.sources and b.real_status in ("ACTIVE", "DORMANT"):
            for q in range(4):
                q_date = year_ago + timedelta(days=90 * q)
                if b.real_status == "DORMANT" and q >= 2:
                    continue  # dormant businesses miss later filings
                events.append({
                    "source":           "LABOUR",
                    "source_record_id": source_record_ids["LABOUR"],
                    "event_category":   "PF_FILING",
                    "event_date":       str(q_date + timedelta(days=r.randint(0, 15))),
                    "is_terminal":      False,
                    "is_high_reliability": False,
                    "payload":          json.dumps({"employees": b.num_employees,
                                                    "wages": round(b.num_employees * 15000, 0)}),
                })

        # Inspections (irregular)
        if "FACTORIES" in b.sources and b.real_status == "ACTIVE":
            if r.random() > 0.5:  # ~50% chance of inspection in last 12 months
                insp_date = today - timedelta(days=r.randint(30, 350))
                events.append({
                    "source":           "FACTORIES",
                    "source_record_id": source_record_ids["FACTORIES"],
                    "event_category":   "INSPECTION",
                    "event_date":       str(insp_date),
                    "is_terminal":      False,
                    "is_high_reliability": False,
                    "payload":          json.dumps({
                        "result": r.choice(["SATISFACTORY", "NOTICE_ISSUED"]),
                        "inspector_id": f"INS{r.randint(100, 999)}",
                    }),
                })

    return events


# ─────────────────────────────────────────────────────────────
# Main: generate and write all files
# ─────────────────────────────────────────────────────────────

def write_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"  ✓ {path.name}: {len(rows)} rows")


def main() -> None:
    print("Generating synthetic businesses...")
    NUM_BUSINESSES = 300  # real-world entities
    businesses = [Business(i) for i in range(1, NUM_BUSINESSES + 1)]

    # Introduce ~20 intentional near-duplicate businesses (similar names, same address)
    # These are TRUE negatives — different legal entities, confusable names
    print("  Adding confusable near-duplicates...")
    for i in range(20):
        base = businesses[i * 10]
        twin = Business(NUM_BUSINESSES + i + 1)
        twin.pin_code = base.pin_code
        twin.locality = base.locality
        # Same locality, intentionally similar first word
        twin.canonical_name = f"{base.canonical_name.split()[0]} {random.choice(INDUSTRY_WORDS)} {random.choice(SUFFIXES)}"
        twin.sources = random.sample(["BBMP", "ESCOM"], k=1)
        businesses.append(twin)

    # Separate by source
    bbmp_rows     = [make_bbmp_record(b, b.idx) for b in businesses if "BBMP" in b.sources]
    escom_rows    = [make_escom_record(b, b.idx) for b in businesses if "ESCOM" in b.sources]
    labour_rows   = [make_labour_record(b, b.idx) for b in businesses if "LABOUR" in b.sources]
    factories_rows= [make_factories_record(b, b.idx) for b in businesses if "FACTORIES" in b.sources]

    print("\nWriting CSVs...")
    write_csv(bbmp_rows,      OUT_DIR / "bbmp_trade_licences.csv")
    write_csv(escom_rows,     OUT_DIR / "escom_connections.csv")
    write_csv(labour_rows,    OUT_DIR / "labour_establishments.csv")
    write_csv(factories_rows, OUT_DIR / "factories_licences.csv")

    print("\nGenerating activity events...")
    events = generate_events(businesses)
    write_csv(events, OUT_DIR / "activity_events.csv")

    # Write a ground truth file for evaluation (which businesses are the same)
    print("\nWriting ground truth...")
    ground_truth = []
    for b in businesses:
        row = {
            "real_business_idx": b.idx,
            "real_status":       b.real_status,
            "pin_code":          b.pin_code,
            "sector":            b.sector,
            "canonical_name":    b.canonical_name,
            "pan":               b.pan,
            "gstin":             b.gstin,
        }
        # One row per (business, source) pair
        for src in b.sources:
            src_id = {
                "BBMP":      f"BBMP/{b.pin_code}/{b.idx:04d}",
                "ESCOM":     f"ESCOM/{b.pin_code}/{b.idx:06d}",
                "LABOUR":    f"KR/BN/{b.idx:05d}/000",
                "FACTORIES": f"KAR/FACT/{b.pin_code}/{b.idx:04d}",
            }[src]
            ground_truth.append({**row, "source": src, "source_record_id": src_id})

    write_csv(ground_truth, OUT_DIR / "ground_truth.csv")

    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Synthetic data generation complete.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total businesses (real entities): {len(businesses)}
  BBMP records:      {len(bbmp_rows)}
  ESCOM records:     {len(escom_rows)}
  Labour records:    {len(labour_rows)}
  Factories records: {len(factories_rows)}
  Activity events:   {len(events)}
Pin codes: 560058, 560100
Ground truth rows:   {len(ground_truth)}

Files written to ./synthetic_data/
""")


if __name__ == "__main__":
    main()
