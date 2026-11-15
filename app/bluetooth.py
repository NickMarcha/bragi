"""Bluetooth on the Bragi host: pairing game controllers from the dashboard.

Talks to the host's BlueZ over the system D-Bus (the container mounts
/run/dbus; BlueZ's default policy lets any local user call it). The
dashboard's Controllers dialog lists the paired controllers, scans for new
ones and pairs, trusts and connects them, so a controller reconnects to the
host on its own from then on. app/gamepad.py takes it from there.

Pairing uses a "NoInputNoOutput" agent, which game controllers need. It is
registered only while a Pair call runs, and refuses anything outside one,
so nothing can pair with the host unasked in between.

BluezBackend is the D-Bus part; Manager holds the logic and is what the
tests drive with a fake backend.
"""

# No `from __future__ import annotations` here: dbus-fast reads D-Bus
# signatures from the agent's method annotations ("o", "s", ...).

import asyncio
import contextlib
import logging
from dataclasses import dataclass

logger = logging.getLogger("bragi.bluetooth")

ADAPTER = "/org/bluez/hci0"
AGENT_PATH = "/bragi/agent"
SCAN_SECONDS = 60


class BluetoothError(Exception):
    """Something the dashboard can show as it is."""


@dataclass(frozen=True)
class Device:
    address: str
    name: str
    icon: str
    paired: bool
    trusted: bool
    connected: bool
    rssi: int | None = None

    @property
    def is_controller(self) -> bool:
        return self.icon == "input-gaming"


class BluezBackend:
    """The host's BlueZ over D-Bus (dbus-fast)."""

    def __init__(self) -> None:
        self._bus = None
        self._agent = None
        self._on_change = None
        self._pairing = False

    async def _connect(self):
        if self._bus is not None and self._bus.connected:
            return self._bus
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as exc:
            raise BluetoothError(f"Can't reach the host's Bluetooth service: {exc}") from exc
        root = self._bus.get_proxy_object("org.bluez", "/", await self._bus.introspect("org.bluez", "/"))
        manager = root.get_interface("org.freedesktop.DBus.ObjectManager")
        manager.on_interfaces_added(lambda *_: self._changed())
        manager.on_interfaces_removed(lambda *_: self._changed())
        # Property changes (Connected, Paired, RSSI...) on any BlueZ object.
        self._bus.add_message_handler(self._on_message)
        await self._bus.call(self._match("type='signal',sender='org.bluez',"
                                         "interface='org.freedesktop.DBus.Properties',member='PropertiesChanged'"))
        return self._bus

    @staticmethod
    def _match(rule: str):
        from dbus_fast import Message
        return Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                       interface="org.freedesktop.DBus", member="AddMatch", signature="s", body=[rule])

    def _on_message(self, message) -> None:
        if message.member == "PropertiesChanged" and (message.path or "").startswith(ADAPTER):
            self._changed()

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()

    def subscribe(self, on_change) -> None:
        self._on_change = on_change

    async def _interface(self, path: str, name: str):
        bus = await self._connect()
        try:
            proxy = bus.get_proxy_object("org.bluez", path, await bus.introspect("org.bluez", path))
            return proxy.get_interface(name)
        except Exception as exc:
            raise BluetoothError(_message(exc)) from exc

    async def devices(self) -> tuple[bool, bool, list[Device]]:
        """(powered, discovering, devices) for the adapter."""
        bus = await self._connect()
        root = bus.get_proxy_object("org.bluez", "/", await bus.introspect("org.bluez", "/"))
        objects = await root.get_interface("org.freedesktop.DBus.ObjectManager").call_get_managed_objects()
        adapter = objects.get(ADAPTER, {}).get("org.bluez.Adapter1")
        if adapter is None:
            raise BluetoothError("The host has no Bluetooth adapter.")
        devices = []
        for path, interfaces in objects.items():
            props = interfaces.get("org.bluez.Device1")
            if props is None or not path.startswith(ADAPTER + "/"):
                continue
            value = lambda key, default=None: props[key].value if key in props else default  # noqa: E731
            devices.append(Device(address=value("Address", ""), name=value("Alias") or value("Name") or value("Address", ""),
                                  icon=value("Icon", ""), paired=bool(value("Paired", False)),
                                  trusted=bool(value("Trusted", False)), connected=bool(value("Connected", False)),
                                  rssi=value("RSSI")))
        return bool(adapter["Powered"].value), bool(adapter["Discovering"].value), devices

    async def set_powered(self) -> None:
        from dbus_fast import Variant
        props = await self._interface(ADAPTER, "org.freedesktop.DBus.Properties")
        try:
            await props.call_set("org.bluez.Adapter1", "Powered", Variant("b", True))
        except Exception as exc:
            raise BluetoothError(_message(exc)) from exc

    async def start_discovery(self) -> None:
        from dbus_fast import Variant
        adapter = await self._interface(ADAPTER, "org.bluez.Adapter1")
        with contextlib.suppress(Exception):
            await adapter.call_set_discovery_filter({"Transport": Variant("s", "auto")})
        try:
            await adapter.call_start_discovery()
        except Exception as exc:
            if "InProgress" not in str(exc):
                raise BluetoothError(_message(exc)) from exc

    async def stop_discovery(self) -> None:
        adapter = await self._interface(ADAPTER, "org.bluez.Adapter1")
        with contextlib.suppress(Exception):
            await adapter.call_stop_discovery()

    async def pair(self, address: str) -> None:
        from dbus_fast import Variant
        path = _device_path(address)
        bus = await self._connect()
        agent_manager = await self._interface("/org/bluez", "org.bluez.AgentManager1")
        if self._agent is None:
            self._agent = _make_agent(lambda: self._pairing)
            bus.export(AGENT_PATH, self._agent)
        self._pairing = True
        try:
            await agent_manager.call_register_agent(AGENT_PATH, "NoInputNoOutput")
            with contextlib.suppress(Exception):
                await agent_manager.call_request_default_agent(AGENT_PATH)
            device = await self._interface(path, "org.bluez.Device1")
            try:
                await device.call_pair()
            except Exception as exc:
                if "AlreadyExists" not in str(exc):
                    raise BluetoothError(_message(exc)) from exc
            props = await self._interface(path, "org.freedesktop.DBus.Properties")
            await props.call_set("org.bluez.Device1", "Trusted", Variant("b", True))
            with contextlib.suppress(Exception):
                await device.call_connect()
        finally:
            self._pairing = False
            with contextlib.suppress(Exception):
                await agent_manager.call_unregister_agent(AGENT_PATH)

    async def remove(self, address: str) -> None:
        adapter = await self._interface(ADAPTER, "org.bluez.Adapter1")
        try:
            await adapter.call_remove_device(_device_path(address))
        except Exception as exc:
            raise BluetoothError(_message(exc)) from exc


