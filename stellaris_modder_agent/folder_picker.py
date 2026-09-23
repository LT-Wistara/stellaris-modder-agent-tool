"""Windows native folder picker with multiple selection."""
import ctypes
import sys
import uuid


class GUID(ctypes.Structure):
    _fields_ = [('data1', ctypes.c_uint32), ('data2', ctypes.c_uint16),
                ('data3', ctypes.c_uint16), ('data4', ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


def _method(instance, index, *argument_types):
    address = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[index]
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argument_types)(address)


def _check(result):
    if result < 0:
        raise OSError(f'Windows 文件夹选择器失败：0x{result & 0xffffffff:08X}')


def _release(instance):
    if instance.value:
        _method(instance, 2)(instance)


def select_folders(parent, title='选择 Mod 目录'):
    """Return selected filesystem folders, or an empty list when cancelled."""
    if sys.platform != 'win32':
        raise OSError('当前系统不支持多选文件夹。')
    ole32 = ctypes.OleDLL('ole32')
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p,
                                       ctypes.c_uint32, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    initialized = ole32.CoInitializeEx(None, 2)
    _check(initialized)
    dialog = ctypes.c_void_p()
    items = ctypes.c_void_p()
    item = ctypes.c_void_p()
    try:
        clsid = GUID.parse('DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7')
        iid = GUID.parse('D57C7288-D4AD-4768-BE02-9D969532D960')
        _check(ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(dialog)))
        options = ctypes.c_uint32()
        _check(_method(dialog, 10, ctypes.POINTER(ctypes.c_uint32))(dialog, ctypes.byref(options)))
        _check(_method(dialog, 9, ctypes.c_uint32)(dialog, options.value | 0x20 | 0x40 | 0x200))
        _check(_method(dialog, 17, ctypes.c_wchar_p)(dialog, title))
        result = _method(dialog, 3, ctypes.c_void_p)(dialog, parent.winfo_id())
        if result & 0xffffffff == 0x800704C7:
            return []
        _check(result)
        _check(_method(dialog, 27, ctypes.POINTER(ctypes.c_void_p))(dialog, ctypes.byref(items)))
        count = ctypes.c_uint32()
        _check(_method(items, 7, ctypes.POINTER(ctypes.c_uint32))(items, ctypes.byref(count)))
        paths = []
        for index in range(count.value):
            item = ctypes.c_void_p()
            _check(_method(items, 8, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))(
                items, index, ctypes.byref(item)))
            name = ctypes.c_void_p()
            try:
                _check(_method(item, 5, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))(
                    item, 0x80058000, ctypes.byref(name)))
                paths.append(ctypes.wstring_at(name.value))
            finally:
                if name.value:
                    ole32.CoTaskMemFree(name)
                _release(item)
                item = ctypes.c_void_p()
        return paths
    finally:
        _release(item)
        _release(items)
        _release(dialog)
        ole32.CoUninitialize()
