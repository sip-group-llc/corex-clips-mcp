"""Private credential file creation, with restrictive permissions before secret writes."""
import json
import os
from pathlib import Path
import subprocess
from contextlib import contextmanager


def ensure_private_directory(path):
    path=Path(path);path.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.is_symlink():raise OSError('Private directory must not be a symbolic link.')
    if os.name=='nt':
        user=os.environ.get('USERDOMAIN','')+'\\'+os.environ['USERNAME']
        subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',user+':(OI)(CI)F','SYSTEM:(OI)(CI)F'],check=True,capture_output=True)
    else:path.chmod(0o700)
    return path


@contextmanager
def private_output(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        if os.name=='nt':
            user=os.environ.get('USERDOMAIN','')+'\\'+os.environ['USERNAME']
            subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',user+':F','SYSTEM:F'],check=True,capture_output=True)
        with os.fdopen(descriptor,'wb') as stream:
            descriptor=None
            yield stream
    except BaseException:
        if descriptor is not None:os.close(descriptor);descriptor=None
        path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:os.close(descriptor)


def write_private_json(path, data):
    with private_output(path) as stream:
        stream.write(json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8'))
