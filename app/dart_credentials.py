"""Windows-user protected DART credential; never return it to the browser."""
import ctypes
import json
from ctypes import wintypes
from pathlib import Path

class Blob(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]

def crypt(value, decrypt=False):
    buffer=ctypes.create_string_buffer(value)
    source=Blob(len(value),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    result=Blob()
    library=ctypes.WinDLL('crypt32',use_last_error=True)
    fn=library.CryptUnprotectData if decrypt else library.CryptProtectData
    fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    fn.restype=wintypes.BOOL
    if not fn(ctypes.byref(source),None,None,None,None,1,ctypes.byref(result)):
        raise RuntimeError('Windows credential protection failed')
    free=ctypes.WinDLL('kernel32').LocalFree
    free.argtypes=[ctypes.c_void_p];free.restype=ctypes.c_void_p
    try:return ctypes.string_at(result.data,result.size)
    finally:free(result.data)

def save_key(directory,key):
    path=Path(directory)/'dart_credential.json'
    encrypted=crypt(key.encode('ascii')).hex()
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version':1,'protected':encrypted}),'utf-8')
    temporary.replace(path)

def load_key(directory):
    path=Path(directory)/'dart_credential.json'
    if not path.exists():return None
    body=json.loads(path.read_text('utf-8'))
    if body.get('version')!=1:raise ValueError('Unsupported credential version')
    return crypt(bytes.fromhex(body['protected']),True).decode('ascii')
