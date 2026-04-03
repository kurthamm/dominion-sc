"""Sensor platform for Dominion SC Energy."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CURRENCY_DOLLAR, EntityCategory, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    COORDINATOR,
    DOMAIN,
    TOTAL_ELECTRIC_COST,
    TOTAL_ELECTRIC_KWH,
    TOTAL_GAS_COST,
    TOTAL_GAS_FT3,
)
from .coordinator import DominionSCCoordinator

_LOGGER = logging.getLogger(__name__)


def _format_cycle_label(cycle_key: str) -> str:
    """Format a billing cycle key as YYYY-MMM (e.g., 2026-Jan).

    Accepts cycle keys in 'YYYY-MM-DD|YYYY-MM-DD' or ISO 'YYYY-MM-DD' format.
    Uses the start date of the cycle. Falls back to raw string on parse failure.
    """
    try:
        date_str = cycle_key.split("|")[0] if "|" in cycle_key else cycle_key
        dt = datetime.fromisoformat(date_str)
        return dt.strftime("%Y-%b")  # e.g., "2026-Jan"
    except (ValueError, TypeError, IndexError):
        _LOGGER.debug("Could not parse cycle key '%s', using raw value", cycle_key)
        return str(cycle_key)


def _format_billing_date(d: date) -> str:
    """Format a date as 'MMM D' (e.g., 'Mar 9'), no zero-padding."""
    return f"{d.strftime('%b')} {d.day}"


# ---------------------------------------------------------------------------
# Energy sensor descriptions (required for Energy Dashboard)
# ---------------------------------------------------------------------------

SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key=TOTAL_ELECTRIC_KWH,
        name="Electric Cumulative Consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:flash",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key=TOTAL_GAS_FT3,
        name="Gas Cumulative Consumption",
        native_unit_of_measurement=UnitOfVolume.CUBIC_FEET,
        icon="mdi:fire",
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key=TOTAL_ELECTRIC_COST,
        name="Electric Cumulative Cost",
        native_unit_of_measurement=CURRENCY_DOLLAR,
        icon="mdi:currency-usd",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
    ),
    SensorEntityDescription(
        key=TOTAL_GAS_COST,
        name="Gas Cumulative Cost",
        native_unit_of_measurement=CURRENCY_DOLLAR,
        icon="mdi:currency-usd",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator: DominionSCCoordinator = runtime[COORDINATOR]
    entities: list[SensorEntity] = [DominionSCTotalSensor(coordinator, entry, desc) for desc in SENSORS]
    entities.extend(
        [
            DominionSCBackfillCyclesSensor(coordinator, entry),
            DominionSCBackfillRemainingSensor(coordinator, entry),
            DominionSCCurrentBillingCycleSensor(coordinator, entry),
            DominionSCLastSyncSensor(coordinator, entry),
        ]
    )
    async_add_entities(entities)


class DominionSCTotalSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """Representation of a Dominion SC cumulative total sensor."""

    _attr_has_entity_name = True
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: DominionSCCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._key = description.key
        self._attr_name = description.name
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float:
        return round(float(self.coordinator.totals.get(self._key, 0.0)), 3)


class DominionSCBackfillCyclesSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """Total backfill cycle count with completed/incomplete detail in attributes.

    State = total number of backfill cycles (completed + incomplete).
    Attributes expose the full cycle lists formatted as YYYY-MMM.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:pound"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Backfill Cycles"
        self._attr_unique_id = f"{entry.entry_id}_backfill_cycles"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _backfill(self) -> dict[str, Any]:
        return self.coordinator.backfill

    @property
    def native_value(self) -> int:
        """Return total number of backfill cycles (completed + incomplete)."""
        completed = self._backfill.get("completed_cycles", [])
        missing = self._backfill.get("missing_cycles", [])
        return len(completed) + len(missing)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return completed/incomplete counts and cycle lists as YYYY-MMM."""
        raw_completed = self._backfill.get("completed_cycles", [])
        raw_missing = self._backfill.get("missing_cycles", [])
        completed_labels = [_format_cycle_label(c) for c in raw_completed]
        incomplete_labels = [_format_cycle_label(c) for c in raw_missing]
        return {
            "completed_count": len(completed_labels),
            "incomplete_count": len(incomplete_labels),
            "completed_cycles": completed_labels,
            "incomplete_cycles": incomplete_labels,
        }


class DominionSCBackfillRemainingSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """Sensor exposing the number of backfill cycles remaining (incomplete_count).

    This sensor's state is an integer count so it appears directly on the
    integration's device overview page.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:counter"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Backfill Cycles Remaining"
        self._attr_unique_id = f"{entry.entry_id}_backfill_cycles_remaining"
        # unit is a simple count of cycles
        self._attr_native_unit_of_measurement = "cycles"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> int | None:
        """Return count of incomplete (missing) backfill cycles or None if unavailable."""
        backfill = getattr(self.coordinator, "backfill", None)
        if not backfill:
            return None
        missing = backfill.get("missing_cycles")
        if isinstance(missing, list):
            return max(0, len(missing))
        # Fallback: try to compute from other attributes
        incomplete = backfill.get("incomplete_count")
        if isinstance(incomplete, int):
            return max(0, incomplete)
        return None


class DominionSCCurrentBillingCycleSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """Current billing cycle formatted as 'MMM D - MMM D' (e.g., 'Mar 9 - Apr 4')."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-range"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Current Billing Cycle"
        self._attr_unique_id = f"{entry.entry_id}_current_billing_cycle"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> str | None:
        """Return current billing cycle as 'MMM D - MMM D' (e.g., 'Mar 9 - Apr 4')."""
        cycle = self.coordinator.current_billing_cycle
        if cycle and cycle.start and cycle.end:
            return f"{_format_billing_date(cycle.start)} - {_format_billing_date(cycle.end)}"
        return None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return raw start/end ISO dates as attributes."""
        cycle = self.coordinator.current_billing_cycle
        return {
            "start": cycle.start.isoformat() if cycle else None,
            "end": cycle.end.isoformat() if cycle else None,
        }


class DominionSCLastSyncSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """Sensor exposing the last successful sync time for the integration."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-sync"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Last Sync"
        self._attr_unique_id = f"{entry.entry_id}_last_sync"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> datetime | None:
        """Return ISO 8601 timestamp of last successful sync, or None."""
        # Coordinator stores the last_sync as ISO8601 string (or None)
        return datetime.fromisoformat(self.coordinator.last_sync) if self.coordinator.last_sync else None
