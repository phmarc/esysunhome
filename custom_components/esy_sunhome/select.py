import asyncio
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.exceptions import HomeAssistantError

from .battery import BatteryState
from .entity import EsySunhomeEntity
from .const import (
    ATTR_SCHEDULE_MODE,
    CONF_MODE_CHANGE_METHOD,
    MODE_CHANGE_API,
    MODE_CHANGE_MQTT,
    DEFAULT_MODE_CHANGE_METHOD,
)

_LOGGER = logging.getLogger(__name__)

# Configuration for retries and timeouts
MODE_CHANGE_TIMEOUT = 30  # Seconds to wait for MQTT confirmation
MAX_RETRIES = 2  # Number of retries after timeout (total attempts = 1 + MAX_RETRIES)

# Icons
ICON_NORMAL = "mdi:battery-sync-outline"
ICON_LOADING = "mdi:sync"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        [
            ModeSelect(coordinator=entry.runtime_data, config_entry=entry),
        ]
    )


class ModeSelect(EsySunhomeEntity, SelectEntity):
    """Represents the operating mode with optimistic updates during retries only."""

    _attr_translation_key = ATTR_SCHEDULE_MODE
    _attr_options = list(BatteryState.modes.values())
    _attr_current_option = _attr_options[0]
    _attr_name = "Operating Mode"
    _attr_icon = ICON_NORMAL

    def __init__(self, coordinator, config_entry: ConfigEntry):
        """Initialize the mode select entity."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._pending_mode_name = None     # Mode NAME we're trying to change to (string)
        self._pending_mode_key = None      # Mode KEY we're trying to change to (int)
        self._retry_count = 0
        self._confirmation_timeout = None
        self._actual_mqtt_mode_name = None # What MQTT actually says (string)
        self._actual_api_mode_code = None  # What the API says (int code from device info)
        self._is_loading = False

    @property
    def _use_mqtt_for_mode_change(self) -> bool:
        """Check if MQTT should be used for mode changes instead of API."""
        method = self._config_entry.options.get(
            CONF_MODE_CHANGE_METHOD, DEFAULT_MODE_CHANGE_METHOD
        )
        return method == MODE_CHANGE_MQTT

    @property
    def icon(self) -> str:
        """Return the icon based on loading state."""
        return ICON_LOADING if self._is_loading else ICON_NORMAL

    @property
    def extra_state_attributes(self) -> dict:
        """Return extra state attributes including loading state."""
        return {
            "loading": self._is_loading,
            "pending_mode": self._pending_mode_name,
            "actual_mode": self._actual_mqtt_mode_name,
            "retry_count": self._retry_count if self._is_loading else 0,
            "mode_change_method": "mqtt" if self._use_mqtt_for_mode_change else "api",
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.
        
        MQTT updates tell us the actual state of the battery.
        We use this to confirm pending changes or update the display.
        """
        # Try to get the mode name from MQTT data
        try:
            # schedule_mode returns the mode NAME (string like "Regular Mode")
            mqtt_mode_name = getattr(self.coordinator.data, ATTR_SCHEDULE_MODE, None)
        except (AttributeError, KeyError, TypeError) as e:
            _LOGGER.debug(f"Could not get mode from coordinator data: {e}")
            mqtt_mode_name = None
        
        if mqtt_mode_name is None:
            # No mode data available yet
            return
        
        # Always track the actual MQTT state
        prev_mqtt_mode = self._actual_mqtt_mode_name
        self._actual_mqtt_mode_name = mqtt_mode_name

        if prev_mqtt_mode != mqtt_mode_name:
            _LOGGER.debug(
                "MQTT mode changed '%s' -> '%s', pending='%s'",
                prev_mqtt_mode, mqtt_mode_name, self._pending_mode_name,
            )

        # Check if we have a pending mode change
        if self._pending_mode_name:
            if mqtt_mode_name == self._pending_mode_name:
                # Success! MQTT confirmed our requested mode
                _LOGGER.info(
                    f"✅ Mode change confirmed via MQTT: {mqtt_mode_name} "
                    f"after {self._retry_count} retries"
                )
                self._attr_current_option = mqtt_mode_name
                self._clear_pending_state(success=True)
            else:
                # MQTT shows something else - keep waiting unless timeout handles it
                _LOGGER.debug(
                    f"Waiting for MQTT confirmation. Current: {mqtt_mode_name}, "
                    f"Requested: {self._pending_mode_name}"
                )
                # Don't update display while pending - keep showing optimistic mode
        else:
            # Not pending - show mode based on API if available, else MQTT
            # API is the source of truth for modes 
            if self._actual_api_mode_code is not None:
                api_code_to_name = {v: k for k, v in BatteryState.modes_to_api.items()}
                api_mode = api_code_to_name.get(self._actual_api_mode_code)
                if api_mode:
                    self._attr_current_option = api_mode
                else:
                    self._attr_current_option = mqtt_mode_name
            else:
                self._attr_current_option = mqtt_mode_name

        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Set operating mode with optimistic update during retries only.
        
        Shows the new mode optimistically while retrying, but reverts to
        actual MQTT state if all retries fail.
        
        Args:
            option: The operating mode name to set
            
        Raises:
            HomeAssistantError: If mode change fails after retries
        """
        mode_key = self.get_mode_key(option)

        _LOGGER.info(
            "MODE CHANGE REQUEST: option='%s', resolved api_code=%s, "
            "current_option='%s', api_mode_code=%s",
            option, mode_key,
            self._attr_current_option, self._actual_api_mode_code,
        )

        if mode_key is None:
            error_msg = f"Invalid operating mode: {option}"
            _LOGGER.error(error_msg)
            raise HomeAssistantError(error_msg)

        # Check current mode via API (handles BEM correctly)
        current_api_mode = await self._fetch_api_mode()
        if current_api_mode == option:
            _LOGGER.info(f"Already in mode {option} (confirmed by API), no change needed")
            return

        _LOGGER.info(
            f"🔄 User requested mode change: {self._actual_mqtt_mode_name} → {option} "
            f"(code: {mode_key})"
        )
        
        # Optimistically update the displayed mode immediately
        # This prevents other automations from thinking it's still in the old mode
        self._attr_current_option = option
        
        # Set pending state (shows loading icon)
        self._set_pending_state(option, mode_key)
        
        # Send the initial API request
        await self._attempt_mode_change(option, mode_key)

    async def _attempt_mode_change(self, mode_name: str, mode_key: int) -> None:
        """Attempt to change mode via API or MQTT based on configuration.
        
        Two methods available (configurable in integration options):
        
        API (default, like the app):
          App → POST /api/lsypattern/switch → ESY Server → MQTT to inverter
          The ESY server is responsible for sending the MQTT command.
        
        MQTT (direct, faster for HA automations):
          HA → MQTT command → Inverter (bypasses cloud)

        Args:
            mode_name: The mode name being changed to (string)
            mode_key: The mode code being changed to (int)
        """
        # All mode changes use the API (like the phone app does).
        # The server handles sending MQTT commands to the inverter.
        _LOGGER.info(
            "MODE CHANGE DISPATCH: mode_name='%s', mode_key=%s, "
            "api_code=%s, device_id=%s",
            mode_name, mode_key, self._actual_api_mode_code,
            self.coordinator.api.device_id,
        )

        try:
            _LOGGER.info(
                "MODE CHANGE VIA API: POST %s with code=%s, deviceId=%s",
                "/api/lsypattern/switch", mode_key,
                self.coordinator.api.device_id,
            )
            await self.coordinator.api.set_mode(mode_key)
            _LOGGER.info(
                "✓ API call sent for mode change to: %s (attempt %d/%d)",
                mode_name, self._retry_count + 1, MAX_RETRIES + 1,
            )

            # Fire event for request success
            self.hass.bus.async_fire(
                "esy_sunhome_mode_change_requested",
                {
                    "device_id": self.coordinator.api.device_id,
                    "mode": mode_name,
                    "mode_code": mode_key,
                    "method": "api",
                    "attempt": self._retry_count + 1,
                }
            )
            
            # Confirm mode change via API (like the phone app does)
            await asyncio.sleep(1)  # Brief delay for server to process
            confirmed_mode = await self._fetch_api_mode()
            if confirmed_mode == mode_name:
                _LOGGER.info(
                    "✅ Mode change confirmed via API: %s", confirmed_mode
                )
                self._attr_current_option = confirmed_mode
                self._clear_pending_state(success=True)
                self.async_write_ha_state()
                return
            else:
                _LOGGER.info(
                    "API shows '%s' after requesting '%s', scheduling retry",
                    confirmed_mode, mode_name,
                )

            # Schedule timeout to retry if API confirmation didn't match
            self._schedule_confirmation_timeout(mode_name, mode_key)

        except Exception as err:
            error_msg = f"Failed to send mode change command for {mode_name}: {err}"
            _LOGGER.error(error_msg)
            
            # Revert to actual mode from API
            api_mode = await self._fetch_api_mode()
            if api_mode:
                self._attr_current_option = api_mode
            elif self._actual_mqtt_mode_name:
                self._attr_current_option = self._actual_mqtt_mode_name
            
            # Restore normal state immediately on error
            self._clear_pending_state(success=False)
            
            # Fire failure event
            self.hass.bus.async_fire(
                "esy_sunhome_mode_changed",
                {
                    "device_id": self.coordinator.api.device_id,
                    "mode": mode_name,
                    "mode_code": mode_key,
                    "success": False,
                    "error": str(err)
                }
            )
            
            self.async_write_ha_state()
            
            # Re-raise as HomeAssistantError so UI shows the error
            raise HomeAssistantError(
                f"Failed to change operating mode to {mode_name}. "
                f"Error: {err}"
            ) from err

    def _set_pending_state(self, mode_name: str, mode_key: int) -> None:
        """Set the entity to pending state (shows loading icon).
        
        Args:
            mode_name: The mode name being changed to (string)
            mode_key: The mode code being changed to (int)
        """
        self._pending_mode_name = mode_name
        self._pending_mode_key = mode_key
        self._retry_count = 0
        self._is_loading = True
        
        self.async_write_ha_state()
        
        _LOGGER.debug(
            f"🔄 Loading state set for mode change to: {mode_name}. "
            f"Showing optimistic mode to prevent automation conflicts."
        )

    def _clear_pending_state(self, success: bool = True) -> None:
        """Clear the pending state and restore normal icon.
        
        Args:
            success: Whether the mode change was successful
        """
        if self._pending_mode_name is None and not self._is_loading:
            return  # Nothing to clear
        
        old_mode = self._pending_mode_name
        old_retry_count = self._retry_count
        
        self._pending_mode_name = None
        self._pending_mode_key = None
        self._retry_count = 0
        self._is_loading = False
        
        # Cancel any pending timeout
        if self._confirmation_timeout:
            self._confirmation_timeout.cancel()
            self._confirmation_timeout = None
        
        if success and old_mode:
            _LOGGER.info(f"✅ Mode change to {old_mode} completed successfully")
            
            # Fire success event
            self.hass.bus.async_fire(
                "esy_sunhome_mode_changed",
                {
                    "device_id": self.coordinator.api.device_id,
                    "mode": old_mode,
                    "success": True,
                    "total_attempts": old_retry_count + 1
                }
            )

    def _schedule_confirmation_timeout(self, mode_name: str, mode_key: int) -> None:
        """Schedule a timeout to retry or revert if MQTT doesn't confirm.
        
        Args:
            mode_name: The mode name being changed to (string)
            mode_key: The mode code being changed to (int)
        """
        async def _timeout_callback():
            """Handle timeout waiting for MQTT confirmation."""
            if not self._pending_mode_name:
                return  # Already confirmed or cleared
            
            self._retry_count += 1
            
            if self._retry_count <= MAX_RETRIES:
                # Still have retries left - try again
                _LOGGER.warning(
                    f"⏱️ Mode change to {mode_name} timed out after {MODE_CHANGE_TIMEOUT}s. "
                    f"Retrying... (attempt {self._retry_count + 1}/{MAX_RETRIES + 1})"
                )
                
                # Fire retry event
                self.hass.bus.async_fire(
                    "esy_sunhome_mode_change_retry",
                    {
                        "device_id": self.coordinator.api.device_id,
                        "mode": mode_name,
                        "mode_code": mode_key,
                        "attempt": self._retry_count + 1,
                        "max_attempts": MAX_RETRIES + 1
                    }
                )
                
                try:
                    await self.coordinator.api.set_mode(mode_key)
                    _LOGGER.info(
                        "✓ Retry API call sent for mode: %s (attempt %d/%d)",
                        mode_name, self._retry_count + 1, MAX_RETRIES + 1,
                    )

                    # Confirm via API
                    await asyncio.sleep(1)
                    confirmed_mode = await self._fetch_api_mode()
                    if confirmed_mode == mode_name:
                        _LOGGER.info("✅ Mode change confirmed via API on retry: %s", confirmed_mode)
                        self._attr_current_option = confirmed_mode
                        self._clear_pending_state(success=True)
                        self.async_write_ha_state()
                        return

                    # Schedule another timeout if not confirmed
                    self._schedule_confirmation_timeout(mode_name, mode_key)
                    
                except Exception as err:
                    _LOGGER.error("❌ Retry %d failed: %s", self._retry_count, err)

                    # Revert to API mode
                    api_mode = await self._fetch_api_mode()
                    if api_mode:
                        self._attr_current_option = api_mode
                    
                    self._clear_pending_state(success=False)
                    self.async_write_ha_state()
            else:
                # No more retries - revert to actual state from API
                api_mode = await self._fetch_api_mode()
                actual_mode = api_mode or self._actual_mqtt_mode_name or "Unknown"

                _LOGGER.error(
                    f"❌ Mode change to {mode_name} failed after {MAX_RETRIES + 1} attempts "
                    f"({(MAX_RETRIES + 1) * MODE_CHANGE_TIMEOUT}s total). "
                    f"Reverting to actual state: {actual_mode}"
                )

                if actual_mode != "Unknown":
                    self._attr_current_option = actual_mode
                
                # Fire final timeout event
                self.hass.bus.async_fire(
                    "esy_sunhome_mode_change_timeout",
                    {
                        "device_id": self.coordinator.api.device_id,
                        "mode": mode_name,
                        "mode_code": mode_key,
                        "total_attempts": self._retry_count + 1,
                        "timeout_seconds": (MAX_RETRIES + 1) * MODE_CHANGE_TIMEOUT,
                        "reverted_to_actual": True,
                        "actual_mode": actual_mode
                    }
                )
                
                # Stop loading and revert to actual state
                self._clear_pending_state(success=False)
                self.async_write_ha_state()
        
        # Cancel any existing timeout
        if self._confirmation_timeout:
            self._confirmation_timeout.cancel()
        
        # Schedule new timeout
        self._confirmation_timeout = self.hass.loop.call_later(
            MODE_CHANGE_TIMEOUT,
            lambda: asyncio.create_task(_timeout_callback())
        )
        
        _LOGGER.debug(
            f"⏲️ Scheduled {MODE_CHANGE_TIMEOUT}s timeout for mode change confirmation "
            f"(retry {self._retry_count + 1}/{MAX_RETRIES + 1})"
        )

    def get_mode_key(self, value: str) -> int:
        """Get the API mode code to send for a given mode name.

        API codes (used with POST /api/lsypattern/switch):
        - Regular Mode -> 1
        - Emergency Mode -> 2
        - Electricity Sell Mode -> 3
        - Battery Energy Management -> 5

        Note: API codes differ from MQTT register values for some modes.

        Args:
            value: The operating mode name (e.g., "Regular Mode")

        Returns:
            The API mode code to send, or None if not found
        """
        return BatteryState.modes_to_api.get(value)

    async def _fetch_api_mode(self) -> str:
        """Fetch the current mode from the API (source of truth).

        Calls GET /api/lsydevice/info and reads the 'code' field.
        Returns the mode name string, or None if the fetch fails.

        API code mapping (same as modes_to_mqtt values):
          1 = Regular Mode
          3 = Electricity Sell Mode
          4 = Emergency Mode
          5 = Battery Energy Management
        """
        # Reverse map: API code (int) -> mode name
        api_code_to_name = {v: k for k, v in BatteryState.modes_to_api.items()}

        try:
            device_info = await self.coordinator.api.get_device_info()
            api_code = device_info.get("code")
            if api_code is not None:
                api_code = int(api_code)
                self._actual_api_mode_code = api_code
                mode_name = api_code_to_name.get(api_code)
                _LOGGER.info(
                    "API MODE CHECK: code=%s, resolved='%s'",
                    api_code, mode_name,
                )
                return mode_name
        except Exception as e:
            _LOGGER.warning("Failed to fetch mode from API: %s", e)

        return None
