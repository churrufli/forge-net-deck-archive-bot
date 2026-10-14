#!/usr/bin/env python3
import argparse
import collections
import datetime as dt
import io
import json
import os
import re
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path

FORMATS = ["duel-commander", "standard", "modern", "legacy", "vintage", "pauper", "pioneer", "block"]
DEFAULT_BASE_URL = "https://github.com/Card-Forge/forge-extras/raw/refs/heads/main/decks/archive"
JSON_NAME_RE = re.compile(r"^(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?P<id>\d+)\.json$")
ZIP_NAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})-(?:(?P<id>\d+)-)?(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.zip$")
MTGO_HOSTS = ("https://www.mtgo.com/", "https://mtgo.com/", "http://www.mtgo.com/")
INVALID_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
MAX_JSON_BYTES = 20 * 1024 * 1024
MAX_DECKS = 2000
MAX_CARD_LINES = 400
MAX_COUNT = 999


def name_key(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.casefold().split())


class CardChecker:
    def __init__(self, cards_path=None, unsupported_path=None):
        self.known = None
        self.unsupported = set()
        self.companions = set()
        if cards_path:
            self.known = {}
            self._load_cards(Path(cards_path))
            if not self.known:
                raise SystemExit(f"No card scripts found in {cards_path}")
        if unsupported_path:
            self._load_unsupported(Path(unsupported_path))

    def _add_script(self, text):
        faces = []
        mode = None
        companion = False
        for line in text.splitlines():
            if line.startswith("K:Companion"):
                companion = True
            if line.startswith("Name:"):
                faces.append(line[5:].strip())
            elif line.startswith("AlternateMode:"):
                mode = line.split(":", 1)[1].strip()
            elif line.startswith("Variant:") and ":FlavorName:" in line:
                flavor = line.split(":FlavorName:", 1)[1].strip()
                if flavor:
                    self.known.setdefault(name_key(flavor), flavor)
        if not faces:
            return
        if companion:
            self.companions.add(name_key(faces[0]))
        self.known.setdefault(name_key(faces[0]), faces[0])
        if mode == "Split" and len(faces) > 1:
            full = " // ".join(faces[:2])
            self.known.setdefault(name_key(full), full)

    def _load_cards(self, path):
        if path.is_file() and path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    n = info.filename
                    if n.endswith(".txt") and not n.startswith("upcoming/") and "/upcoming/" not in n:
                        self._add_script(zf.read(info).decode("utf-8", "replace"))
            return
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d != "upcoming"]
            for f in files:
                if f.endswith(".txt"):
                    self._add_script((Path(root) / f).read_text(encoding="utf-8", errors="replace"))

    def _load_unsupported(self, path):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line[:1] in (" ", "\t") and line.strip():
                self.unsupported.add(name_key(line.strip()))

    def resolve(self, raw):
        raw = " ".join(raw.split())
        candidates = [raw, raw.replace(" && ", " // ")]
        candidates.append(re.sub(r"\s*/{1,2}\s*", " // ", candidates[1]))
        if self.known is None:
            chosen = candidates[1]
            if ":" not in chosen and re.fullmatch(r"[^/]+\S/\S[^/]+", chosen):
                chosen = candidates[2]
        else:
            chosen = None
            for c in candidates:
                hit = self.known.get(name_key(c))
                if hit:
                    chosen = hit
                    break
            if chosen is None:
                return None, "not in Forge"
        for c in candidates + [chosen]:
            if name_key(c) in self.unsupported:
                return None, "unsupported"
        return chosen, None


def clean_text(value, fallback):
    s = INVALID_FILE_CHARS.sub("", str(value if value is not None else ""))
    s = " ".join(s.split()).strip(" .")
    return s[:80] or fallback


def card_lines(entries, checker, problems):
    if entries is None:
        return []
    if not isinstance(entries, list) or len(entries) > MAX_CARD_LINES:
        raise ValueError("bad card list")
    out = []
    for e in entries:
        if not isinstance(e, dict):
            raise ValueError("bad card entry")
        count = e.get("Count")
        name = e.get("CardName")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_COUNT:
            raise ValueError("bad count")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("bad card name")
        if any(ch in name for ch in "\r\n|[]") or any(unicodedata.category(ch) == "Cc" for ch in name):
            raise ValueError("bad card name")
        resolved, why = checker.resolve(name)
        if resolved is None:
            problems.append((name.strip(), why))
            continue
        out.append(f"{count} {resolved}")
    return out


def line_count(lines):
    return sum(int(line.split(" ", 1)[0]) for line in lines)


