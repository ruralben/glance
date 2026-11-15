"""Fetch the family's Garmin and Strava numbers and write the locked fitness file for Glance Digital Wall Calendar.

Run by the scheduled GitHub job (.github/workflows/fitness.yml). It reads four
secrets from the environment:

  GARMIN_TOKENS  who is tracked on Garmin Connect and each person's sign-in
                 token, sent by the Manager's Fitness tab when someone signs in
  STRAVA         the family's Strava app (client ID and secret) and, for each
                 person who clicked Authorize, their Strava refresh token and
                 yearly goal, sent by the same tab
  GARMIN_KEY     the key the renewed tokens are kept locked with between runs.
                 It is not the display key: that one is in the TV's address.
  BOARD_KEY      the display key, which locks the numbers the board reads

and writes two files for the "fitness" branch: fitness.enc (the numbers,
locked with the display key) and tokens.enc (the tokens as Garmin and Strava
renewed them, plus what is kept between runs, locked with GARMIN_KEY).

Garmin: each person keeps a history of up to 90 days. Every run fetches the
last seven days again (today keeps changing, and a watch can sync late) and
fills in up to BACKFILL older days not fetched yet. Today also gets its detail:
body battery and stress through the day, and last night's sleep stages.
Garmin's own Health API is for businesses only, so this uses the unofficial
garminconnect library; Garmin can change things and break it, and the board
then says "Garmin not answering" rather than showing old numbers as new.

Strava: its own free API. Activities this year (and the two weeks before, for
the first days of January), the newest one's route, totals, records and every
route this year for the map. Fastest mile and 5K come from each run's best
efforts, fetched once per run and kept.

This script must never print a name, a number or a token: the job's log is
public. It prints counts and nothing else.

    python build_fitness.py --old-tokens old.enc --out fitness.enc --tokens-out tokens.enc
"""
import os, sys, json, base64, argparse, datetime, urllib.request, urllib.parse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DAYS = 7          # fetched again on every run
HISTORY = 90      # days kept per person
BACKFILL = 21     # older days filled in per run
KEEP = ("d", "steps", "goal", "bb", "bbHigh", "sleep", "sleepH", "rhr", "imod", "ivig", "igoal", "stress")
STEP = 15         # minutes between points on today's curves
STRAVA_API = "https://www.strava.com/api/v3"
EFFORTS_PER_RUN = 25   # runs whose best efforts are fetched per job run (Strava allows 100 calls a quarter hour)


def key_bytes(text, name):
    text = (text or "").strip()
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except Exception:
        raw = b""
    if len(raw) != 32:
        raise SystemExit(name + " is missing or is not a 32-byte key.")
    return raw


def lock(payload, key):
    nonce = os.urandom(12)
    blob = AESGCM(key).encrypt(nonce, json.dumps(payload, separators=(",", ":")).encode("utf-8"), None)
    return base64.b64encode(nonce + blob).decode("ascii")


def unlock(text, key):
    raw = base64.b64decode(text.strip())
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf-8"))


