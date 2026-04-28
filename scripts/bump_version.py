"""
Bump the patch version in soundboard/__init__.py.
Prints the new version string to stdout.
Run from the workspace root.
"""

import re
import sys
from pathlib import Path

INIT_FILE = Path("soundboard/__init__.py")

if not INIT_FILE.exists():
    print(f"ERROR: {INIT_FILE} not found. Run from workspace root.", file=sys.stderr)
    sys.exit(1)

content = INIT_FILE.read_text(encoding="utf-8")

match = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', content)
if not match:
    print("ERROR: __version__ not found in soundboard/__init__.py", file=sys.stderr)
    sys.exit(1)

major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
new_version = f"{major}.{minor}.{patch + 1}"

new_content = re.sub(
    r'__version__\s*=\s*"\d+\.\d+\.\d+"',
    f'__version__ = "{new_version}"',
    content,
)

INIT_FILE.write_text(new_content, encoding="utf-8")
print(new_version)
