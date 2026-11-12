"""Audit boundary for tests and their Python subprocesses, not a production sandbox."""
import os
import sys
from pathlib import Path


def install(roots):
    protected = [Path(root).resolve() for root in roots]

    def protected_path(value, fd=-1):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return False
        path = Path(os.fsdecode(value))
        if not path.is_absolute() and isinstance(fd, int) and fd >= 0:
            if sys.platform == 'darwin':
                import fcntl
                base = fcntl.fcntl(fd, 50, b'\0' * 1024).split(b'\0', 1)[0]
                path = Path(os.fsdecode(base)) / path
            else:
                path = Path(os.readlink(f'/proc/self/fd/{fd}')) / path
        path = path.absolute().resolve()
        return any(path.is_relative_to(root) for root in protected)

    def guard(event, args):
        if event == 'socket.connect':
            raise RuntimeError('OFFLINE_TEST_NETWORK_FORBIDDEN')
        if event == 'open':
            path, mode, flags = args
            writing = (mode and any(c in mode for c in 'wax+')) or (flags and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if writing and protected_path(path):
                raise RuntimeError('ORIGINAL_WRITE_FORBIDDEN:' + str(path))
        if event in ('os.remove', 'os.rmdir') and protected_path(args[0], args[1]):
            raise RuntimeError('ORIGINAL_REMOVE_FORBIDDEN:' + str(args))
        if event == 'os.rename' and (protected_path(args[0], args[2]) or protected_path(args[1], args[3])):
            raise RuntimeError('ORIGINAL_MOVE_FORBIDDEN:' + str(args))
        if event in ('os.mkdir', 'os.chmod', 'os.utime') and protected_path(args[0]):
            if event != 'os.mkdir' or not Path(args[0]).is_dir():
                raise RuntimeError('ORIGINAL_MUTATION_FORBIDDEN:' + str(args))
        if event == 'subprocess.Popen' and any(script in str(args[1]) for script in ('scraper_bwin.py', 'generate_context.py', 'prematch_multi.py')):
            raise RuntimeError('ACQUISITION_SUBPROCESS_FORBIDDEN')

    sys.addaudithook(guard)
