"""
TNB Tariff C (Medium-Voltage General Commercial) — bill projection.

Reference: Tenaga Nasional Berhad 2025 schedule.
  - Energy charge:        RM 0.365/kWh  (kWh consumed)
  - Maximum Demand (MD):  RM 35.50/kW   (peak kW within billing month)
  - Capacity charge:      RM 16.20/kW   (contracted demand, simplified flat)
  - 1.6% Kumpulan Wang Tenaga Boleh Baharu (KWBB) levy on energy charge
  - 8% Service Tax on the post-rebate total
  - ICPT surcharge: omitted (varies; set to 0 for hackathon)

This is a hackathon-grade approximation that captures the *shape* of the
bill — peak kW dominates, kWh is secondary — which is the point of EcoTrust.
"""
from dataclasses import dataclass

ENERGY_RATE_RM_PER_KWH = 0.365
MD_RATE_RM_PER_KW = 35.50
CAPACITY_RATE_RM_PER_KW = 16.20
KWBB_LEVY = 0.016
SERVICE_TAX = 0.08


@dataclass
class BillBreakdown:
    energy_kwh: float
    energy_rm: float
    peak_kw: float
    md_rm: float
    capacity_rm: float
    kwbb_rm: float
    service_tax_rm: float
    total_rm: float


def project_monthly_bill(total_kwh_30d: float, peak_kw: float,
                         contracted_demand_kw: float | None = None) -> BillBreakdown:
    """Project a monthly TNB Tariff C bill from 30-day kWh and peak kW."""
    energy_rm = total_kwh_30d * ENERGY_RATE_RM_PER_KWH
    md_rm = peak_kw * MD_RATE_RM_PER_KW
    cap_rm = (contracted_demand_kw or peak_kw) * CAPACITY_RATE_RM_PER_KW
    kwbb_rm = energy_rm * KWBB_LEVY
    subtotal = energy_rm + md_rm + cap_rm + kwbb_rm
    tax_rm = subtotal * SERVICE_TAX
    return BillBreakdown(
        energy_kwh=round(total_kwh_30d, 2),
        energy_rm=round(energy_rm, 2),
        peak_kw=round(peak_kw, 2),
        md_rm=round(md_rm, 2),
        capacity_rm=round(cap_rm, 2),
        kwbb_rm=round(kwbb_rm, 2),
        service_tax_rm=round(tax_rm, 2),
        total_rm=round(subtotal + tax_rm, 2),
    )


def extrapolate_30d(kwh_24h: float) -> float:
    return kwh_24h * 30.0
