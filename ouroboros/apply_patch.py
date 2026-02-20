"""
Apply-patch shim for Claude Code CLI.
Writes apply_patch script to /usr/local/bin/ on import.

Supports: *** Update File, *** Add File, *** Delete File, *** End of File.
"""
import pathlib


APPLY_PATCH_PATH = pathlib.Path("/home/alexroll/.local/bin/apply_patch")
APPLY_PATCH_CODE = r"""#!/usr/bin/env python3
import os
import sys
import pathlib

def _norm_line(l: str) -> str:
    if l.startswith(" "):
        return l[1:]
    return l

def _find_subseq(hay, needle):
    if not needle:
        return 0
    n = len(needle)
    for i in range(0, len(hay) - n + 1):
        ok = True
        for j in range(n):
            if hay[i + j] != needle[j]:
                ok = False
                break
        if ok:
            return i
    return -1

def _find_subseq_rstrip(hay, needle):
    if not needle:
        return 0
    hay2 = [x.rstrip() for x in hay]
    needle2 = [x.rstrip() for x in needle]
    return _find_subseq(hay2, needle2)

def apply_update_file(path: str, hunks: list[list[str]]):
    p = pathlib.Path(path)
    if not p.exists():
        sys.stderr.write(f"apply_patch: file not found: {path}\n")
        sys.exit(2)

    text = p.read_text(encoding="utf-8")
    src = text.splitlines()

    for hunk in hunks:
        old_seq = []
        new_seq = []
        for line in hunk:
            if line.startswith("+"):
                new_seq.append(line[1:])
            elif line.startswith("-"):
                old_seq.append(line[1:])
            else:
                c = _norm_line(line)
                old_seq.append(c)
                new_seq.append(c)

        idx = _find_subseq(src, old_seq)
        if idx < 0:
            idx = _find_subseq_rstrip(src, old_seq)
        if idx < 0:
            sys.stderr.write("apply_patch: failed to match hunk in file: " + path + "\n")
            sys.stderr.write("HUNK (old_seq):\n" + "\n".join(old_seq) + "\n")
            sys.exit(3)

        src = src[:idx] + new_seq + src[idx + len(old_seq):]

    p.write_text("\n".join(src) + "\n", encoding="utf-8")

def apply_add_file(path: str, content_lines: list[str]):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(content_lines) + "\n", encoding="utf-8")

def apply_delete_file(path: str):
    p = pathlib.Path(path)
    if p.exists():
        p.unlink()
    else:
        sys.stderr.write(f"apply_patch: delete target not found (ignored): {path}\n")

def _is_action_boundary(line: str) -> bool:
    return line.startswith("*** ") and any(
        line.startswith(p) for p in (
            "*** Update File:", "*** Add File:", "*** Delete File:",
            "*** End Patch", "*** End of File",
        )
    )

def main():
    lines = sys.stdin.read().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("*** Begin Patch"):
            i += 1
            continue

        if line.startswith("*** Update File:"):
            path = line.split(":", 1)[1].strip()
            i += 1

            hunks = []
            cur = []
            while i < len(lines) and not _is_action_boundary(lines[i]):
                if lines[i].startswith("@@"):
                    if cur:
                        hunks.append(cur)
                        cur = []
                    i += 1
                    continue
                cur.append(lines[i])
                i += 1
            if cur:
                hunks.append(cur)
            # Skip optional *** End of File marker
            if i < len(lines) and lines[i].startswith("*** End of File"):
                i += 1

            apply_update_file(path, hunks)
            continue

        if line.startswith("*** Add File:"):
            path = line.split(":", 1)[1].strip()
            i += 1

            content_lines = []
            while i < len(lines) and not _is_action_boundary(lines[i]):
                l = lines[i]
                if l.startswith("+"):
                    content_lines.append(l[1:])
                elif l.strip():  # non-empty, non-+ line — treat as content
                    content_lines.append(l)
                i += 1
            # Skip optional *** End of File marker
            if i < len(lines) and lines[i].startswith("*** End of File"):
                i += 1

            apply_add_file(path, content_lines)
            continue

        if line.startswith("*** Delete File:"):
            path = line.split(":", 1)[1].strip()
            i += 1
            apply_delete_file(path)
            continue

        if line.startswith("*** End Patch"):
            i += 1
            continue

        if line.startswith("*** End of File"):
            i += 1
            continue

        if line.startswith("***"):
            sys.stderr.write(f"apply_patch: unknown directive: {line}\n")
            sys.exit(4)

        i += 1

if __name__ == "__main__":
    main()
"""


def install():
    """Install apply_patch script to /usr/local/bin/."""
    APPLY_PATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    APPLY_PATCH_PATH.write_text(APPLY_PATCH_CODE, encoding="utf-8")
    APPLY_PATCH_PATH.chmod(0o755)
