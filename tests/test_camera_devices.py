from types import SimpleNamespace

import pytest

from agent import camera_devices


class NativeDevices:
    def __init__(self, names, *, initialized=True, failure=None, empty=False):
        self.names = names
        self.initialized = initialized
        self.failure = failure
        self.empty = empty
        self.position = 0
        self.released = []
        self.uninitialized = 0

    def initialize(self):
        if self.failure == "initialize":
            raise OSError("COM initialization failed")
        return self.initialized

    def uninitialize(self):
        self.uninitialized += 1

    def create_device_enumerator(self):
        if self.failure == "create":
            raise OSError("Device enumerator failed")
        return "devices"

    def create_category_enumerator(self, devices):
        assert devices == "devices"
        if self.failure == "category":
            raise OSError("Category enumeration failed")
        return None if self.empty else "enumerator"

    def next_moniker(self, enumerator):
        assert enumerator == "enumerator"
        if self.failure == "next":
            raise OSError("Camera enumeration failed")
        if self.position == len(self.names):
            return None
        self.position += 1
        return self.position

    def friendly_name(self, moniker):
        if self.failure == "name":
            raise OSError("Reading metadata failed")
        return self.names[moniker - 1]

    def release(self, pointer):
        if pointer:
            self.released.append(pointer)


def install_native(monkeypatch, native):
    monkeypatch.setattr(camera_devices, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(camera_devices, "_DirectShow", lambda: native)


def test_names_use_matching_directshow_indices_including_obs(monkeypatch):
    native = NativeDevices(["HP Wide Vision HD Camera", "OBS Virtual Camera"])
    install_native(monkeypatch, native)
    assert camera_devices.directshow_camera_choices() == [
        ("HP Wide Vision HD Camera", 700), ("OBS Virtual Camera", 701),
    ]
    assert native.released == [1, 2, "enumerator", "devices"]
    assert native.uninitialized == 1


def test_missing_names_do_not_renumber_later_devices_or_merge_duplicates(monkeypatch):
    native = NativeDevices([None, "Камера", "  ", "OBS Virtual Camera", "Камера"])
    install_native(monkeypatch, native)
    assert camera_devices.directshow_camera_choices() == [
        ("Камера", 701), ("OBS Virtual Camera", 703), ("Камера", 704),
    ]
    assert native.released == [1, 2, 3, 4, 5, "enumerator", "devices"]


def test_no_devices_releases_category_owner(monkeypatch):
    native = NativeDevices([], empty=True)
    install_native(monkeypatch, native)
    assert camera_devices.directshow_camera_choices() == []
    assert native.released == ["devices"]
    assert native.uninitialized == 1


def test_existing_qt_com_apartment_is_not_uninitialized(monkeypatch):
    native = NativeDevices(["OBS Virtual Camera"], initialized=False)
    install_native(monkeypatch, native)
    assert camera_devices.directshow_camera_choices() == [("OBS Virtual Camera", 700)]
    assert native.uninitialized == 0


@pytest.mark.parametrize("failure,released,uninitialized", [
    ("initialize", [], 0),
    ("create", [], 1),
    ("category", ["devices"], 1),
    ("next", ["enumerator", "devices"], 1),
    ("name", [1, "enumerator", "devices"], 1),
])
def test_failures_propagate_with_balanced_native_cleanup(monkeypatch, failure, released, uninitialized):
    native = NativeDevices(["OBS Virtual Camera"], failure=failure)
    install_native(monkeypatch, native)
    with pytest.raises(OSError):
        camera_devices.directshow_camera_choices()
    assert native.released == released
    assert native.uninitialized == uninitialized


def test_outside_windows_does_not_load_native_libraries(monkeypatch):
    monkeypatch.setattr(camera_devices, "sys", SimpleNamespace(platform="linux"))

    def unexpected():
        raise AssertionError("Windows libraries must not load")

    monkeypatch.setattr(camera_devices, "_DirectShow", unexpected)
    assert camera_devices.directshow_camera_choices() == []


def test_encoded_indices_cannot_select_another_backend(monkeypatch):
    # OpenCV explicitly supports camera_id + domain_offset with CAP_ANY:
    # https://docs.opencv.org/4.13.0/d8/dfe/classcv_1_1VideoCapture.html
    # Its Windows implementation then enumerates CLSID_VideoInputDeviceCategory
    # in moniker order, matching this module rather than Qt's MF device list.
    native = NativeDevices([f"Camera {index}" for index in range(105)])
    install_native(monkeypatch, native)
    choices = camera_devices.directshow_camera_choices()
    assert len(choices) == 100
    assert choices[-1] == ("Camera 99", 799)
    assert all(index // 100 * 100 == 700 for _, index in choices)
    assert camera_devices.CAP_DSHOW == 700


@pytest.mark.parametrize("result,owned", [(0, True), (1, True), (-2147417850, False)])
def test_native_com_initialization_balances_only_owned_reference(result, owned):
    native = camera_devices._DirectShow.__new__(camera_devices._DirectShow)
    native.ole = SimpleNamespace(CoInitializeEx=lambda *_: result)
    assert native.initialize() is owned


def test_native_com_initialization_error_is_reported():
    native = camera_devices._DirectShow.__new__(camera_devices._DirectShow)
    native.ole = SimpleNamespace(CoInitializeEx=lambda *_: -2147467259)
    with pytest.raises(OSError, match="80004005"):
        native.initialize()
