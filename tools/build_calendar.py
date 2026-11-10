"""Fetch the family calendars and write the encrypted data file for Glance Digital Wall Calendar.

Run by the scheduled GitHub job (.github/workflows/calendar.yml). It reads two
secrets from the environment:

  BOARD_SETTINGS  the text of private/settings.txt: location and one line per
                  calendar (name | color | private iCal address)
  BOARD_KEY       the key the data file is locked with

and writes calendar.enc: the next two weeks of events, trimmed to day, time,
title and which calendar, then encrypted. The repository is public, so what
lands in it must be unreadable without the key. The key lives only in the
address the TV opens and in the repository's secrets.

This script must never print an event title, a calendar address or the key:
the job's log is public. It prints counts and nothing else.

    python build_calendar.py --out calendar.enc [--old previous.enc]

For testing, a calendar "address" may be a path to a local .ics file.
"""
import os, re, sys, json, base64, argparse, datetime, urllib.request
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DAYS_AHEAD = 14          # today plus this many days
REFRESH_HOURS = 20       # rewrite at least this often, so the board can tell
                         # "nothing changed" from "the job has stopped"


def parse_settings(text):
    out = {"lat": None, "lon": None, "place": "", "tz": "America/Chicago", "calendars": [],
           "albums": []}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = [p.strip() for p in line.split("=", 1)]
        key = key.lower()
        if key == "location" and val:
            parts = [p.strip() for p in val.split(",", 2)]
            out["lat"], out["lon"] = float(parts[0]), float(parts[1])
            out["place"] = parts[2] if len(parts) > 2 else ""
        elif key == "timezone" and val:
            out["tz"] = val
        elif key == "photos" and val:
            # one line per album:  link | name | tag  (name and tag optional)
            parts = [p.strip() for p in val.split("|")]
            if parts[0]:
                out["albums"].append({"link": parts[0], "name": parts[1] if len(parts) > 1 else "",
                                      "id": parts[2] if len(parts) > 2 else ""})
        elif key == "calendar":
            # name | color | address | tag. The tag is a short random label the
            # layout uses to say which calendars a module shows; it gives
            # away nothing about the calendar.
            parts = [p.strip() for p in val.split("|")]
            if len(parts) >= 3 and parts[2]:
                out["calendars"].append({"name": parts[0], "color": parts[1] or "#888888",
                                         "url": parts[2], "id": parts[3] if len(parts) > 3 else ""})
    return out


