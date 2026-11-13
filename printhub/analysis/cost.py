"""Калькулятор себестоимости печати."""
from __future__ import annotations

from typing import Any, Optional

# Средняя потребляемая мощность, Вт (по данным производителей и замерам сообщества)
DEFAULT_POWER_W = {
    "fdm": 150.0,
    "resin": 60.0,
}


def calc_cost(*, technology: str, time_s: float, material_g: Optional[float] = None,
              material_ml: Optional[float] = None, material: Optional[dict[str, Any]] = None,
              settings: Optional[dict[str, Any]] = None, power_w: float = 0.0,
              quantity: int = 1) -> dict[str, Any]:
    """Возвращает разбивку стоимости. Цена материала — за кг (и для пластика, и для смолы)."""
    s = settings or {}
    m = material or {}
    density = float(m.get("density") or (1.10 if technology == "resin" else 1.24))
    if material_g is None and material_ml is not None:
        material_g = material_ml * density
    if material_ml is None and material_g is not None and technology == "resin":
        material_ml = material_g / density
    grams = float(material_g or 0.0)
    price_kg = float(m.get("price") or 0.0)
    hours = max(time_s, 0.0) / 3600.0
    power = power_w or DEFAULT_POWER_W.get(technology, 120.0)

    material_cost = grams / 1000.0 * price_kg
    electricity = power / 1000.0 * hours * float(s.get("electricity_price", 0) or 0)
    depreciation = hours * float(s.get("depreciation_per_hour", 0) or 0)
    labor = float(s.get("labor_per_job", 0) or 0)
    base = material_cost + electricity + depreciation
    failure = base * float(s.get("failure_pct", 0) or 0) / 100.0
    subtotal = base + failure + labor
    markup = subtotal * float(s.get("markup_pct", 0) or 0) / 100.0
    total = subtotal + markup
    q = max(int(quantity or 1), 1)
    return {
        "currency": s.get("currency", "₽"),
        "hours": round(hours, 2),
        "material_g": round(grams, 2),
        "material_ml": round(material_ml, 2) if material_ml is not None else None,
        "material_cost": round(material_cost, 2),
        "electricity_kwh": round(power / 1000.0 * hours, 3),
        "electricity_cost": round(electricity, 2),
        "depreciation_cost": round(depreciation, 2),
        "failure_reserve": round(failure, 2),
        "labor_cost": round(labor, 2),
        "markup": round(markup, 2),
        "total": round(total, 2),
        "quantity": q,
        "total_for_quantity": round(total * q, 2),
        "per_item": round(total, 2),
    }
