"""
Setup for different kinds of Tuya climate devices
"""

import asyncio
import logging

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import (
    ATTR_CURRENT_HUMIDITY,
    ATTR_CURRENT_TEMPERATURE,
    ATTR_FAN_MODE,
    ATTR_HUMIDITY,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    ATTR_SWING_HORIZONTAL_MODE,
    ATTR_SWING_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    DEFAULT_MAX_HUMIDITY,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_HUMIDITY,
    DEFAULT_MIN_TEMP,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    PRECISION_TENTHS,
    PRECISION_WHOLE,
    UnitOfTemperature,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .device import TuyaLocalDevice
from .entity import TuyaLocalEntity, unit_from_ascii
from .helpers.config import async_tuya_setup_platform
from .helpers.device_config import TuyaEntityConfig

_LOGGER = logging.getLogger(__name__)

# Some fan coil devices (e.g. the Ideal Clima Ikaro) only keep a setpoint that
# is written while they are running: a setpoint set from Home Assistant while
# the unit is off is accepted by the DP and then discarded at the next power
# change, when the device reloads the setpoint it has stored (observed 7-12 s
# after switching on).  Send the requested setpoint again once the device has
# started, twice to cover the spread of the observed timing.
_REASSERT_SETPOINT_DELAYS = (15, 35)


async def async_setup_entry(hass, config_entry, async_add_entities):
    config = {**config_entry.data, **config_entry.options}
    await async_tuya_setup_platform(
        hass,
        async_add_entities,
        config,
        "climate",
        TuyaLocalClimate,
    )


def validate_temp_unit(unit):
    unit = unit_from_ascii(unit)
    try:
        return UnitOfTemperature(unit)
    except ValueError:
        if unit:
            _LOGGER.warning("%s is not a valid temperature unit", unit)


class TuyaLocalClimate(TuyaLocalEntity, ClimateEntity, RestoreEntity):
    """Representation of a Tuya Climate entity."""

    def __init__(self, device: TuyaLocalDevice, config: TuyaEntityConfig):
        """
        Initialise the climate device.
        Args:
           device (TuyaLocalDevice): The device API instance.
           config (TuyaEntityConfig): The entity config.
        """
        super().__init__()
        dps_map = self._init_begin(device, config)

        self._current_temperature_dps = dps_map.pop(
            ATTR_CURRENT_TEMPERATURE,
            None,
        )
        self._current_humidity_dps = dps_map.pop(ATTR_CURRENT_HUMIDITY, None)
        self._fan_mode_dps = dps_map.pop(ATTR_FAN_MODE, None)
        self._humidity_dps = dps_map.pop(ATTR_HUMIDITY, None)
        self._hvac_mode_dps = dps_map.pop(ATTR_HVAC_MODE, None)
        self._hvac_action_dps = dps_map.pop(ATTR_HVAC_ACTION, None)
        self._preset_mode_dps = dps_map.pop(ATTR_PRESET_MODE, None)
        self._swing_horizontal_mode_dps = dps_map.pop(
            ATTR_SWING_HORIZONTAL_MODE,
            None,
        )
        self._swing_mode_dps = dps_map.pop(ATTR_SWING_MODE, None)
        self._temperature_dps = dps_map.pop(ATTR_TEMPERATURE, None)
        self._temp_high_dps = dps_map.pop(ATTR_TARGET_TEMP_HIGH, None)
        self._temp_low_dps = dps_map.pop(ATTR_TARGET_TEMP_LOW, None)
        self._unit_dps = dps_map.pop("temperature_unit", None)
        self._mintemp_dps = dps_map.pop("min_temperature", None)
        self._maxtemp_dps = dps_map.pop("max_temperature", None)

        # Last temperature requested from HA, re-sent once the device runs
        # (see _schedule_setpoint_reassert).
        self._requested_temperature = None

        self._init_end(dps_map)

        if self._fan_mode_dps:
            self._attr_supported_features |= ClimateEntityFeature.FAN_MODE
        if self._humidity_dps:
            self._attr_supported_features |= ClimateEntityFeature.TARGET_HUMIDITY
        if self._preset_mode_dps:
            self._attr_supported_features |= ClimateEntityFeature.PRESET_MODE
        if self._swing_mode_dps:
            if self._swing_mode_dps.values(device):
                self._attr_supported_features |= ClimateEntityFeature.SWING_MODE
        if self._swing_horizontal_mode_dps:
            if self._swing_horizontal_mode_dps.values(device):
                self._attr_supported_features |= (
                    ClimateEntityFeature.SWING_HORIZONTAL_MODE
                )
        if self._temp_high_dps and self._temp_low_dps:
            self._attr_supported_features |= (
                ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
            )
        elif self._temperature_dps is not None:
            self._attr_supported_features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if HVACMode.OFF in self.hvac_modes:
            self._attr_supported_features |= ClimateEntityFeature.TURN_OFF
        if self._hvac_mode_dps and self._hvac_mode_dps.type is bool:
            self._attr_supported_features |= ClimateEntityFeature.TURN_ON

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # The setpoint requested from HA is remembered to be re-sent once the
        # device runs (see _schedule_setpoint_reassert); restore it across
        # restarts so it does not have to be set again.
        if self._temperature_dps is None or self._requested_temperature is not None:
            return
        state = await self.async_get_last_state()
        if state is None:
            return
        temperature = state.attributes.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        # Ignore a remembered setpoint that is outside the current range, e.g.
        # one saved before the device minimum was raised.
        if self.min_temp is not None and temperature < self.min_temp:
            return
        if self.max_temp is not None and temperature > self.max_temp:
            return
        self._requested_temperature = temperature

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        # If there is a separate DPS that returns the units, use that
        if self._unit_dps:
            unit = validate_temp_unit(self._unit_dps.get_value(self._device))
            # Only return valid units
            if unit:
                return unit
        # If there unit attribute configured in the temperature dps, use that
        if self._temperature_dps and self._temperature_dps.unit:
            unit = validate_temp_unit(self._temperature_dps.unit)
            if unit:
                return unit
        if self._temp_high_dps and self._temp_high_dps.unit:
            unit = validate_temp_unit(self._temp_high_dps.unit)
            if unit:
                return unit
        if self._temp_low_dps and self._temp_low_dps.unit:
            unit = validate_temp_unit(self._temp_low_dps.unit)
            if unit is not None:
                return unit
        if self._current_temperature_dps and self._current_temperature_dps.unit:
            unit = validate_temp_unit(self._current_temperature_dps.unit)
            if unit:
                return unit
        # Return the default unit
        return UnitOfTemperature.CELSIUS

    @property
    def precision(self):
        """Return the precision of the temperature setting."""
        # unlike sensor, this is a decimal of the smallest unit that can be
        # represented, not a number of decimal places.
        dp = self._temperature_dps or self._temp_high_dps
        temp = dp.scale(self._device) if dp else 1
        current = (
            self._current_temperature_dps.scale(self._device)
            if self._current_temperature_dps
            else 1
        )
        if max(temp, current) > 1.0:
            return PRECISION_TENTHS
        return PRECISION_WHOLE

    @property
    def target_temperature(self):
        """Return the currently set target temperature."""
        if self._temperature_dps is None:
            raise NotImplementedError()
        return self._temperature_dps.get_value(self._device)

    @property
    def target_temperature_high(self):
        """Return the currently set high target temperature."""
        if self._temp_high_dps is None:
            raise NotImplementedError()
        return self._temp_high_dps.get_value(self._device)

    @property
    def target_temperature_low(self):
        """Return the currently set low target temperature."""
        if self._temp_low_dps is None:
            raise NotImplementedError()
        return self._temp_low_dps.get_value(self._device)

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        dps = self._temperature_dps
        if dps is None:
            dps = self._temp_high_dps
        if dps is None:
            dps = self._temp_low_dps
        if dps is None:
            return 1
        return dps.step(self._device)

    @property
    def min_temp(self):
        """Return the minimum supported target temperature."""
        # if a separate min_temperature dps is specified, the device tells us.
        if self._mintemp_dps is not None:
            m = self._mintemp_dps.get_value(self._device)
            if m is not None:
                return m

        if self._temperature_dps is None:
            if self._temp_low_dps is None:
                return None
            r = self._temp_low_dps.range(self._device)
        else:
            r = self._temperature_dps.range(self._device)
        return DEFAULT_MIN_TEMP if r is None else r[0]

    @property
    def max_temp(self):
        """Return the maximum supported target temperature."""
        # if a separate max_temperature dps is specified, the device tells us.
        if self._maxtemp_dps is not None:
            m = self._maxtemp_dps.get_value(self._device)
            if m is not None:
                return m

        if self._temperature_dps is None:
            if self._temp_high_dps is None:
                return None
            r = self._temp_high_dps.range(self._device)
        else:
            r = self._temperature_dps.range(self._device)
        return DEFAULT_MAX_TEMP if r is None else r[1]

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        if kwargs.get(ATTR_PRESET_MODE) is not None:
            _LOGGER.info(
                "%s setting temperature: setting preset mode to %s",
                self._config.config_id,
                kwargs.get(ATTR_PRESET_MODE),
            )
            await self.async_set_preset_mode(kwargs.get(ATTR_PRESET_MODE))
        if kwargs.get(ATTR_TEMPERATURE) is not None:
            _LOGGER.info(
                "%s setting temperature to %s",
                self._config.config_id,
                kwargs.get(ATTR_TEMPERATURE),
            )
            self._requested_temperature = kwargs.get(ATTR_TEMPERATURE)
            await self.async_set_target_temperature(
                kwargs.get(ATTR_TEMPERATURE),
            )
        high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        if high is not None or low is not None:
            _LOGGER.info(
                "%s setting temperature range to %s - %s",
                self._config.config_id,
                low,
                high,
            )
            await self.async_set_target_temperature_range(low, high)

    async def async_set_target_temperature(self, target_temperature):
        if self._temperature_dps is None:
            raise NotImplementedError()

        await self._temperature_dps.async_set_value(
            self._device,
            target_temperature,
        )

    async def async_set_target_temperature_range(self, low, high):
        """Set the target temperature range."""
        dps_map = {}
        if low is not None and self._temp_low_dps is not None:
            dps_map.update(
                self._temp_low_dps.get_values_to_set(self._device, low, dps_map),
            )
        if high is not None and self._temp_high_dps is not None:
            dps_map.update(
                self._temp_high_dps.get_values_to_set(self._device, high, dps_map),
            )
        if dps_map:
            await self._device.async_set_properties(dps_map)

    @property
    def current_temperature(self):
        """Return the current measured temperature."""
        if self._current_temperature_dps:
            temp = self._current_temperature_dps.get_value(self._device)
            if self._current_temperature_dps.suggested_display_precision is not None:
                # Round the value to the suggested precision
                temp = round(
                    temp, self._current_temperature_dps.suggested_display_precision
                )
            return temp

    @property
    def target_humidity(self):
        """Return the currently set target humidity."""
        if self._humidity_dps is None:
            raise NotImplementedError()
        return self._humidity_dps.get_value(self._device)

    @property
    def min_humidity(self):
        """Return the minimum supported target humidity."""
        if self._humidity_dps is None:
            return None
        r = self._humidity_dps.range(self._device)
        return DEFAULT_MIN_HUMIDITY if r is None else r[0]

    @property
    def max_humidity(self):
        """Return the maximum supported target humidity."""
        if self._humidity_dps is None:
            return None
        r = self._humidity_dps.range(self._device)
        return DEFAULT_MAX_HUMIDITY if r is None else r[1]

    async def async_set_humidity(self, humidity: int):
        if self._humidity_dps is None:
            raise NotImplementedError()

        _LOGGER.info(
            "%s setting humidity to %s",
            self._config.config_id,
            humidity,
        )
        await self._humidity_dps.async_set_value(self._device, humidity)

    @property
    def current_humidity(self):
        """Return the current measured humidity."""
        if self._current_humidity_dps:
            return self._current_humidity_dps.get_value(self._device)

    @property
    def hvac_action(self):
        """Return the current HVAC action."""
        if self._hvac_action_dps is None:
            return None
        if self.hvac_mode is HVACMode.OFF:
            return HVACAction.OFF

        action = self._hvac_action_dps.get_value(self._device)
        try:
            return HVACAction(action) if action else None
        except ValueError:
            _LOGGER.warning(
                "%s/%s: Unrecognised HVAC Action %s ignored",
                self._config._device.config,
                self.name or "climate",
                action,
            )

    @property
    def hvac_mode(self):
        """Return current HVAC mode."""
        if self._hvac_mode_dps is None:
            return HVACMode.AUTO
        hvac_mode = self._hvac_mode_dps.get_value(self._device)
        try:
            return HVACMode(hvac_mode) if hvac_mode else None
        except ValueError:
            _LOGGER.warning(
                "%s/%s: Unrecognised HVAC Mode of %s ignored",
                self._config._device.config,
                self.name or "climate",
                hvac_mode,
            )

    @property
    def hvac_modes(self):
        """Return available HVAC modes."""
        if self._hvac_mode_dps is None:
            return [HVACMode.AUTO]
        else:
            return self._hvac_mode_dps.values(self._device)

    async def async_set_hvac_mode(self, hvac_mode):
        """Set new HVAC mode."""
        if self._hvac_mode_dps is None:
            raise NotImplementedError()
        _LOGGER.info(
            "%s setting HVAC mode to %s",
            self._config.config_id,
            hvac_mode,
        )
        was_off = self.hvac_mode == HVACMode.OFF
        await self._hvac_mode_dps.async_set_value(self._device, hvac_mode)
        if was_off and hvac_mode != HVACMode.OFF:
            self._schedule_setpoint_reassert()

    async def async_turn_on(self):
        """Turn on the climate device."""
        # Bypass the usual dps mapping to switch the power dp directly
        # this way the hvac_mode will be kept when toggling off and on.
        if self._hvac_mode_dps and self._hvac_mode_dps.type is bool:
            _LOGGER.info("%s turning on", self._config.config_id)
            await self._device.async_set_property(self._hvac_mode_dps.id, True)
            self._schedule_setpoint_reassert()
        else:
            await super().async_turn_on()

    async def async_turn_off(self):
        """Turn off the climate device."""
        # Bypass the usual dps mapping to switch the power dp directly
        # this way the hvac_mode will be kept when toggling off and on.
        if self._hvac_mode_dps and self._hvac_mode_dps.type is bool:
            _LOGGER.info("%s turning off", self._config.config_id)
            await self._device.async_set_property(
                self._hvac_mode_dps.id,
                False,
            )
        else:
            await super().async_turn_off()

    def _schedule_setpoint_reassert(self):
        """Re-send the setpoint HA asked for, once the device runs.

        Fan coil devices like the Ideal Clima Ikaro only store a setpoint
        written while they are running: one set while the unit is off is
        accepted by the DP and then discarded when the device reloads its
        stored setpoint on start-up.  Send it again after it has settled.
        """
        if self._temperature_dps is None or self._requested_temperature is None:
            return
        for delay in _REASSERT_SETPOINT_DELAYS:
            self._device._hass.async_create_task(
                self._reassert_target_temperature(delay)
            )

    async def _reassert_target_temperature(self, delay):
        """Re-send the setpoint the device may have discarded on start-up."""
        await asyncio.sleep(delay)
        temperature = self._requested_temperature
        if temperature is None:
            return
        if self.hvac_mode == HVACMode.OFF:
            # Nothing to do while the device is off: a setpoint written when
            # it is not running would not be stored anyway.
            return
        if self.target_temperature == temperature:
            # The device already has the requested setpoint, nothing to do.
            return
        _LOGGER.info(
            "%s re-sending temperature %s after start-up",
            self._config.config_id,
            temperature,
        )
        await self.async_set_target_temperature(temperature)

    @property
    def preset_mode(self):
        """Return the current preset mode."""
        if self._preset_mode_dps is None:
            raise NotImplementedError()

        return self._preset_mode_dps.get_value(self._device)

    @property
    def preset_modes(self):
        """Return the list of presets that this device supports."""
        if self._preset_mode_dps:
            return self._preset_mode_dps.values(self._device)

    async def async_set_preset_mode(self, preset_mode):
        """Set the preset mode."""
        if self._preset_mode_dps is None:
            raise NotImplementedError()
        _LOGGER.info(
            "%s setting preset mode to %s",
            self._config.config_id,
            preset_mode,
        )
        await self._preset_mode_dps.async_set_value(self._device, preset_mode)

    @property
    def swing_mode(self):
        """Return the current swing mode."""
        if self._swing_mode_dps is None:
            raise NotImplementedError()
        return self._swing_mode_dps.get_value(self._device)

    @property
    def swing_modes(self):
        """Return the list of swing modes that this device supports."""
        if self._swing_mode_dps:
            return self._swing_mode_dps.values(self._device)

    async def async_set_swing_mode(self, swing_mode):
        """Set the preset mode."""
        if self._swing_mode_dps is None:
            raise NotImplementedError()
        _LOGGER.info(
            "%s setting swing mode to %s",
            self._config.config_id,
            swing_mode,
        )
        await self._swing_mode_dps.async_set_value(self._device, swing_mode)

    @property
    def swing_horizontal_mode(self):
        """Return the current horizontal swing mode."""
        if self._swing_horizontal_mode_dps is None:
            raise NotImplementedError()
        return self._swing_horizontal_mode_dps.get_value(self._device)

    @property
    def swing_horizontal_modes(self):
        """Return the list of swing modes that this device supports."""
        if self._swing_horizontal_mode_dps:
            return self._swing_horizontal_mode_dps.values(self._device)

    async def async_set_swing_horizontal_mode(self, swing_mode):
        """Set the preset mode."""
        if self._swing_horizontal_mode_dps is None:
            raise NotImplementedError()
        _LOGGER.info(
            "%s setting horizontal swing mode to %s",
            self._config.config_id,
            swing_mode,
        )
        await self._swing_horizontal_mode_dps.async_set_value(
            self._device,
            swing_mode,
        )

    @property
    def fan_mode(self):
        """Return the current fan mode."""
        if self._fan_mode_dps is None:
            raise NotImplementedError()
        return self._fan_mode_dps.get_value(self._device)

    @property
    def fan_modes(self):
        """Return the list of fan modes that this device supports."""
        if self._fan_mode_dps:
            return self._fan_mode_dps.values(self._device)

    async def async_set_fan_mode(self, fan_mode):
        """Set the fan mode."""
        if self._fan_mode_dps is None:
            raise NotImplementedError()
        _LOGGER.info(
            "%s setting fan mode to %s",
            self._config.config_id,
            fan_mode,
        )
        await self._fan_mode_dps.async_set_value(self._device, fan_mode)