def fetch(addr):
    if "://" not in addr:                       # local file, for testing
        with open(addr, "rb") as f:
            return f.read()
    req = urllib.request.Request(addr, headers={"User-Agent": "glance"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read()


MAX_PHOTOS = 1000        # across all albums together. Google's share page lists
                         # only about the first 300 pictures of any one album,
                         # so more than that takes more than one album.
_PHOTO = re.compile(r'\["(https://lh3\.googleusercontent\.com/[^"\\]+)",(\d+),(\d+)')


def album_photos(link):
    """Picture addresses from a Google Photos album shared by link.

    Google offers no supported way to read an existing album, so this reads
    the album's share page the way a browser would and picks out the picture
    addresses in it. It is unofficial and can stop working if Google changes
    that page; when it does, the board keeps the last list it had.
    """
    if "://" not in link:                       # local file, for testing
        with open(link, encoding="utf-8", errors="replace") as f:
            html = f.read()
    else:
        req = urllib.request.Request(link, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=40) as r:
            html = r.read().decode("utf-8", errors="replace")
    seen, out = set(), []
    for url, w, h in _PHOTO.findall(html):
        # Skip tiny entries (profile pictures and the like).
        if url in seen or int(w) < 400 or int(h) < 400:
            continue
        seen.add(url)
        out.append(url)
    if not out:
        raise ValueError("no pictures found on the album page")
    return out


def pool(lists, limit):
    """One list from several albums: take turns between them so that every
    album is represented if the total has to be cut off, and drop repeats."""
    seen, out, owner, i = set(), [], [], 0
    while len(out) < limit and any(i < len(l) for l in lists):
        for n, l in enumerate(lists):
            if i < len(l) and l[i] not in seen and len(out) < limit:
                seen.add(l[i])
                out.append(l[i])
                owner.append(n)             # which album each picture came from
        i += 1
    return out, owner


def events_from(ics_bytes, cal_index, tz, first, last):
    """Events between first and last (dates, inclusive), one row per day shown."""
    cal = icalendar.Calendar.from_ical(ics_bytes)
    start = datetime.datetime.combine(first, datetime.time.min, tz)
    stop = datetime.datetime.combine(last + datetime.timedelta(days=1), datetime.time.min, tz)
    rows = []
    for ev in recurring_ical_events.of(cal).between(start, stop):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        title = str(ev.get("SUMMARY", "") or "").strip() or "(no title)"
        s = ev["DTSTART"].dt
        e = ev["DTEND"].dt if ev.get("DTEND") is not None else None
        if isinstance(s, datetime.datetime):
            s = (s if s.tzinfo else s.replace(tzinfo=tz)).astimezone(tz)
            if e is None:
                e = s
            e = (e if e.tzinfo else e.replace(tzinfo=tz)).astimezone(tz)
            # An event running past midnight still belongs to the day it starts.
            if first <= s.date() <= last:
                rows.append({"d": s.date().isoformat(), "t": s.strftime("%H:%M"),
                             # Ending after midnight: call it the end of the day.
                             "e": ("23:59" if e.date() > s.date() else e.strftime("%H:%M")) if e > s else "",
                             "s": title, "c": cal_index})
        else:                                   # all-day; DTEND is exclusive
            e = e or (s + datetime.timedelta(days=1))
            day = max(s, first)
            while day < e and day <= last:
                rows.append({"d": day.isoformat(), "t": "", "e": "", "s": title, "c": cal_index})
                day += datetime.timedelta(days=1)
    return rows


def key_bytes(text):
    text = text.strip()
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if len(raw) != 32:
        raise SystemExit("BOARD_KEY is not a 32-byte key.")
    return raw


def lock(payload, key):
    nonce = os.urandom(12)
    blob = AESGCM(key).encrypt(nonce, json.dumps(payload, ensure_ascii=False,
                                                 separators=(",", ":")).encode("utf-8"), None)
    return base64.b64encode(nonce + blob).decode("ascii")


def unlock(text, key):
    raw = base64.b64decode(text.strip())
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--old", default="")
    a = ap.parse_args()

    settings = parse_settings(os.environ.get("BOARD_SETTINGS", ""))
    key = key_bytes(os.environ.get("BOARD_KEY", ""))
    if not settings["calendars"]:
        raise SystemExit("No calendars in BOARD_SETTINGS.")
    tz = ZoneInfo(settings["tz"])
    now = datetime.datetime.now(tz)
    first, last = now.date(), now.date() + datetime.timedelta(days=DAYS_AHEAD)

    old = None
    if a.old and os.path.exists(a.old):
        try:
            old = unlock(open(a.old, encoding="ascii").read(), key)
        except Exception:
            old = None                           # unreadable or a different key: start fresh

    events, failed = [], []
    for i, c in enumerate(settings["calendars"]):
        try:
            got = events_from(fetch(c["url"]), i, tz, first, last)
            events.extend(got)
            print("calendar %d: %d event row(s)" % (i + 1, len(got)))
        except Exception as ex:
            # The message could carry the address, so only the kind of error is shown.
            failed.append(i)
            print("calendar %d: FAILED (%s)" % (i + 1, type(ex).__name__))
            # Keep showing what that calendar had last time, not a hole.
            if old:
                events.extend(r for r in old.get("events", [])
                              if r.get("c") == i
                              and first.isoformat() <= r.get("d", "") <= last.isoformat())

    events.sort(key=lambda r: (r["d"], r["t"] != "", r["t"], r["s"].lower()))

    photos, photo_of, photos_failed = [], [], False
    if settings["albums"]:
        got = []
        for n, album in enumerate(settings["albums"], 1):
            try:
                got.append(album_photos(album["link"]))
                print("photo album %d: %d picture(s)" % (n, len(got[-1])))
            except Exception as ex:
                photos_failed = True
                got.append([])                  # keep its place, so album numbers stay true
                print("photo album %d: FAILED (%s)" % (n, type(ex).__name__))
        photos, photo_of = pool(got, MAX_PHOTOS)
        if photos_failed and old and len(old.get("photos", [])) > len(photos):
            # Better last time's fuller list than a thinner one from a bad run.
            photos, photo_of = old["photos"], old.get("photoOf", [])
        print("photos: %d in the list" % len(photos))

    body = {
        "photos": photos, "photosFailed": photos_failed,
        # photoOf[i] is the album (by position in "albums") that photos[i] came
        # from, so a Photos module can show only the albums chosen for it.
        "photoOf": photo_of, "albums": [{"id": a["id"], "name": a["name"]} for a in settings["albums"]],
        "tz": settings["tz"], "lat": settings["lat"], "lon": settings["lon"],
        "place": settings["place"], "from": first.isoformat(), "to": last.isoformat(),
        "calendars": [{"name": c["name"], "color": c["color"], "id": c["id"]} for c in settings["calendars"]],
        "events": events, "failed": failed,
    }

    if old:
        same = all(old.get(k) == body[k] for k in body)
        try:
            age = now - datetime.datetime.fromisoformat(old["generated"])
        except Exception:
            age = datetime.timedelta(days=99)
        if same and age < datetime.timedelta(hours=REFRESH_HOURS):
            print("no change; data file left as it is")
            return 0

    body["generated"] = now.isoformat(timespec="seconds")
    with open(a.out, "w", encoding="ascii", newline="\n") as f:
        f.write(lock(body, key) + "\n")
    print("wrote data file: %d event row(s), %d calendar(s), %d failed"
          % (len(events), len(settings["calendars"]), len(failed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
