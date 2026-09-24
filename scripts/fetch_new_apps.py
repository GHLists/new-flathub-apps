#!/usr/bin/env python3
"""Fetch Flathub apps added between the previous list and now.

New apps are read from the Flathub "recently added" collection, which lists
apps ordered by the time they were added to Flathub. Pages are walked until
the oldest app on a page predates the requested window. The end of the last
list is stored in the manifest so the next run resumes where the previous one
stopped.
"""

import argparse
import csv
import datetime as dt
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

COLLECTION_URL = "https://flathub.org/api/v2/collection/recently-added"
DEFAULT_USER_AGENT = "new-flathub-apps/1.0 (https://github.com/GHLists/new-flathub-apps)"

PAGE_SIZE = 250
MAX_PAGES = 10
SUMMARY_LIMIT = 200
CSV_HEADER = (
    "added_at",
    "app_id",
    "name",
    "developer",
    "license",
    "verified",
    "installs_last_month",
    "favorites",
    "summary",
)

TRANSIENT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    json.JSONDecodeError,
    http.client.HTTPException,
    OSError,
)


def iso(moment):
    moment = moment.astimezone(dt.timezone.utc)
    if moment.microsecond:
        fraction = f"{moment.microsecond:06d}".rstrip("0")
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction}Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def parse_epoch(value):
    """Parse a unix timestamp as returned by the Flathub API."""
    return dt.datetime.fromtimestamp(int(value), dt.timezone.utc)


def timestamp_filename(moment):
    moment = moment.astimezone(dt.timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H-%M-%S")
    if moment.microsecond:
        stamp += "-" + f"{moment.microsecond:06d}".rstrip("0")
    return stamp + "Z"


def fetch_json(url, user_agent, retries=3, backoff=5.0):
    last_error = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except TRANSIENT_ERRORS as error:
            last_error = error
        if attempt < retries:
            print(f"attempt {attempt} failed ({last_error}), retrying", file=sys.stderr)
            time.sleep(backoff * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fetch_new_apps(since, user_agent, retries, max_pages):
    """Return the newest apps, walking pages until ``since`` is covered."""
    apps = []
    for page in range(1, max_pages + 1):
        url = f"{COLLECTION_URL}?page={page}&per_page={PAGE_SIZE}"
        payload = fetch_json(url, user_agent, retries=retries)
        if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
            raise RuntimeError("Flathub response has an invalid hits field")
        batch = payload["hits"]
        apps.extend(batch)
        if len(batch) < PAGE_SIZE:
            return apps, True
        try:
            oldest = min(parse_epoch(app["added_at"]) for app in batch)
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Flathub response contains an invalid added_at") from error
        if oldest <= since:
            return apps, True
    return apps, False


def clean_text(value, limit=SUMMARY_LIMIT):
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def build_row(app, added):
    installs = app.get("installs_last_month")
    favorites = app.get("favorites_count")
    return {
        "added_at": iso(added),
        "app_id": app.get("app_id") or "",
        "name": app.get("name") or "",
        "developer": app.get("developer_name") or "",
        "license": app.get("project_license") or "",
        "verified": "true" if app.get("verification_verified") else "false",
        "installs_last_month": installs if isinstance(installs, int) else "",
        "favorites": favorites if isinstance(favorites, int) else "",
        "summary": clean_text(app.get("summary")),
    }


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def read_manifest_text(path):
    """Read the manifest from disk, or fall back to the committed copy.

    The workflow checks out only ``scripts`` from the repository, so the
    manifest can be missing from the working tree even though it is committed.
    """
    manifest_path = Path(path)
    try:
        return manifest_path.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{manifest_path.as_posix()}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def load_manifest(path):
    text = read_manifest_text(path)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"manifest {path} is not valid JSON") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"manifest {path} must contain a JSON object")
    version = data.get("state_version", 1)
    if version != 1:
        raise RuntimeError(f"manifest {path} has an unsupported state version")
    return data


def save_manifest(path, manifest):
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp")
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, manifest_path)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help="UTC start timestamp as ISO 8601 (default: end of the last list)",
    )
    parser.add_argument(
        "--until",
        help="UTC end timestamp as ISO 8601 (default: now)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=MAX_PAGES,
        help=f"maximum API pages to walk (default: {MAX_PAGES})",
    )
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--manifest", default="latest.json")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=1.0,
        help="window length when no previous list exists (default: 1)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    until = parse_timestamp(args.until) if args.until else now
    manifest = load_manifest(args.manifest)

    if args.since:
        since = parse_timestamp(args.since)
        if "window" in manifest:
            stored_window = parse_timestamp(manifest["window"])
            if since < stored_window:
                raise RuntimeError(
                    "backfill would move the window backwards; "
                    f"the manifest window is {iso(stored_window)}"
                )
    elif "window" in manifest:
        since = parse_timestamp(manifest["window"])
    else:
        since = until - dt.timedelta(hours=args.lookback_hours)

    if since >= until:
        print(f"nothing to do ({iso(since)} >= {iso(until)})", file=sys.stderr)
        return 0

    apps, exhausted = fetch_new_apps(
        since, args.user_agent, args.retries, max(1, args.max_pages)
    )

    rows = []
    seen = set()
    skipped = 0
    for app in apps:
        if not isinstance(app, dict):
            skipped += 1
            continue
        app_id = app.get("app_id")
        if not isinstance(app_id, str) or not app_id:
            skipped += 1
            continue
        try:
            added = parse_epoch(app["added_at"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        if added <= since or added > until:
            continue
        if app_id in seen:
            continue
        seen.add(app_id)
        rows.append(build_row(app, added))
    rows.sort(key=lambda row: row["added_at"])
    if skipped:
        print(f"skipped {skipped} malformed apps", file=sys.stderr)

    manifest["window"] = iso(until)
    manifest["source_truncated"] = not exhausted
    if rows:
        output = Path(args.output_dir) / f"new-apps-{timestamp_filename(until)}.csv"
        write_csv(output, rows)
        manifest["list"] = {
            "path": output.as_posix(),
            "from": iso(since),
            "to": iso(until),
            "count": len(rows),
        }
        print(
            f"wrote {len(rows)} apps added between {iso(since)} "
            f"and {iso(until)} to {output}"
        )
    else:
        print(f"no new apps between {iso(since)} and {iso(until)}")
    if not exhausted:
        print(
            "Flathub page limit reached; the window may be incomplete",
            file=sys.stderr,
        )
    save_manifest(args.manifest, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
