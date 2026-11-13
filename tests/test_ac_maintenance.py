"""Offline AC maintenance contracts using real LibJciHitachi and minimal HA stubs.

No Home Assistant installation, credentials, or device connection is required.
The repository queue methods are executed with a mocked API; these tests do not
prove that an appliance starts or completes a requested maintenance cycle.
"""
import ast
import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from queue import Queue
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "jcihitachi_tw"


@pytest.fixture
def modules(monkeypatch):
    """Use the repository queue/update methods with small HA platform stubs."""
    from JciHitachi.model import STATUS_DICT

    # Each import exercises the actual registration without leaking it to other tests.
    monkeypatch.setitem(STATUS_DICT, "AC", dict(STATUS_DICT["AC"]))
    constants = {name: name.lower() for name in ("API", "COORDINATOR", "UPDATE_DATA", "UPDATED_DATA")}
    constants["DOMAIN"] = "jcihitachi_tw"
    node = ast.parse((COMPONENT / "__init__.py").read_text())
    data_class = next(n for n in node.body if isinstance(n, ast.ClassDef) and n.name == "UpdateData")
    entity_class = next(n for n in node.body if isinstance(n, ast.ClassDef) and n.name == "JciHitachiEntity")
    methods = [n for n in entity_class.body if isinstance(n, ast.FunctionDef) and n.name in ("put_queue", "update")]
    from dataclasses import dataclass, field
    from typing import Optional
    ns = {**constants, "dataclass": dataclass, "field": field, "Optional": Optional, "_LOGGER": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[data_class, *methods], type_ignores=[]), str(COMPONENT / "__init__.py"), "exec"), ns)

    class Base:
        def __init__(self, thing, coordinator):
            self._thing = thing
            self.coordinator = coordinator

        @property
        def name(self):
            return self._thing.name
    Base.put_queue = ns["put_queue"]
    Base.update = ns["update"]
    package = ModuleType("offline_hitachi")
    package.__path__ = []
    for key, value in constants.items():
        setattr(package, key, value)
    package.JciHitachiEntity = Base
    monkeypatch.setitem(sys.modules, "offline_hitachi", package)
    errors = ModuleType("homeassistant.exceptions")
    errors.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", errors)
    switch_platform = ModuleType("homeassistant.components.switch")
    switch_platform.SwitchEntity = type("SwitchEntity", (), {})
    monkeypatch.setitem(sys.modules, "homeassistant.components.switch", switch_platform)
    sensor_platform = ModuleType("homeassistant.components.sensor")
    sensor_platform.SensorEntity = type("SensorEntity", (), {
        "state_class": property(lambda self: getattr(self, "_attr_state_class", None)),
    })
    sensor_platform.SensorDeviceClass = SimpleNamespace(HUMIDITY="humidity", PM25="pm25", TEMPERATURE="temperature", ENERGY="energy", ENUM="enum")
    sensor_platform.SensorStateClass = SimpleNamespace(MEASUREMENT="measurement", TOTAL="total", TOTAL_INCREASING="total_increasing")
    monkeypatch.setitem(sys.modules, "homeassistant.components.sensor", sensor_platform)
    ha_const = ModuleType("homeassistant.const")
    ha_const.CONCENTRATION_MICROGRAMS_PER_CUBIC_METER = "µg/m³"
    ha_const.PERCENTAGE = "%"
    ha_const.UnitOfEnergy = SimpleNamespace(KILO_WATT_HOUR="kWh")
    ha_const.UnitOfTemperature = SimpleNamespace(CELSIUS="°C")
    ha_const.EntityCategory = SimpleNamespace(DIAGNOSTIC="diagnostic")
    monkeypatch.setitem(sys.modules, "homeassistant.const", ha_const)
    loaded = {}
    for name in ("switch", "sensor"):
        spec = importlib.util.spec_from_file_location("offline_hitachi." + name, COMPONENT / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded[name] = module
    return SimpleNamespace(**loaded, error=errors.HomeAssistantError)


@pytest.fixture
def context():
    """Provide one available idle AC and an API mock that cannot publish network traffic."""
    thing = SimpleNamespace(type="AC", name="offline-test", gateway_mac_address="offline-id", thing_name="offline-unit",
                            available=True, support_code=SimpleNamespace(CleanSwitch=3, _raw_status={"CleanSwitch": 3}),
                            status_code=SimpleNamespace(Switch="off", CleanSwitch="off", _raw_status={"Switch": 0, "UnmappedIonFeature": 1}))
    api = MagicMock()
    api.things = {thing.name: thing}
    api.set_status.return_value = True
    coordinator = MagicMock()
    hass = SimpleNamespace(data={"jcihitachi_tw": {"api": api, "coordinator": coordinator, "update_data": Queue()}}, loop=MagicMock())
    return hass, api, thing, coordinator


@pytest.mark.parametrize("feature,power", [("Ion", "off"), ("AirPurifier", "on")])
@pytest.mark.parametrize("enabled", [False, True])
def test_official_ac_fields_reach_existing_backend_once(modules, context, feature, power, enabled):
    """Both official fields use exactly one 0/1 command and preserve the required power state."""
    hass, api, thing, coordinator = context
    thing.status_code.Switch = power
    entity = modules.switch.JciHitachiACIonFeatureSwitch(thing, coordinator, feature, feature, power)
    entity.hass = hass
    (entity.turn_on if enabled else entity.turn_off)()
    api.set_status.assert_called_once_with(status_name=feature, device_name=thing.name,
                                          status_value=int(enabled), status_str_value=None)


@pytest.mark.parametrize("feature,power", [("Ion", "off"), ("AirPurifier", "on")])
@pytest.mark.parametrize("blocked_by", ["wrong_power", "unknown_power", "offline"])
def test_ineligible_state_does_not_send_ion_command(modules, context, feature, power, blocked_by):
    """Unavailable or ineligible units must not receive either feature command."""
    hass, api, thing, coordinator = context
    thing.status_code.Switch = power
    if blocked_by == "wrong_power":
        thing.status_code.Switch = "on" if power == "off" else "off"
    elif blocked_by == "unknown_power":
        thing.status_code.Switch = "unsupported"
    else:
        thing.available = False
    entity = modules.switch.JciHitachiACIonFeatureSwitch(thing, coordinator, feature, feature, power)
    entity.hass = hass
    with pytest.raises(modules.error):
        entity.turn_on()
    api.set_status.assert_not_called()


@pytest.mark.parametrize("feature", ["Ion", "AirPurifier"])
@pytest.mark.parametrize("support", [1, 3])
def test_actual_backend_accepts_both_capability_forms(modules, feature, support):
    """Use the real library validator with official range-style and older wider capability values."""
    from JciHitachi.model import JciHitachiAWSStatus, JciHitachiAWSStatusSupport
    capabilities = JciHitachiAWSStatusSupport({"DeviceType": 1, feature: support})
    for value in (0, 1):
        assert JciHitachiAWSStatus.str2id("AC", feature, status_value=value, support_code=capabilities) == (True, feature, value)
    state = JciHitachiAWSStatus({"DeviceType": 1, feature: 1})
    assert getattr(state.legacy_status, feature) == 1


@pytest.mark.parametrize("support,expected", [(1, 2), (3, 2), (0, 0), (None, 0), ("unsupported", 0), (True, 0)])
def test_ion_switches_require_advertised_capabilities(modules, context, support, expected):
    """Missing AC capabilities must not create apparently usable feature switches."""
    hass, api, thing, coordinator = context
    thing.support_code.Ion = support
    thing.support_code.AirPurifier = support
    add = MagicMock()
    asyncio.run(modules.switch.async_setup_entry(hass, None, add))
    entities = [entity for call in add.call_args_list for entity in call.args[0]]
    assert sum(isinstance(entity, modules.switch.JciHitachiACIonFeatureSwitch) for entity in entities) == expected


@pytest.mark.parametrize("value,expected", [(0, False), (1, True), (None, None), (7, None), ("unsupported", None), (True, None)])
@pytest.mark.parametrize("feature,power", [("Ion", "off"), ("AirPurifier", "on")])
def test_ion_state_does_not_invent_a_boolean(modules, context, value, expected, feature, power):
    """Unknown, missing and non-0/1 readings must stay unknown."""
    hass, api, thing, coordinator = context
    hass.data["jcihitachi_tw"]["updated_data"] = {thing.name: SimpleNamespace(**{feature: value})}
    entity = modules.switch.JciHitachiACIonFeatureSwitch(thing, coordinator, feature, feature, power)
    entity.hass = hass
    assert entity.is_on is expected


@pytest.mark.parametrize("feature_support", [None, 1])
def test_existing_sleep_switch_is_preserved(modules, context, feature_support):
    """An AC advertising sleep still gets its original switch with or without the new features."""
    hass, api, thing, coordinator = context
    thing.support_code.SleepModeRemainingTime = 480
    thing.support_code.Ion = thing.support_code.AirPurifier = feature_support
    add = MagicMock()
    asyncio.run(modules.switch.async_setup_entry(hass, None, add))
    entities = [entity for call in add.call_args_list for entity in call.args[0]]
    sleep = [entity for entity in entities if isinstance(entity, modules.switch.JciHitachiACSleepModeSwitchEntity)]
    assert len(sleep) == 1
    assert sleep[0].unique_id == "offline-id_sleep_mode_switch"
    assert sum(isinstance(entity, modules.switch.JciHitachiACIonFeatureSwitch) for entity in entities) == (2 if feature_support else 0)


@pytest.mark.parametrize("feature", ["Ion", "AirPurifier"])
def test_registration_keeps_existing_sdk_mapping(modules, monkeypatch, feature):
    """Future SDK definitions must survive the compatibility registration unchanged."""
    from JciHitachi.model import STATUS_DICT

    existing = {"controllable": False, "is_numeric": False, "legacy_name": "future_sdk_field"}
    monkeypatch.setitem(STATUS_DICT["AC"], feature, existing)
    modules.switch.__spec__.loader.exec_module(modules.switch)
    assert STATUS_DICT["AC"][feature] is existing


@pytest.mark.parametrize("enabled", [False, True])
def test_original_freeze_switch_keeps_single_legacy_command(modules, context, enabled):
    """Preserve PR128's start/stop request contract without adding retries or new preconditions."""
    hass, api, thing, coordinator = context
    entity = modules.switch.JciHitachiFreezeCleanSwitchEntity(thing, coordinator)
    entity.hass = hass
    (entity.turn_on if enabled else entity.turn_off)()
    api.set_status.assert_called_once_with(
        status_name="freeze_clean", device_name=thing.name,
        status_value=None, status_str_value="on" if enabled else "off",
    )
    api.get_status.assert_called_once_with(legacy=True)


@pytest.mark.parametrize("code,expected", [(0, "idle"), (1, "starting"), (2, "cleaning"), (7, None), ("unsupported", None)])
def test_original_freeze_sensor_preserves_enum_and_raw_value(modules, context, code, expected):
    """Keep PR128's experimental labels and unknown-value diagnostics as written by its author."""
    hass, api, thing, coordinator = context
    hass.data["jcihitachi_tw"]["updated_data"] = {thing.name: SimpleNamespace(CleanStatus=code)}
    entity = modules.sensor.JciHitachiFreezeCleanStatusSensorEntity(thing, coordinator)
    entity.hass = hass
    assert entity.native_value == expected
    assert entity.extra_state_attributes == {"raw_value": None if code == "unsupported" else code}


@pytest.mark.parametrize("language", ["en", "zh-Hant"])
@pytest.mark.parametrize("feature,power,key", [("Ion", "off", "ac_ion"), ("AirPurifier", "on", "ac_airpurifier")])
def test_ac_switch_translation_keys_resolve(modules, context, language, feature, power, key):
    """Both shipped languages must define a nonempty name for each entity's translation key."""
    hass, api, thing, coordinator = context
    entity = modules.switch.JciHitachiACIonFeatureSwitch(thing, coordinator, feature, feature, power)
    translations = json.loads((COMPONENT / "translations" / f"{language}.json").read_text())
    assert entity._attr_translation_key == key
    assert translations["entity"]["switch"][key]["name"].strip()


@pytest.mark.parametrize("raw,support,expected", [
    ({"PM25": 3}, None, 1),
    ({"PM25": 0}, "unsupported", 1),
    ({}, 999, 1),
    ({}, 0, 1),
    ({}, None, 0),
    ({}, "unsupported", 0),
    ({}, True, 0),
    ({}, -1, 0),
])
def test_ac_pm25_requires_reported_field_or_capability(modules, context, raw, support, expected):
    """Create an AC measurement only when its status or numeric capability advertises PM25."""
    hass, api, thing, coordinator = context
    thing.status_code._raw_status = raw
    thing.support_code.PM25 = support
    add = MagicMock()
    asyncio.run(modules.sensor.async_setup_entry(hass, None, add))
    entities = [entity for call in add.call_args_list for entity in call.args[0]]
    assert sum(isinstance(entity, modules.sensor.JciHitachiPM25SensorEntity) for entity in entities) == expected
    api.set_status.assert_not_called()


@pytest.mark.parametrize("value", [0, 1, 3, 999])
def test_actual_sdk_keeps_ac_pm25_measurement_and_legacy_name(modules, value):
    """The real SDK must preserve zero and positive readings without making PM25 controllable."""
    from JciHitachi.model import JciHitachiAWSStatus, STATUS_DICT

    state = JciHitachiAWSStatus({"DeviceType": 1, "PM25": value})
    assert state.PM25 == value
    assert state.legacy_status.pm25_value == value
    assert STATUS_DICT["AC"]["PM25"]["controllable"] is False
    assert STATUS_DICT["AC"]["PM25"]["is_numeric"] is True
    assert STATUS_DICT["AC"]["PM25"]["legacy_name"] == "pm25_value"


@pytest.mark.parametrize("device_type", ["AC", "DH"])
@pytest.mark.parametrize("value,expected", [
    (0, 0), (1, 1), (3, 3), (999, 999), (2.5, 2.5),
    (None, None), (True, None), (False, None), (-1, None),
    (float("nan"), None), (float("inf"), None), (float("-inf"), None),
    ("unsupported", None),
])
def test_pm25_sensor_preserves_measurements_and_rejects_invalid_values(modules, context, device_type, value, expected):
    """Both AC and existing DH sensors keep valid readings and never send a control command."""
    hass, api, thing, coordinator = context
    thing.type = device_type
    hass.data["jcihitachi_tw"]["updated_data"] = {thing.name: SimpleNamespace(pm25_value=value)}
    entity = modules.sensor.JciHitachiPM25SensorEntity(thing, coordinator)
    entity.hass = hass
    assert entity.native_value == expected
    assert entity.device_class == "pm25"
    assert entity.native_unit_of_measurement == "µg/m³"
    assert entity.state_class == "measurement"
    assert entity.unique_id == "offline-id_pm25_sensor"
    api.set_status.assert_not_called()


def test_pm25_sensor_missing_field_stays_unknown(modules, context):
    """An initial cached payload lacking the new legacy field must not become zero or raise."""
    hass, api, thing, coordinator = context
    hass.data["jcihitachi_tw"]["updated_data"] = {thing.name: SimpleNamespace()}
    entity = modules.sensor.JciHitachiPM25SensorEntity(thing, coordinator)
    entity.hass = hass
    assert entity.native_value is None
    api.set_status.assert_not_called()


def test_pm25_registration_keeps_existing_sdk_mapping(modules, monkeypatch):
    """The PM25 compatibility registration must preserve any future upstream SDK definition."""
    from JciHitachi.model import STATUS_DICT

    existing = {"controllable": False, "is_numeric": True, "legacy_name": "future_pm25"}
    monkeypatch.setitem(STATUS_DICT["AC"], "PM25", existing)
    modules.sensor.__spec__.loader.exec_module(modules.sensor)
    assert STATUS_DICT["AC"]["PM25"] is existing


def test_existing_dh_pm25_sensor_is_still_created(modules, context):
    """Adding AC support must retain the existing dehumidifier measurement entity."""
    hass, api, thing, coordinator = context
    thing.type = "DH"
    add = MagicMock()
    asyncio.run(modules.sensor.async_setup_entry(hass, None, add))
    entities = [entity for call in add.call_args_list for entity in call.args[0]]
    assert sum(isinstance(entity, modules.sensor.JciHitachiPM25SensorEntity) for entity in entities) == 1
    api.set_status.assert_not_called()