def split_commanders(main, side, checker):
    commanders = list(side)
    extra = []
    for line in list(commanders):
        if line_count(main) + line_count(commanders) <= 100:
            break
        if name_key(line.split(" ", 1)[1]) in checker.companions:
            commanders.remove(line)
            extra.append(line)
    if not 1 <= line_count(commanders) <= 2 or line_count(main) + line_count(commanders) != 100:
        return None, None
    return commanders, extra


def build_deck(index, deck, checker, commander=False):
    player = clean_text(deck.get("Player"), "Unknown")
    result = clean_text(deck.get("Result"), "")
    title = f"{index:02d} {player} {result}".strip()
    problems = []
    main = card_lines(deck.get("Mainboard"), checker, problems)
    side = card_lines(deck.get("Sideboard"), checker, problems)
    if problems:
        return title, None, problems
    if not main:
        return title, None, [("(empty mainboard)", "invalid")]
    lines = ["[metadata]", f"Name={title}", "[Main]", *main]
    if commander:
        commanders, extra = split_commanders(main, side, checker)
        if commanders is None:
            return title, None, [("(not a 100-card deck with 1-2 commanders)", "invalid")]
        lines += ["[commander]", *commanders]
        if extra:
            lines += ["[sideboard]", *extra]
    else:
        lines += ["[sideboard]", *side]
    return title, "\r\n".join(lines) + "\r\n", []


def detect_format(slug, formats):
    for f in FORMATS:
        if f in formats and re.search(rf"(^|-){re.escape(f)}(-|$)", slug):
            return f
    return None


def is_league(slug):
    return slug.endswith("league")


def scan_archive(archive, fmt):
    names = set()
    challenge_ids = set()
    latest = None
    d = archive / fmt
    if d.is_dir():
        for p in d.iterdir():
            m = ZIP_NAME_RE.match(p.name)
            if not m:
                continue
            names.add(p.name)
            if m["id"] and not is_league(m["slug"]):
                challenge_ids.add((m["slug"], m["id"]))
            if latest is None or m["date"] > latest:
                latest = m["date"]
    return names, challenge_ids, latest


def iter_cache(cache_root, since, until):
    tdir = cache_root / "Tournaments"
    if not tdir.is_dir():
        raise SystemExit(f"{tdir} not found")
    for source in sorted(p for p in tdir.iterdir() if p.is_dir()):
        for ydir in sorted(p for p in source.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}", p.name)):
            if ydir.name < since[:4] or ydir.name > until[:4]:
                continue
            for mdir in sorted(p for p in ydir.iterdir() if p.is_dir() and re.fullmatch(r"\d{2}", p.name)):
                for ddir in sorted(p for p in mdir.iterdir() if p.is_dir() and re.fullmatch(r"\d{2}", p.name)):
                    day = f"{ydir.name}-{mdir.name}-{ddir.name}"
                    if day < since or day > until:
                        continue
                    for f in sorted(ddir.iterdir()):
                        if f.is_file() and not f.is_symlink() and f.suffix == ".json":
                            yield source.name, f


def write_zip(target, decks, date):
    stamp = tuple(int(x) for x in date.split("-")) + (12, 0, 0)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for title, content in decks:
            info = zipfile.ZipInfo(f"{title}.dck", date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, content.encode("utf-8"))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".zip")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(buf.getvalue())
        if target.exists():
            raise FileExistsError(target)
        os.link(tmp, target)
    finally:
        os.unlink(tmp)