def pick(d, *path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def iso_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ Garmin ----
def fetch(g, dates, today_iso):
    """A row per day; also today's raw sleep, kept for its stages."""
    days, sleep_today = [], None
    for d in dates:
        row = {"d": d}
        try:
            s = g.get_user_summary(d) or {}
            avg = s.get("averageStressLevel")
            row.update({"steps": s.get("totalSteps"), "goal": s.get("dailyStepGoal"), "bb": s.get("bodyBatteryMostRecentValue"),
                        "bbHigh": s.get("bodyBatteryHighestValue"), "bbLow": s.get("bodyBatteryLowestValue"),
                        "rhr": s.get("restingHeartRate"), "imod": s.get("moderateIntensityMinutes"),
                        "ivig": s.get("vigorousIntensityMinutes"), "igoal": s.get("intensityMinutesGoal"),
                        "stress": avg if isinstance(avg, (int, float)) and avg >= 0 else None})
        except Exception:
            pass
        try:
            sl = g.get_sleep_data(d) or {}
            if d == today_iso:
                sleep_today = sl
            row["sleep"] = pick(sl, "dailySleepDTO", "sleepScores", "overall", "value")
            secs = pick(sl, "dailySleepDTO", "sleepTimeSeconds")
            row["sleepH"] = round(secs / 3600.0, 1) if isinstance(secs, (int, float)) else None
        except Exception:
            pass
        days.append({k: v for k, v in row.items() if k in KEEP and v is not None})
    return days, sleep_today


def thin(points):
    """Average [minute, value] points into one per STEP minutes; Garmin marks missing values below zero."""
    by = {}
    for m, v in points:
        if v is None or v < 0:
            continue
        by.setdefault(int(m // STEP) * STEP, []).append(v)
    return [[m, round(sum(v) / len(v))] for m, v in sorted(by.items())]


def detail(g, d, tz, sleep_raw):
    """Today's curves, as minutes from local midnight: body battery, stress, sleep stages."""
    midnight = datetime.datetime.combine(datetime.date.fromisoformat(d), datetime.time(), tz)

    def mins(ms):
        return (datetime.datetime.fromtimestamp(ms / 1000.0, tz) - midnight).total_seconds() / 60.0

    out = {}
    try:
        bb = g.get_body_battery(d) or []
        e = bb[0] if isinstance(bb, list) and bb else {}
        pts = []
        for x in e.get("bodyBatteryValuesArray") or []:
            if isinstance(x, list) and len(x) >= 2 and isinstance(x[0], (int, float)):
                lvl = x[2] if len(x) >= 4 else x[1]
                if isinstance(lvl, (int, float)):
                    pts.append([mins(x[0]), lvl])
        out["bbc"] = thin(pts)
        out["charged"], out["drained"] = e.get("charged"), e.get("drained")
    except Exception:
        pass
    try:
        st = g.get_stress_data(d) or {}
        out["stc"] = thin([[mins(x[0]), x[1]] for x in st.get("stressValuesArray") or []
                           if isinstance(x, list) and len(x) >= 2 and isinstance(x[0], (int, float)) and isinstance(x[1], (int, float))])
        if isinstance(st.get("avgStressLevel"), (int, float)) and st["avgStressLevel"] >= 0:
            out["stressAvg"] = st["avgStressLevel"]
    except Exception:
        pass
    try:
        lv = []
        for x in (sleep_raw or {}).get("sleepLevels") or []:
            a = datetime.datetime.fromisoformat(str(x["startGMT"])[:19]).replace(tzinfo=datetime.timezone.utc)
            b = datetime.datetime.fromisoformat(str(x["endGMT"])[:19]).replace(tzinfo=datetime.timezone.utc)
            lv.append([round((a - midnight).total_seconds() / 60), round((b - midnight).total_seconds() / 60), int(round(float(x["activityLevel"])))])
        if lv:
            out["stages"] = lv              # 0 deep, 1 light, 2 REM, 3 awake
        dto = (sleep_raw or {}).get("dailySleepDTO") or {}
        if dto.get("sleepStartTimestampGMT") and dto.get("sleepEndTimestampGMT"):
            out["bed"], out["wake"] = round(mins(dto["sleepStartTimestampGMT"])), round(mins(dto["sleepEndTimestampGMT"]))
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in (None, [])}


def merge(old, new, today):
    """The history, newest first: new rows replace old ones for the same day."""
    first = (today - datetime.timedelta(days=HISTORY - 1)).isoformat()
    by = {r["d"]: r for r in old if isinstance(r, dict) and r.get("d")}
    by.update({r["d"]: r for r in new})
    return [by[d] for d in sorted(by, reverse=True) if d >= first]


def garmin(sent, kept, today, tz, out, renewed):
    from garminconnect import Garmin
    ok = 0
    for n, x in enumerate(sent.get("accounts", []), 1):
        # A token renewed by an earlier run is newer than the one the Manager
        # sent, unless the person has signed in again since (a new signedAt).
        prev = kept.get(x.get("id")) or {}
        token = prev["tokens"] if prev.get("signedAt") == x.get("signedAt") and prev.get("tokens") else x.get("tokens")
        person = {"id": x.get("id"), "name": x.get("name", ""), "color": x.get("color", "#4da3ff"), "days": [], "hist": []}
        entry = dict(x, tokens=token)
        hist = prev.get("hist") or prev.get("days") or []
        have = {r.get("d") for r in hist if isinstance(r, dict)}
        recent = [(today - datetime.timedelta(days=k)).isoformat() for k in range(DAYS)]
        older = [d for d in [(today - datetime.timedelta(days=k)).isoformat() for k in range(DAYS, HISTORY)] if d not in have][:BACKFILL]
        try:
            g = Garmin()
            g.login(token)
            rows, sleep_raw = fetch(g, recent + older, recent[0])
            hist = merge(hist, rows, today)
            person["today"] = dict(detail(g, recent[0], tz, sleep_raw), d=recent[0])
            entry["tokens"] = g.client.dumps()
            person["ok"], person["okAt"] = True, iso_utc()
            ok += 1
            t = person["today"]
            print("garmin %d: ok, %d days kept; today: %d battery points, %d stress points, %d sleep stages" %
                  (n, len(hist), len(t.get("bbc", [])), len(t.get("stc", [])), len(t.get("stages", []))))
        except Exception as ex:
            print("garmin %d: FAILED (%s)" % (n, type(ex).__name__))
            hist = merge(hist, [], today)                    # the board dims these and says since when
            person["ok"], person["okAt"] = False, prev.get("okAt", "")
            if prev.get("today"):
                person["today"] = prev["today"]
        person["hist"] = hist
        person["days"] = [r for r in hist if r["d"] in recent]
        entry.pop("days", None)
        entry.update(hist=hist, okAt=person["okAt"], today=person.get("today"))
        renewed["accounts"].append(entry)
        out["people"].append(person)
    return ok, len(sent.get("accounts", []))


# ------------------------------------------------------------------ Strava ----
def http_json(url, data=None, token=None):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def decode_polyline(s):
    """Google's encoded polyline, the way Strava sends routes."""
    pts, i, lat, lng = [], 0, 0, 0
    while i < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            v = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += v
            else:
                lng += v
        pts.append([lat / 1e5, lng / 1e5])
    return pts


def sample(pts, n):
    if len(pts) <= n:
        return [[round(a, 5), round(b, 5)] for a, b in pts]
    step = (len(pts) - 1) / float(n - 1)
    return [[round(pts[int(round(k * step))][0], 5), round(pts[int(round(k * step))][1], 5)] for k in range(n)]


KINDS = {"Run": "Run", "TrailRun": "Run", "VirtualRun": "Run", "Ride": "Ride", "GravelRide": "Ride", "MountainBikeRide": "Ride",
         "VirtualRide": "Ride", "EBikeRide": "Ride", "Walk": "Walk", "Hike": "Hike", "Swim": "Swim"}


def strava(sent, kept, today, tz, out, renewed):
    accounts = [x for x in sent.get("accounts") or [] if x.get("refresh")]
    if not accounts or not sent.get("client_id") or not sent.get("client_secret"):
        return 0, 0
    year0 = datetime.date(today.year, 1, 1)
    after = datetime.datetime.combine(min(year0, today - datetime.timedelta(days=14)), datetime.time(), tz).timestamp()
    ok = 0
    out["strava"] = {"people": []}
    renewed["strava"] = []
    for n, x in enumerate(accounts, 1):
        prev = kept.get(x.get("id")) or {}
        refresh = prev["refresh"] if prev.get("signedAt") == x.get("signedAt") and prev.get("refresh") else x.get("refresh")
        person = {"id": x.get("id"), "name": x.get("name", ""), "color": x.get("color", "#ff7a3d"), "goal": x.get("goal")}
        entry = {"id": x.get("id"), "signedAt": x.get("signedAt"), "refresh": refresh, "efforts": prev.get("efforts") or {}, "okAt": prev.get("okAt", "")}
        try:
            tok = http_json("https://www.strava.com/oauth/token", {"client_id": sent["client_id"], "client_secret": sent["client_secret"],
                                                                     "grant_type": "refresh_token", "refresh_token": refresh})
            entry["refresh"] = tok["refresh_token"]
            access = tok["access_token"]
            acts = []
            for page in range(1, 6):
                got = http_json("%s/athlete/activities?after=%d&per_page=200&page=%d" % (STRAVA_API, int(after), page), token=access)
                acts += got
                if len(got) < 200:
                    break
            acts.sort(key=lambda q: q.get("start_date") or "")
            # Fastest mile and 5K: from each run's best efforts, fetched once.
            eff, asked = entry["efforts"], 0
            for q in reversed(acts):
                if KINDS.get(q.get("sport_type") or q.get("type")) == "Run" and str(q["id"]) not in eff and asked < EFFORTS_PER_RUN:
                    asked += 1
                    try:
                        dt = http_json("%s/activities/%d" % (STRAVA_API, q["id"]), token=access)
                        eff[str(q["id"])] = {e.get("name"): e.get("elapsed_time") for e in dt.get("best_efforts") or [] if e.get("name") in ("1 mile", "5k")}
                    except Exception:
                        break
            keep_ids = {str(q["id"]) for q in acts}
            entry["efforts"] = {k: v for k, v in eff.items() if k in keep_ids}

            def row(q):
                return {"id": q["id"], "d": (q.get("start_date_local") or "")[:16], "k": KINDS.get(q.get("sport_type") or q.get("type"), "Other"),
                        "n": (q.get("name") or "")[:60], "m": round(q.get("distance") or 0), "s": q.get("moving_time") or 0,
                        "e": round(q.get("total_elevation_gain") or 0)}
            rows = [row(q) for q in acts]
            this_year = [r for r in rows if r["d"][:4] == str(today.year)]
            person["recent"] = rows[-40:][::-1]                                  # newest first: the week and the feed
            person["year"] = {"m": sum(r["m"] for r in this_year), "s": sum(r["s"] for r in this_year), "n": len(this_year)}
            if acts:
                person["latest"] = dict(row(acts[-1]), route=sample(decode_polyline(pick(acts[-1], "map", "summary_polyline") or ""), 160))
            person["routes"] = [sample(decode_polyline(pick(q, "map", "summary_polyline")), 40)
                                for q in acts if (q.get("start_date_local") or "")[:4] == str(today.year) and pick(q, "map", "summary_polyline")][-200:]

            def best(rs, key, low=False):
                rs = [r for r in rs if r.get(key)]
                if not rs:
                    return None
                r = (min if low else max)(rs, key=lambda q: q[key])
                return {"v": r[key], "d": r["d"][:10]}
            runs = [dict(r, mile=entry["efforts"].get(str(r["id"]), {}).get("1 mile"), k5=entry["efforts"].get(str(r["id"]), {}).get("5k"))
                    for r in this_year if r["k"] == "Run"]
            weeks = {}
            for r in this_year:
                d = datetime.date.fromisoformat(r["d"][:10])
                w = (d - datetime.timedelta(days=d.weekday())).isoformat()
                weeks[w] = weeks.get(w, 0) + r["m"]
            person["records"] = {"mile": best(runs, "mile", True), "k5": best(runs, "k5", True), "run": best(runs, "m"),
                                 "ride": best([r for r in this_year if r["k"] == "Ride"], "m"), "climb": best(this_year, "e"),
                                 "week": ({"v": max(weeks.values()), "d": max(weeks, key=weeks.get)} if weeks else None)}
            person["ok"], person["okAt"] = True, iso_utc()
            entry["okAt"] = person["okAt"]
            entry["last"] = {k: person.get(k) for k in ("recent", "year", "latest", "routes", "records")}
            ok += 1
            print("strava %d: ok, %d activities, %d best efforts asked" % (n, len(acts), asked))
        except Exception as ex:
            print("strava %d: FAILED (%s)" % (n, type(ex).__name__))
            person.update(prev.get("last") or {})
            person["ok"], person["okAt"] = False, prev.get("okAt", "")
            entry["last"] = prev.get("last")
        out["strava"]["people"].append(person)
        renewed["strava"].append(entry)
    return ok, len(accounts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-tokens", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tokens-out", required=True)
    a = ap.parse_args()

    board = key_bytes(os.environ.get("BOARD_KEY"), "BOARD_KEY")
    gkey = key_bytes(os.environ.get("GARMIN_KEY"), "GARMIN_KEY")
    try:
        sent = json.loads(os.environ.get("GARMIN_TOKENS") or "{}")
    except Exception:
        raise SystemExit("GARMIN_TOKENS could not be read.")
    try:
        sent_strava = json.loads(os.environ.get("STRAVA") or "{}")
    except Exception:
        raise SystemExit("STRAVA could not be read.")
    old = {}
    if a.old_tokens and os.path.exists(a.old_tokens):
        try:
            old = unlock(open(a.old_tokens, encoding="ascii").read(), gkey)
        except Exception:
            old = {}                                  # unreadable or a different key: start from what was sent

    tz = datetime.timezone.utc
    if os.environ.get("TZ_NAME"):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(os.environ["TZ_NAME"])
        except Exception:
            pass
    today = datetime.datetime.now(tz).date()
    out = {"at": iso_utc(), "people": []}
    renewed = {"accounts": []}
    ok_g, n_g = (0, 0)
    if sent.get("accounts"):
        ok_g, n_g = garmin(sent, {x.get("id"): x for x in old.get("accounts", []) if isinstance(x, dict)}, today, tz, out, renewed)
    ok_s, n_s = strava(sent_strava, {x.get("id"): x for x in old.get("strava", []) if isinstance(x, dict)}, today, tz, out, renewed)

    open(a.out, "w", encoding="ascii").write(lock(out, board) + "\n")
    open(a.tokens_out, "w", encoding="ascii").write(lock(renewed, gkey) + "\n")
    print("garmin: %d of %d accounts fetched; strava: %d of %d" % (ok_g, n_g, ok_s, n_s))
    if n_g + n_s and not ok_g + ok_s:
        sys.exit(1)


if __name__ == "__main__":
    main()
