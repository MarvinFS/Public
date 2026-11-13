"""Read an append-only JSONL file a little at a time.

Claude Code and Codex only ever add lines to a transcript. A refresh that finds one grown
by a few kilobytes needs those kilobytes, not the other 600 MB, so a parser keeps what it
made of a file and asks for the lines the file gained since.

A file counts as unchanged (and is not opened) when its size and mtime are the ones seen
last time. os.stat is exact for a file another process holds open for writing; the size
and time a directory listing carries are not (measured: a held-open file's listed size
lagged the real one), so the listing is never used for this.
"""

import hashlib
import os
from pathlib import Path
from typing import Any

# A file that is larger than before can still be a different file (replaced by a longer one).
# So the first and the last of the bytes already read are remembered, as a digest, and must
# still be there for the rest to be taken as new. 4 KB is longer than most lines of either
# writer, so two different files do not agree on both ends.
_ANCHOR_BYTES = 4096
_READ_BUFFER = 1 << 20


class TailedFile:
    """What one append-only JSONL file has said so far, kept between refreshes.

    A subclass decides what to make of the lines: `reset` drops that, `add_line` takes one
    complete line, and `peek_line` says what an unterminated last line would add. A last line
    with no newline is not counted as read: the writer may be half way through it, so it is
    only looked at (`tail`) and read again from its start on the next change, which is also
    what a full parse of the file as it stands would make of it.

    Lines end at "\\n": both writers terminate every record with one.
    """

    def __init__(self) -> None:
        self.version = 0        # counts the reads that took anything in, so a merge can tell
        self._start_over()

    # --- what a subclass provides -------------------------------------------------
    def reset(self) -> None:
        """Drop everything made of the lines so far."""

    def add_line(self, line: str) -> None:
        raise NotImplementedError

    def peek_line(self, line: str) -> Any:
        """What the unterminated last line would add, without adding it."""
        raise NotImplementedError

    # --- the reading ----------------------------------------------------------------
    def _start_over(self) -> None:
        self.size = -1          # bytes of the file seen, or -1 when nothing below can be trusted
        self.mtime_ns = 0
        self.offset = 0         # end of the last complete line taken in
        self.anchor = b""       # digest of the first and last bytes before the offset
        self.tail: Any = None   # peek_line's answer for an unterminated last line
        self.reset()

    def refresh(self, path: Path) -> bool:
        """Bring this up to date with the file; True when the file was opened.

        Raises OSError when the file cannot be read, after which the next call starts over.
        """
        st = os.stat(path)
        if st.st_size == self.size and st.st_mtime_ns == self.mtime_ns:
            return False
        try:
            with open(path, "rb", buffering=_READ_BUFFER) as f:
                if self.size < 0 or st.st_size < self.size or not self._anchor_holds(f):
                    self._start_over()      # shrunk, replaced, or never read
                self._read_new_lines(f)
            self.mtime_ns = st.st_mtime_ns
        except BaseException:
            # Lines already added would be added again by the next call.
            self._start_over()
            raise
        self.version += 1
        return True

    def _anchor_holds(self, f) -> bool:
        return not self.offset or _digest_ends(f, self.offset) == self.anchor

    def _read_new_lines(self, f) -> None:
        f.seek(self.offset)
        pos = size = self.offset
        self.tail = None
        for raw in f:
            line = raw.decode("utf-8", errors="ignore")
            if raw.endswith(b"\n"):
                self.add_line(line)
                pos = size = pos + len(raw)
            else:
                self.tail = self.peek_line(line)
                size = pos + len(raw)
                # The writer is half way through this line, and a buffered file has no sticky
                # end of file: if it finishes the line now, the next chunk is the rest of it,
                # which is not a line. The next refresh reads it whole, from `pos`.
                break
        self.offset, self.size = pos, size
        self.anchor = _digest_ends(f, pos) if pos else b""


def _digest_ends(f, offset: int) -> bytes:
    """A digest of the first and the last _ANCHOR_BYTES of the first `offset` bytes of `f`."""
    f.seek(0)
    ends = f.read(min(_ANCHOR_BYTES, offset))
    last = max(0, offset - _ANCHOR_BYTES)
    f.seek(last)
    return hashlib.blake2b(ends + f.read(offset - last), digest_size=16).digest()