def cmd_convert(args):
    cache = Path(args.cache).resolve()
    archive = Path(args.archive).resolve()
    formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    for f in formats:
        if f not in FORMATS:
            raise SystemExit(f"Unknown format: {f}")
    unsupported = args.unsupported
    if unsupported is None and Path("unsupportedcards.txt").is_file():
        unsupported = "unsupportedcards.txt"
    if not args.forge_cards and not unsupported:
        raise SystemExit("Provide --forge-cards and/or --unsupported (or unsupportedcards.txt in the current directory)")
    checker = CardChecker(args.forge_cards, unsupported)

    today = dt.datetime.now(dt.timezone.utc).date()
    until = args.until or (today - dt.timedelta(days=args.settle_days)).isoformat()
    state = {}
    since_all = None
    for f in formats:
        names, cids, latest = scan_archive(archive, f)
        if args.since and args.since != "auto":
            since = args.since
        elif latest:
            since = (dt.date.fromisoformat(latest) - dt.timedelta(days=args.lookback_days)).isoformat()
        else:
            raise SystemExit(f"No existing archive for {f}; pass --since YYYY-MM-DD")
        state[f] = {"names": names, "cids": cids, "since": since}
        since_all = since if since_all is None or since < since_all else since_all
    print(f"Range: {since_all} .. {until}", file=sys.stderr)

    planned = {}
    stats = collections.Counter()
    rejected_cards = collections.Counter()
    skipped = []
    for source, path in iter_cache(cache, since_all, until):
        m = JSON_NAME_RE.match(path.name)
        if not m:
            continue
        slug, ev_id = m["slug"], m["id"]
        date = f'{m["y"]}-{m["m"]}-{m["d"]}'
        fmt = detect_format(slug, formats)
        if not fmt or date < state[fmt]["since"] or date > until:
            continue
        zip_name = f"{date}-{ev_id}-{slug}.zip"
        st = state[fmt]
        if zip_name in st["names"]:
            stats["exists"] += 1
            continue
        if not is_league(slug) and (slug, ev_id) in st["cids"] and (fmt, zip_name) not in planned:
            stats["duplicate"] += 1
            continue
        if path.stat().st_size > MAX_JSON_BYTES:
            skipped.append((zip_name, "json too large"))
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            uri = str((data.get("Tournament") or {}).get("Uri") or "")
            if not source.startswith("mtgo.com") and not uri.startswith(MTGO_HOSTS):
                continue
            decks = data.get("Decks")
            if not isinstance(decks, list) or len(decks) > MAX_DECKS:
                raise ValueError("bad decks list")
            built = []
            for deck in decks:
                if not isinstance(deck, dict):
                    raise ValueError("bad deck")
                title, content, problems = build_deck(len(built) + 1, deck, checker, fmt == "duel-commander")
                if content is None:
                    stats["decks_rejected"] += 1
                    for card, why in set(problems):
                        rejected_cards[(card, why)] += 1
                    continue
                built.append((title, content))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
            skipped.append((zip_name, f"invalid data: {e}"))
            continue
        if len(built) < args.min_decks:
            if (fmt, zip_name) not in planned:
                skipped.append((zip_name, f"{len(built)} valid decks (minimum {args.min_decks})"))
            continue
        titles = [t for t, _ in built]
        if len(set(titles)) != len(titles):
            skipped.append((zip_name, "duplicate deck names"))
            continue
        key = (fmt, zip_name)
        if key in planned:
            if len(built) <= len(planned[key][3]):
                stats["duplicate"] += 1
                continue
            stats["decks_written"] -= len(planned[key][3])
        planned[key] = (fmt, zip_name, date, built)
        if not is_league(slug):
            st["cids"].add((slug, ev_id))
        stats["decks_written"] += len(built)

    planned_names = {z for _, z in planned}
    best = {}
    for z, why in skipped:
        if z in planned_names:
            continue
        m = re.match(r"(\d+) valid decks", why)
        rank = int(m.group(1)) if m else -1
        if z not in best or rank > best[z][0]:
            best[z] = (rank, why)
    skipped = [(z, why) for z, (_, why) in best.items()]
    stats["small"] = sum(1 for _, why in skipped if "minimum" in why)
    if len(planned) > args.max_new:
        raise SystemExit(f"Refusing to write {len(planned)} archives (limit {args.max_new}); raise --max-new if this is expected")

    per_format = collections.Counter()
    for fmt, zip_name, date, built in sorted(planned.values(), key=lambda x: (x[0], x[1])):
        per_format[fmt] += 1
        if not args.dry_run:
            write_zip(archive / fmt / zip_name, built, date)
        print(f"{'would add' if args.dry_run else 'added'} {fmt}/{zip_name} ({len(built)} decks)")

    report = [f"## Deck archive update ({since_all} .. {until})", ""]
    report.append(f"New archives: **{len(planned)}**, decks: **{stats['decks_written']}**, decks rejected: **{stats['decks_rejected']}**, tournaments under {args.min_decks} decks skipped: **{stats['small']}**")
    report.append("")
    if per_format:
        report += ["| Format | New archives |", "|---|---|"]
        report += [f"| {f} | {n} |" for f, n in sorted(per_format.items())]
        report.append("")
    if planned:
        rows = sorted(planned.values(), key=lambda x: (x[0], x[1]))
        report += [f"<details><summary>New tournaments ({len(rows)})</summary>", ""]
        report += [f"- `{f}/{z}` ({len(b)} decks)" for f, z, _, b in rows[:300]]
        if len(rows) > 300:
            report.append(f"- ... and {len(rows) - 300} more")
        report += ["", "</details>", ""]
    if rejected_cards:
        report += ["<details><summary>Cards that caused rejected decks</summary>", "", "| Card | Reason | Decks |", "|---|---|---|"]
        for (card, why), n in rejected_cards.most_common(100):
            safe = card.replace("|", "\\|").replace("<", "&lt;")
            report.append(f"| {safe} | {why} | {n} |")
        report += ["", "</details>", ""]
    if skipped:
        report += [f"<details><summary>Skipped tournaments ({len(skipped)})</summary>", ""]
        report += [f"- `{z}`: {why}" for z, why in skipped[:200]]
        report += ["", "</details>", ""]
    text = "\n".join(report)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    print(text, file=sys.stderr)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"new_archives={len(planned)}\n")
    return 0


