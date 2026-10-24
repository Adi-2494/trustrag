"""Fail if any source file contains invisible or bidirectional-override characters ("Trojan Source").
Such characters can make code display differently from how it runs; write them as \\u escapes instead."""

import pathlib
import sys

FORBIDDEN = set(range(0x200B, 0x2010)) | set(range(0x202A, 0x202F)) | set(range(0x2060, 0x2065)) | {0xFEFF, 0x00AD}
SUFFIXES = {".py", ".toml", ".yml", ".yaml", ".md", ".txt", ".cfg", ".json"}


def main(paths: list[str]) -> int:
    files = [pathlib.Path(p) for p in paths] or [
        p
        for p in pathlib.Path(".").rglob("*")
        if p.suffix in SUFFIXES and ".venv" not in p.parts and ".git" not in p.parts
    ]
    bad = 0
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            for ch in line:
                if ord(ch) in FORBIDDEN:
                    print(f"{path}:{number}: invisible character U+{ord(ch):04X}; write it as \\u{ord(ch):04x}")
                    bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
