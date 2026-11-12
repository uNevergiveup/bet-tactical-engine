"""Local reference boundary. Never follow the restored project's external state."""
from pathlib import Path
import hashlib
import re

LEGACY_ROOT = Path('/Users/antonio/Projects/scraper')


def local_reference(root, value, digest=None):
    original_root = Path(root).absolute()
    root = original_root.resolve()
    if not value:
        raise ValueError('REFERENCE_PATH_MISSING')
    path = Path(value).expanduser()
    remapped = False
    if path.is_absolute() and path.is_relative_to(LEGACY_ROOT):
        path = root / path.relative_to(LEGACY_ROOT)
        remapped = True
    elif not path.is_absolute():
        path = root / path
    elif path.is_relative_to(original_root):
        path = root / path.relative_to(original_root)
    # Check lexical and resolved paths: a latest symlink must never be an input.
    if path.is_relative_to(root / 'outputs/latest'):
        raise ValueError('LEGACY_ACTIVE_STATE_FORBIDDEN: use an explicit run selection')
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or resolved.is_relative_to(root / 'outputs/latest'):
        raise ValueError('REFERENCE_OUTSIDE_PROJECT')
    if remapped and not digest:
        raise ValueError('REMAPPED_REFERENCE_REQUIRES_SHA256')
    if digest is not None:
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('REFERENCE_SHA256_INVALID')
        if not resolved.is_file() or hashlib.sha256(resolved.read_bytes()).hexdigest() != digest:
            raise ValueError('REFERENCE_SHA256_MISMATCH')
    return resolved


def explicit_selection(path):
    """Reject global pointers before resolving them, including external links."""
    root = Path(__file__).resolve().parent
    return local_reference(root, path)
