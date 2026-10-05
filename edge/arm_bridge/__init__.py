"""Arm bridge: reusable MQTT contract layer between Oceanus and any arm SDK.

The bridge only depends on the frozen device protocol under ``edge/device_sim``.
Different arm SDKs (HTTP, serial, CAN) live behind one :class:`ArmDriver`
adapter, so the platform contract never changes when hardware changes.
"""
