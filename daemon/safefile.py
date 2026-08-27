"""Descriptor-first file helpers, shared by the daemon and jarvis-config.

Every path Jarvis touches is predictable -- ~/.config/jarvis/config.toml, the
voices directory, an agent's reply file -- and two of the readers are
long-lived (the always-on listener and the settings panel behind the bar
widget). A plain `open(path)` on a predictable path is three separate hazards:

  * a symlink planted at that name redirects the open somewhere else,
  * a FIFO planted at that name blocks the open, or the read after it,
    forever -- which stalls the UI or the service rather than failing it,
  * an oversized file is read wholly into memory.

So nothing here opens a path by name and trusts what comes back. Every read
opens with O_NONBLOCK (opening a FIFO returns immediately instead of waiting
for a writer), checks the *descriptor* with fstat before reading a byte --
regular file, expected owner -- and reads a bounded number of bytes. Reads
also refuse a symlink at the final component (O_NOFOLLOW) unless the caller
opts out, which only the .desktop scan does, where a user's symlink is
ordinary and the descriptor checks still apply. Every write goes to an unpredictable private
temporary file in the destination directory and is moved into place with
os.replace, so a reader sees either the old file or the new one and never a
half-written one, and there is no guessable `.tmp` name to pre-plant.

O_NOFOLLOW only covers the last path component; the parent directories here
are the user's own $XDG_* dirs, created 0700 where we create them.
"""

import contextlib
import errno
import os
import stat
import tempfile

# Enough for a hand-edited config with comments, far below anything that
# would hurt to hold in memory.
MAX_CONFIG_BYTES = 1 << 20        # 1 MiB
MAX_TEXT_BYTES = 1 << 20


class UnsafeFile(OSError):
    """A path resolved to something we will not read or write."""


def open_ro(path, allow_symlink=False, owners=None):
    """Open `path` read-only, or raise. Returns a file descriptor.

    Never blocks on a FIFO, and confirms via fstat on the descriptor we
    actually got that it is a regular file with an acceptable owner --
    ourselves by default, `owners` (a set of uids) where something
    system-owned is legitimate, such as /usr/share/applications.

    `allow_symlink` drops O_NOFOLLOW. Only for read paths where a symlink is
    a normal thing for the user to have made: the redirect hazards that
    matter -- a FIFO that never returns, a device, a huge file -- are caught
    by the descriptor checks and the read ceiling regardless of how we got
    there. Every *write* path keeps O_NOFOLLOW.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
    if not allow_symlink:
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        # O_NOFOLLOW on a symlink reports ELOOP; say what actually happened.
        if exc.errno == errno.ELOOP:
            raise UnsafeFile(f"{path} is a symlink") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeFile(f"{path} is not a regular file")
        if st.st_uid not in (owners or {os.geteuid()}):
            raise UnsafeFile(f"{path} has an unexpected owner (uid {st.st_uid})")
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_bytes(path, limit=MAX_TEXT_BYTES, **kwargs):
    """Whole file as bytes, refusing anything over `limit`."""
    with os.fdopen(open_ro(path, **kwargs), "rb") as fh:
        # One byte past the ceiling, so "exactly at the limit" still reads
        # and anything larger is refused rather than silently truncated.
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise UnsafeFile(f"{path} is larger than {limit} bytes")
    return data


def read_text(path, limit=MAX_TEXT_BYTES, encoding="utf-8", **kwargs):
    return read_bytes(path, limit, **kwargs).decode(encoding, "replace")


@contextlib.contextmanager
def private_temp(path, suffix=".tmp"):
    """A private, unpredictably named temp file beside `path`.

    Yields (fd, tmp_path). On any exception the temp file is removed; on
    success the caller is expected to os.replace it into position -- or to
    call `commit` below, which does that and the mode fixup together.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    # mkstemp creates with O_EXCL at mode 0600 under a random name, so there
    # is nothing to pre-plant and no window where the file is world-readable.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".jarvis-", suffix=suffix)
    try:
        yield fd, tmp
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def commit(tmp, path, mode=0o600):
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_atomic(path, data, mode=0o600):
    """Replace `path` with `data`, atomically, via a private temp file."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    with private_temp(path) as (fd, tmp):
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        commit(tmp, path, mode)


def open_w_nofollow(path, mode=0o600):
    """Open `path` for writing without following a symlink at the last
    component. For append-style sinks we own outright (a log), where the
    atomic-replace dance would be pointless."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
    return os.fdopen(os.open(path, flags, mode), "w")
