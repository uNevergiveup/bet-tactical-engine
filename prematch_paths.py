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


def reference_root(*control_paths):
    """Infer a boundary from explicit control files, never from a document's references."""
    import os
    paths = [Path(p).absolute() for p in control_paths]
    for path in paths:
        if 'outputs' in path.parts:
            index = path.parts.index('outputs')
            return Path(*path.parts[:index]).resolve()
    for path in paths:
        for parent in path.parents:
            if parent.name == 'prospective_trial':
                return parent.parent.parent.resolve()
    return Path(os.path.commonpath([str(p.parent) for p in paths])).resolve()


def manifest_reference(root, value, hashes):
    """Lookup legacy manifest keys without rewriting the immutable manifest."""
    raw = Path(value)
    candidate = Path(root) / raw.relative_to(LEGACY_ROOT) if raw.is_absolute() and raw.is_relative_to(LEGACY_ROOT) else raw
    path = local_reference(root, candidate)
    names = [str(path), str(LEGACY_ROOT / path.relative_to(Path(root).resolve()))]
    found = {hashes[name] for name in names if name in hashes}
    if len(found) != 1:
        raise ValueError('MANIFEST_REFERENCE_MISSING_OR_CONFLICTING')
    return local_reference(root, path, found.pop())


def control_file(value):
    """Explicit input has no implicit legacy SHA: require a local, non-global file."""
    path = Path(value).absolute()
    if path.is_relative_to(LEGACY_ROOT):
        raise ValueError('LEGACY_CONTROL_REQUIRES_EXPLICIT_LOCAL_SHA_BINDING')
    return local_reference(reference_root(path), path)