def list_title(m, decks):
    words = " ".join(w.capitalize() for w in m["slug"].split("-"))
    ident = f'{m["id"]} ' if m["id"] else ""
    return f'{m["date"]} {ident}{words} ({decks} decks)'


def cmd_lists(args):
    archive = Path(args.archive).resolve()
    lists_dir = Path(args.lists).resolve()
    base = args.base_url.rstrip("/")
    changed = 0
    for list_file in sorted(lists_dir.glob("net-decks-archive-*.txt")):
        fmt = list_file.stem[len("net-decks-archive-"):]
        zdir = archive / fmt
        if not zdir.is_dir():
            continue
        text = list_file.read_text(encoding="utf-8")
        lines = text.splitlines()
        listed = set()
        last_date = ""
        for line in lines:
            if "|" not in line:
                continue
            url = line.split("|", 1)[1].strip()
            listed.add(url.rsplit("/", 1)[-1])
            d = line[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d > last_date:
                last_date = d
        window_start = ""
        if last_date:
            window_start = (dt.date.fromisoformat(last_date) - dt.timedelta(days=args.relist_days)).isoformat()
        new = []
        for p in sorted(zdir.iterdir(), key=lambda p: p.name):
            m = ZIP_NAME_RE.match(p.name)
            if not m or p.name in listed or m["date"] < window_start:
                continue
            with zipfile.ZipFile(p) as zf:
                n = sum(1 for i in zf.infolist() if not i.is_dir() and i.filename.endswith(".dck"))
            if n >= args.min_decks:
                new.append((p.name, f"{list_title(m, n)} | {base}/{fmt}/{p.name}"))
        if not new:
            continue
        changed += len(new)
        merged = list(lines)
        for key, line in new:
            idx = len(merged)
            for i, existing in enumerate(merged):
                if "|" in existing and existing.split("|", 1)[1].strip().rsplit("/", 1)[-1] > key:
                    idx = i
                    break
            merged.insert(idx, line)
        body = "\n".join(merged)
        if text.endswith("\n"):
            body += "\n"
        if not args.dry_run:
            list_file.write_text(body, encoding="utf-8", newline="\n")
        print(f"{list_file.name}: +{len(new)}")
    print(f"List entries added: {changed}", file=sys.stderr)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Convert MTGODecklistCache tournaments into Forge deck archives")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="create new zip archives from MTGODecklistCache JSON")
    c.add_argument("--cache", required=True, help="MTGODecklistCache checkout")
    c.add_argument("--archive", required=True, help="forge-extras decks/archive directory")
    c.add_argument("--forge-cards", help="Forge res/cardsfolder directory or cardsfolder.zip")
    c.add_argument("--unsupported", help="unsupportedcards.txt (default: ./unsupportedcards.txt if present)")
    c.add_argument("--formats", default=",".join(FORMATS))
    c.add_argument("--since", default="auto", help="YYYY-MM-DD or auto (latest archived date minus --lookback-days)")
    c.add_argument("--until", help="YYYY-MM-DD (default: today minus --settle-days)")
    c.add_argument("--lookback-days", type=int, default=7)
    c.add_argument("--settle-days", type=int, default=2)
    c.add_argument("--max-new", type=int, default=500)
    c.add_argument("--min-decks", type=int, default=5, help="skip tournaments with fewer valid decks")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--report")
    c.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    c.set_defaults(func=cmd_convert)

    l = sub.add_parser("lists", help="append new archives to net-decks-archive-*.txt")
    l.add_argument("--archive", required=True)
    l.add_argument("--lists", required=True, help="directory with net-decks-archive-*.txt")
    l.add_argument("--base-url", default=DEFAULT_BASE_URL)
    l.add_argument("--min-decks", type=int, default=5, help="do not list archives with fewer decks")
    l.add_argument("--relist-days", type=int, default=14, help="also list unlisted archives up to this many days older than the newest list entry")
    l.add_argument("--dry-run", action="store_true")
    l.set_defaults(func=cmd_lists)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