def _device_path(address: str) -> str:
    return f"{ADAPTER}/dev_{address.upper().replace(':', '_')}"


def _message(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    if "Blocked" in text or "rfkill" in text.lower():
        return "Bluetooth is switched off on the host (rfkill). Run `sudo rfkill unblock bluetooth` there once."
    if "NotReady" in text:
        return "The host's Bluetooth adapter isn't ready. Is it powered?"
    if "AuthenticationFailed" in text or "AuthenticationCanceled" in text:
        return "Pairing failed. Put the controller in pairing mode again and retry."
    if "UnknownObject" in text or "DoesNotExist" in text:
        return "That device isn't in range any more. Scan again."
    return text


def _make_agent(pairing):
    """BlueZ's Agent1 for "Just Works" pairing, accepting only while a
    Pair call from the dashboard runs."""
    from dbus_fast import DBusError
    from dbus_fast.service import ServiceInterface, method

    def allow():
        if not pairing():
            raise DBusError("org.bluez.Error.Rejected", "Bragi isn't pairing right now")

    class Agent(ServiceInterface):
        def __init__(self):
            super().__init__("org.bluez.Agent1")

        @method()
        def Release(self):
            pass

        @method()
        def RequestPinCode(self, device: "o") -> "s":
            allow()
            return "0000"

        @method()
        def RequestPasskey(self, device: "o") -> "u":
            allow()
            return 0

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):
            allow()

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
            allow()

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):
            allow()

        @method()
        def RequestAuthorization(self, device: "o"):
            allow()

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):
            allow()

        @method()
        def Cancel(self):
            pass

    return Agent()


class Manager:
    """What the dashboard sees and asks for. Every change calls
    on_change(), which main.py turns into a "bluetooth" broadcast."""

    def __init__(self, backend, on_change) -> None:
        self._backend = backend
        self._on_change = on_change
        self._scan_task: asyncio.Task | None = None
        self.pairing: str | None = None
        self.error: str | None = None
        self.last: tuple[bool, bool, list[Device]] = (False, False, [])
        backend.subscribe(self._changed)

    def _changed(self) -> None:
        self._on_change()

    async def refresh(self) -> dict:
        try:
            self.last = await self._backend.devices()
        except BluetoothError as exc:
            self.error = str(exc)
        return self.view()

    def view(self) -> dict:
        powered, discovering, devices = self.last
        paired = sorted((d for d in devices if d.paired), key=lambda d: (not d.connected, d.name.lower()))
        # While scanning: controllers not paired yet, strongest signal first.
        found = sorted((d for d in devices if not d.paired and d.is_controller),
                       key=lambda d: -(d.rssi if d.rssi is not None else -999))
        return {"powered": powered, "scanning": discovering or self._scan_task is not None,
                "paired": paired, "found": found, "pairing": self.pairing, "error": self.error}

    async def scan(self) -> None:
        """Looks for controllers for SCAN_SECONDS, then stops by itself."""
        self.error = None
        try:
            powered, _, _ = await self._backend.devices()
            if not powered:
                await self._backend.set_powered()
            await self._backend.start_discovery()
        except BluetoothError as exc:
            self.error = str(exc)
            self._changed()
            return
        if self._scan_task is not None:
            self._scan_task.cancel()
        self._scan_task = asyncio.create_task(self._stop_later())
        self._changed()

    async def _stop_later(self) -> None:
        try:
            await asyncio.sleep(SCAN_SECONDS)
            await self._backend.stop_discovery()
        finally:
            self._scan_task = None
            self._changed()

    async def stop_scan(self) -> None:
        if self._scan_task is not None:
            self._scan_task.cancel()
        await self._backend.stop_discovery()
        self._changed()

    async def pair(self, address: str) -> None:
        self.error = None
        self.pairing = address
        self._changed()
        try:
            # Discovery slows pairing down and isn't needed once the device is known.
            await self.stop_scan()
            await self._backend.pair(address)
        except BluetoothError as exc:
            self.error = str(exc)
        finally:
            self.pairing = None
            self._changed()

    async def forget(self, address: str) -> None:
        self.error = None
        try:
            await self._backend.remove(address)
        except BluetoothError as exc:
            self.error = str(exc)
        self._changed()


manager: Manager | None = None


def start(on_change) -> Manager:
    global manager
    manager = Manager(BluezBackend(), on_change)
    return manager
