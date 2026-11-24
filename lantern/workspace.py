"""Supervised UTF-8 file operations within Lantern's persistent work directory."""

from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path, PureWindowsPath
import secrets
import stat

from .digest_prefix import DIGEST_PREFIX_PATTERN, MIN_DIGEST_PREFIX, normalize_digest_prefix
from .workspace_diff import edit_summary


MAX_TEXT = 32_768
MAX_BYTES = 4 * MAX_TEXT
TMP_PREFIX = ".lantern-tmp-"
PATH = {"type": "string", "maxLength": 512, "description": "Relative path, using / between directories."}
OFFSET = {"type": "integer", "minimum": 0, "default": 0}
SHA = {"type": "string", "pattern": DIGEST_PREFIX_PATTERN,
       "description": f"SHA-256 of the current file, or its first {MIN_DIGEST_PREFIX} or more hex characters."}
TEXT = {"type": "string", "maxLength": MAX_TEXT}
NONEMPTY_TEXT = {**TEXT, "minLength": 1}
WORKSPACE_EDITS = frozenset(("workspace_write", "workspace_append", "workspace_replace"))


def _tool(name, description, properties, required):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": properties, "required": required,
    }}


WORKSPACE_TOOLS = (
    _tool("workspace_list", "List one directory of your persistent workspace in name order. Regular files report their size in bytes; directories and links report null.",
          {"path": PATH, "offset": OFFSET, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
    _tool("workspace_read", "Read a page of a UTF-8 workspace file. sha256 and sha256_prefix cover the whole file.",
          {"path": PATH, "offset": OFFSET, "length": {"type": "integer", "minimum": 1, "maximum": 8000}}, ["path"]),
    _tool("workspace_write", "Save a complete UTF-8 note or source file. Parent directories are created "
          "as needed. To replace a file, copy its sha256_prefix from workspace_read into expected_sha256. "
          "Without expected_sha256 this call only creates a new file.",
          {"path": PATH, "text": TEXT, "expected_sha256": SHA},
          ["path", "text"]),
    _tool("workspace_append", "Append text verbatim to an existing UTF-8 workspace file. Include any desired "
          "newlines. Optionally supply the exact current ending as expected_suffix or its sha256_prefix as "
          "expected_sha256 to check the revision.",
          {"path": PATH, "text": TEXT, "expected_suffix": NONEMPTY_TEXT, "expected_sha256": SHA}, ["path", "text"]),
    _tool("workspace_replace", "Replace one exact occurrence of find in an existing UTF-8 workspace file. "
          "Supply enough surrounding text to make the match unique. An empty replace value deletes the match. "
          "Optionally supply the file's sha256_prefix as expected_sha256.",
          {"path": PATH, "find": NONEMPTY_TEXT, "replace": TEXT, "expected_sha256": SHA}, ["path", "find", "replace"]),
)


def _parts(path, allow_root=False):
    if allow_root and path in ("", "."):
        return []
    if not isinstance(path, str) or not path or len(path) > 512:
        raise ValueError("Supply a relative workspace path of 1–512 characters.")
    parts = path.split("/")
    for part in parts:
        if (not part or part.endswith((" ", ".")) or len(part) > 128
                or part.casefold().startswith(TMP_PREFIX) or PureWindowsPath(part).is_reserved()
                or any(ord(c) < 32 or c in '<>:"|?*\\' for c in part)):
            raise ValueError("Use ordinary relative workspace names without path traversal or device names.")
    return parts


def _integer(args, key, default, minimum, maximum=None):
    value = args.get(key, default)
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"Invalid {key}.")
    return value


def _plain_file(info):
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and not (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))


