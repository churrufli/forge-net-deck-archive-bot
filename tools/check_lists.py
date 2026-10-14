import re
import subprocess
import sys
from pathlib import Path

LINE = r"^\d{{4}}-\d{{2}}-\d{{2}} [^|\r\n]+ \(\d+ decks\) \| https://github\.com/Card-Forge/forge-extras/raw/refs/heads/main/decks/archive/{fmt}/[A-Za-z0-9-]+\.zip$"


def check(old_text, new_text, fmt):
    old = old_text.splitlines()
    new = new_text.splitlines()
    pattern = re.compile(LINE.format(fmt=re.escape(fmt)))
    i = 0
    added = 0
    for line in new:
        if i < len(old) and line == old[i]:
            i += 1
        else:
            if not pattern.match(line):
                return None, f"unexpected line {line!r}"
            added += 1
    if i != len(old):
        return None, "an existing line was changed, removed or reordered"
    return added, None


def main():
    forge = Path(sys.argv[1])
    names = subprocess.run(["git", "-C", str(forge), "diff", "--name-only"], check=True, capture_output=True, text=True).stdout.split()
    for rel in names:
        m = re.fullmatch(r"forge-gui/res/lists/net-decks-archive-([a-z-]+)\.txt", rel)
        if not m:
            sys.exit(f"unexpected changed file {rel}")
        old = subprocess.run(["git", "-C", str(forge), "show", f"HEAD:{rel}"], check=True, capture_output=True, text=True).stdout
        new = (forge / rel).read_text(encoding="utf-8")
        added, err = check(old, new, m.group(1))
        if err:
            sys.exit(f"{rel}: {err}")
        print(f"{rel}: +{added}")


if __name__ == "__main__":
    main()
