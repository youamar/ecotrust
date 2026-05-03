"""
GHG Protocol Scope 2 calculation.

Scope 2 emissions = electricity consumed (kWh) * grid emission factor (kgCO2e/kWh)
TNB Malaysia 2024 grid emission factor: 0.585 kgCO2e/kWh.
"""
TNB_GRID_EF_2024 = 0.585  # kgCO2e per kWh


def scope2_emissions_kg(kwh: float, ef: float = TNB_GRID_EF_2024) -> float:
    return kwh * ef


def avoided_emissions_kg(baseline_kwh: float, actual_kwh: float, ef: float = TNB_GRID_EF_2024) -> float:
    return max(0.0, baseline_kwh - actual_kwh) * ef