@contextmanager
def _windows_directory(path):
    # Pin each directory against renames, junction swaps and deletion while we
    # use path-based Windows APIs.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create_file.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    # FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE,
    # OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT.
    handle = create_file(str(path), 0x80, 3, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("A workspace directory must be an ordinary directory.")
        yield path
    finally:
        close(handle)


def _at(parent, name):
    if isinstance(parent, Path):
        return parent / name, {}
    return name, {"dir_fd": parent}


@contextmanager
def _parent(root, components, create=False, *, preview=False):
    # The configured root's ancestors belong to the operator. Pin that root and
    # every child used in this request. All tool operations serialize at the root.
    root = Path(root).absolute()
    if root.resolve(strict=True) != root:
        raise ValueError("The configured workspace must be a real directory.")
    with ExitStack() as stack:
        if os.name == "nt":
            import msvcrt
            parent = stack.enter_context(_windows_directory(root))
            # Keep the reserved lock file next to the tool root in operator-only
            # workspace metadata. Its byte-range lock dies with the process.
            # Preview pins the directory and verifies the digest of the bytes
            # it reads. Leave the lock file untouched, even on a fresh root.
            if not preview:
                lock = stack.enter_context((root.parent / ("." + root.name + ".lock")).open("a+b"))
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open(root, flags)
            stack.callback(os.close, parent)
            fcntl.flock(parent, fcntl.LOCK_EX)
        for component in components:
            path, kwargs = _at(parent, component)
            if create:
                try:
                    os.mkdir(path, mode=0o700, **kwargs)
                except FileExistsError:
                    pass
                else:
                    if isinstance(parent, int):
                        os.fsync(parent)
            try:
                if os.name == "nt":
                    parent = stack.enter_context(_windows_directory(path))
                else:
                    parent = os.open(path, flags, **kwargs)
                    stack.callback(os.close, parent)
            except FileNotFoundError:
                if not preview:
                    raise
                yield None  # Missing ordinary parent. Preview never creates it.
                return
        yield parent


def _read(parent, name):
    path, kwargs = _at(parent, name)
    before = os.stat(path, follow_symlinks=False, **kwargs)
    if not _plain_file(before):
        raise ValueError("Only singly linked, regular workspace files can be opened.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags, **kwargs), "rb") as source:
        info = os.fstat(source.fileno())
        if not _plain_file(info) or not os.path.samestat(before, info):
            raise ValueError("The workspace file changed while opening it.")
        if info.st_size > MAX_BYTES:
            raise ValueError("The file is too large for the text workspace tools.")
        data = source.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("The file is too large for the text workspace tools.")
    return data


class _EditConflict(ValueError):
    """A host-generated precondition error that can be returned to Lantern."""


def _prepare_edit(parent, filename, tool, arguments, expected):
    try:
        original = _read(parent, filename) if parent is not None else None
    except FileNotFoundError:
        original = None
    digest = hashlib.sha256(original).hexdigest() if original is not None else None
    if expected is not None and (digest is None or not digest.startswith(expected)):
        raise _EditConflict("The SHA-256 does not match the current file or the file is missing. Read it again before saving.")
    if tool == "workspace_write":
        if original is not None and expected is None:
            raise _EditConflict("File exists. Use workspace_read and supply its sha256_prefix to replace it.")
        return original, digest, arguments["text"].encode("utf-8")

    if original is None:
        raise _EditConflict("This edit requires an existing workspace file.")
    try:
        old = original.decode("utf-8")
    except UnicodeDecodeError:
        raise _EditConflict("This edit requires a valid UTF-8 workspace file.") from None
    if tool == "workspace_append":
        if "expected_suffix" in arguments and not old.endswith(arguments["expected_suffix"]):
            raise _EditConflict("The file does not end with expected_suffix. Read the current ending and retry.")
        new = old + arguments["text"]
    else:
        find = arguments["find"]
        index = old.find(find)
        if index == -1:
            raise _EditConflict("The find text does not occur in this file. Read the current text and retry.")
        if old.find(find, index + 1) != -1:
            raise _EditConflict("The find text occurs more than once. Include enough surrounding text for a unique match.")
        new = old[:index] + arguments["replace"] + old[index + len(find):]
    data = new.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise _EditConflict(f"The edit would exceed the workspace file limit of {MAX_BYTES} bytes.")
    return original, digest, data


def _file_revision(data):
    digest = hashlib.sha256(data).hexdigest()
    return {"sha256": digest, "sha256_prefix": digest[:MIN_DIGEST_PREFIX], "size_bytes": len(data)}


def _write(parent, name, data, *, replacing):
    temp, kwargs = _at(parent, TMP_PREFIX + secrets.token_hex(16))
    target, _ = _at(parent, name)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(temp, flags, 0o600, **kwargs)
    directories = {} if isinstance(parent, Path) else {"src_dir_fd": parent, "dst_dir_fd": parent}
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if replacing:
            os.replace(temp, target, **directories)
        elif os.name == "nt":
            os.rename(temp, target)  # Windows fails if destination already exists.
        else:
            # Linux RENAME_NOREPLACE publishes in a single namespace operation,
            # including when a human editor creates the target during this save.
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.renameat2(parent, os.fsencode(temp), parent, os.fsencode(target), 1):
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        if isinstance(parent, int):
            os.fsync(parent)
    finally:
        try:
            os.unlink(temp, **kwargs)
        except FileNotFoundError:
            pass
    return _file_revision(data)


def _checked_parts(name, arguments):
    spec = next((t["inputSchema"] for t in WORKSPACE_TOOLS if t["name"] == name), None)
    if (spec is None or not isinstance(arguments, dict) or set(arguments) - spec["properties"].keys()
            or not set(spec["required"]) <= arguments.keys()):
        raise ValueError("Invalid workspace tool or arguments.")
    return _parts(arguments.get("path", "."), allow_root=name == "workspace_list")


def _check_edit_values(arguments):
    for field in ("text", "expected_suffix", "find", "replace"):
        if field not in arguments:
            continue
        value = arguments[field]
        if not isinstance(value, str) or len(value) > MAX_TEXT:
            raise ValueError(f"Supply {field} as a string of up to {MAX_TEXT} characters.")
        if field in ("expected_suffix", "find") and not value:
            raise ValueError(f"Supply nonempty {field} text.")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(f"Supply valid UTF-8 text for {field}.") from None
    return normalize_digest_prefix(arguments["expected_sha256"]) if "expected_sha256" in arguments else None


def workspace_edit_evidence(directory: Path, arguments: dict, name: str = "workspace_write", *, proposal_key=None) -> dict:
    """Identify the full base digest for review and for the executor's revision check."""
    try:
        if name not in WORKSPACE_EDITS:
            raise ValueError("Unknown workspace edit.")
        parts = _checked_parts(name, arguments)
        try:
            expected = _check_edit_values(arguments)
        except ValueError as exc:
            return {"status": "unavailable", "complete": False, "reason": str(exc)}
        with _parent(directory, parts[:-1], preview=True) as parent:
            original, base_digest, data = _prepare_edit(parent, parts[-1], name, arguments, expected)
    except _EditConflict as exc:
        return {"status": "unavailable", "complete": False, "reason": str(exc)}
    except (OSError, ValueError, RuntimeError):
        return {"status": "unavailable", "complete": False,
                "reason": "Could not verify a safe UTF-8 workspace revision for this proposal."}
    try:
        old = original.decode("utf-8") if original is not None else None
    except UnicodeDecodeError:
        return {"status": "unavailable", "complete": False, "base_sha256": base_digest,
                "reason": "The verified base revision is not UTF-8. No text comparison is available."}
    new = data.decode("utf-8")
    if proposal_key is not None and name != "workspace_write":
        # Old denials may identify the equivalent whole-file proposal. The
        # supervisor can fingerprint it locally without sending the old file.
        proposal_key({"path": arguments["path"], "text": new, "expected_sha256": base_digest})
    summary = edit_summary(old, new)
    if not summary["complete"] and name != "workspace_write":
        summary["reason"] = "Edit exceeds the review excerpt limit. The tool arguments contain the requested edit."
    summary["base_sha256"] = base_digest
    summary["proposed_sha256"] = hashlib.sha256(data).hexdigest()
    return summary


def workspace_tool(directory: Path, name: str, arguments: dict) -> dict:
    """Operate exclusively in an operator-provisioned, persistent files/ directory."""
    parts = _checked_parts(name, arguments)
    listing = name == "workspace_list"
    path = "/".join(parts) or "."
    if name in WORKSPACE_EDITS:
        expected = _check_edit_values(arguments)
    else:
        offset = _integer(arguments, "offset", 0, 0)
        limit = _integer(arguments, "limit", 30, 1, 100) if listing else _integer(arguments, "length", 2400, 1, 8000)
    try:
        create_parents = name == "workspace_write" and expected is None
        with _parent(directory, parts if listing else parts[:-1], create=create_parents) as parent:
            if listing:
                with os.scandir(parent) as entries:
                    kept = [e for e in entries if not e.name.casefold().startswith(TMP_PREFIX)]
                    kept.sort(key=lambda e: e.name)
                page = []
                for entry in kept[offset:offset + limit]:
                    item = {"name": entry.name, "directory": entry.is_dir(follow_symlinks=False)}
                    try:
                        entry_path, kwargs = _at(parent, entry.name)
                        info = os.stat(entry_path, follow_symlinks=False, **kwargs)
                    except OSError:
                        info = None
                    item["size_bytes"] = info.st_size if info is not None and _plain_file(info) else None
                    page.append(item)
                return {"path": path, "entries": page, "next_offset": offset + limit if offset + limit < len(kept) else None}
            if name in WORKSPACE_EDITS:
                original, _, data = _prepare_edit(parent, parts[-1], name, arguments, expected)
                return {"path": path, **_write(parent, parts[-1], data, replacing=original is not None)}
            raw = _read(parent, parts[-1])
            content = raw.decode("utf-8")
            if offset > len(content):
                raise ValueError("Offset is past the end of the workspace file.")
            return {"path": path, "text": content[offset:offset + limit], **_file_revision(raw),
                    "next_offset": offset + limit if offset + limit < len(content) else None}
    except _EditConflict as exc:
        return {"path": path, "error": str(exc)}
    except FileNotFoundError:
        return {"path": path, "error": "Workspace file or directory does not exist."}
    except (OSError, UnicodeError) as exc:
        raise ValueError("Workspace operation failed. Check the path, UTF-8 encoding, and file type.") from exc
