"""Enumerate Windows cameras without opening their video streams.

Qt's Windows camera list uses Media Foundation, which omits DirectShow-only
devices such as OBS Virtual Camera. Use the same moniker order as OpenCV's
DirectShow backend and encode that backend in its existing integer camera ID.
"""

from __future__ import annotations

import ctypes as C
import sys
import uuid


CAP_DSHOW = 700  # OpenCV backend value; no cv2 import or camera probing.
_HRESULT = C.c_int32
_ULONG = C.c_uint32
_RPC_E_CHANGED_MODE = -2147417850


class _Guid(C.Structure):
    _fields_ = [("data", C.c_ubyte * 16)]


def _guid(value):
    return _Guid.from_buffer_copy(uuid.UUID(value).bytes_le)


class _VariantValue(C.Union):
    _fields_ = [
        ("bstr", C.c_void_p),
        ("storage", C.c_ubyte * (16 if C.sizeof(C.c_void_p) == 8 else 8)),
    ]


class _Variant(C.Structure):
    _fields_ = [("kind", C.c_uint16), ("reserved", C.c_uint16 * 3), ("value", _VariantValue)]


def _checked(result, operation):
    if result < 0:
        raise OSError(f"{operation} failed (HRESULT 0x{result & 0xffffffff:08X})")
    return result


def _method(pointer, index, result, *arguments):
    table = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
    return C.WINFUNCTYPE(result, C.c_void_p, *arguments)(table[index])


class _DirectShow:
    """Small COM wrapper; binding is only to metadata, never a capture filter."""

    def __init__(self):
        self.ole = C.WinDLL("ole32")
        self.automation = C.WinDLL("oleaut32")
        self.ole.CoInitializeEx.argtypes = (C.c_void_p, _ULONG)
        self.ole.CoInitializeEx.restype = _HRESULT
        self.ole.CoUninitialize.argtypes = ()
        self.ole.CoUninitialize.restype = None
        self.ole.CoCreateInstance.argtypes = (
            C.POINTER(_Guid), C.c_void_p, _ULONG, C.POINTER(_Guid), C.POINTER(C.c_void_p),
        )
        self.ole.CoCreateInstance.restype = _HRESULT
        self.automation.VariantClear.argtypes = (C.POINTER(_Variant),)
        self.automation.VariantClear.restype = _HRESULT

    def initialize(self):
        result = self.ole.CoInitializeEx(None, 0)
        # Qt can already own an STA apartment on this thread. Do not change or
        # uninitialize it; enumeration works inside that existing apartment.
        if result == _RPC_E_CHANGED_MODE:
            return False
        _checked(result, "CoInitializeEx")
        return True  # Both S_OK and S_FALSE require a balancing uninitialize.

    def uninitialize(self):
        self.ole.CoUninitialize()

    def release(self, pointer):
        if pointer:
            _method(pointer, 2, _ULONG)(pointer)

    def create_device_enumerator(self):
        pointer = C.c_void_p()
        _checked(self.ole.CoCreateInstance(
            C.byref(_guid("62be5d10-60eb-11d0-bd3b-00a0c911ce86")), None, 1,
            C.byref(_guid("29840822-5b84-11d0-bd3b-00a0c911ce86")), C.byref(pointer),
        ), "CoCreateInstance")
        if not pointer:
            raise OSError("DirectShow returned no device enumerator")
        return pointer

    def create_category_enumerator(self, devices):
        pointer = C.c_void_p()
        result = _method(devices, 3, _HRESULT, C.POINTER(_Guid), C.POINTER(C.c_void_p), _ULONG)(
            devices, C.byref(_guid("860bb310-5d01-11d0-bd3b-00a0c911ce86")), C.byref(pointer), 0,
        )
        _checked(result, "CreateClassEnumerator")
        if result == 1:  # S_FALSE means this device category is empty.
            self.release(pointer)
            return None
        if not pointer:
            raise OSError("DirectShow returned no camera enumerator")
        return pointer

    def next_moniker(self, enumerator):
        pointer, fetched = C.c_void_p(), _ULONG()
        result = _method(enumerator, 3, _HRESULT, _ULONG, C.POINTER(C.c_void_p), C.POINTER(_ULONG))(
            enumerator, 1, C.byref(pointer), C.byref(fetched),
        )
        if result != 0:
            self.release(pointer)
            _checked(result, "IEnumMoniker.Next")
            return None
        if fetched.value != 1 or not pointer:
            self.release(pointer)
            raise OSError("DirectShow returned an invalid camera moniker")
        return pointer

    def friendly_name(self, moniker):
        bag = C.c_void_p()
        try:
            result = _method(moniker, 9, _HRESULT, C.c_void_p, C.c_void_p,
                             C.POINTER(_Guid), C.POINTER(C.c_void_p))(
                moniker, None, None, C.byref(_guid("55272a00-42cb-11ce-8135-00aa004bb851")), C.byref(bag),
            )
            if result < 0 or not bag:
                return None
            for property_name in ("FriendlyName", "Description"):
                value = _Variant()
                try:
                    result = _method(bag, 3, _HRESULT, C.c_wchar_p, C.POINTER(_Variant), C.c_void_p)(
                        bag, property_name, C.byref(value), None,
                    )
                    if result >= 0 and value.kind == 8 and value.value.bstr:
                        name = C.wstring_at(value.value.bstr).strip()
                        if name:
                            return name
                finally:
                    self.automation.VariantClear(C.byref(value))
            return None
        finally:
            self.release(bag)


def directshow_camera_choices() -> list[tuple[str, int]]:
    """Return names and OpenCV IDs, or [] outside Windows/with no cameras.

    Native enumeration failures raise OSError so callers can choose a fallback.
    The IDs include CAP_DSHOW and must not be combined with a different backend.
    Only metadata is read: no device is activated and no frames are requested.
    """
    if sys.platform != "win32":
        return []
    native = _DirectShow()
    initialized = False
    devices = enumerator = None
    try:
        initialized = native.initialize()
        devices = native.create_device_enumerator()
        enumerator = native.create_category_enumerator(devices)
        choices = []
        if not enumerator:
            return choices
        # OpenCV's encoded ID reserves two decimal digits for the camera index.
        for index in range(100):
            moniker = native.next_moniker(enumerator)
            if not moniker:
                break
            try:
                name = native.friendly_name(moniker)
            finally:
                native.release(moniker)
            if isinstance(name, str) and name.strip():
                choices.append((name.strip(), CAP_DSHOW + index))
        return choices
    finally:
        native.release(enumerator)
        native.release(devices)
        if initialized:
            native.uninitialize()
