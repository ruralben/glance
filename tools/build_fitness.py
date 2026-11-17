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

The job runs every 15 minutes. Most runs are light: today's numbers and
curves only, a handful of requests a person. About once an hour (FULL_EVERY)
a run is full: it also fetches the last seven days again (a watch can sync
late), fills in older days, asks for the recovery readings and refreshes Strava.
Garmin's way in is unofficial, and asking too often can get it blocked.

Garmin: each person keeps a history of up to 90 days. A full run fetches the
last seven days again and fills in up to BACKFILL older days not fetched yet. Today also gets its detail:
body battery and stress through the day, and last night's sleep stages; and
whatever the watch works out about recovery and fitness (HRV status, training
readiness, VO2 max, fitness age, training status, pulse ox, breathing), each
kept up to INS_DAYS so a missed night does not blank it.
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
KEEP = ("d", "steps", "goal", "bb", "bbHigh", "sleep", "sleepH", "rhr", "imod", "ivig", "igoal", "stress",
        "kcal", "kcalT", "dist", "bbUp", "bbDown")
INS_DAYS = 7      # an insight older than this (say, HRV from a night the watch was off) is dropped
INS_KEEP = {"vo2": 60, "fage": 60}   # these change slowly and are measured seldom (VO2 max needs an outdoor walk or run)
FULL_EVERY = 50   # minutes between full runs
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
                        "stress": avg if isinstance(avg, (int, float)) and avg >= 0 else None,
                        "kcal": round(s["activeKilocalories"]) if isinstance(s.get("activeKilocalories"), (int, float)) else None,
                        "kcalT": round(s["totalKilocalories"]) if isinstance(s.get("totalKilocalories"), (int, float)) else None,
                        "dist": round(s["totalDistanceMeters"]) if isinstance(s.get("totalDistanceMeters"), (int, float)) else None,
                        "bbUp": s.get("bodyBatteryChargedValue"), "bbDown": s.get("bodyBatteryDrainedValue")})
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
    def levels(arr):
        pts = []
        for x in arr or []:
            if isinstance(x, list) and len(x) >= 2 and isinstance(x[0], (int, float)):
                lvl = x[2] if len(x) >= 4 else x[1]
                if isinstance(lvl, (int, float)):
                    pts.append([mins(x[0]), lvl])
        return pts

    try:
        bb = g.get_body_battery(d) or []
        e = bb[0] if isinstance(bb, list) and bb else {}
        out["bbc"] = thin(levels(e.get("bodyBatteryValuesArray")))
        out["charged"], out["drained"] = e.get("charged"), e.get("drained")
    except Exception:
        pass
    try:
        st = g.get_stress_data(d) or {}
        # The stress answer carries body battery every few minutes; the body
        # battery answer has only a handful of points. Use whichever has more.
        dense = thin(levels(st.get("bodyBatteryValuesArray")))
        if len(dense) > len(out.get("bbc") or []):
            out["bbc"] = dense
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


def num(v):
    """A number from Garmin, which sometimes sends them as text; None for anything else."""
    try:
        v = float(v)
        return v if v == v and v > 0 else None
    except (TypeError, ValueError):
        return None


def insights(g, d):
    """What the watch works out about recovery and fitness, where it does:
    HRV status, training readiness, VO2 max, fitness age, training status,
    pulse ox and breathing rate. Each is asked for on its own; a watch that
    does not record one simply has no answer for it."""
    out = {}
    try:
        h = (g.get_hrv_data(d) or {}).get("hrvSummary") or {}
        if num(h.get("lastNightAvg")):
            out["hrv"] = {"v": round(num(h["lastNightAvg"])), "st": str(h.get("status") or "").upper(),
                          "wk": round(num(h["weeklyAvg"])) if num(h.get("weeklyAvg")) else None}
    except Exception:
        pass
    try:
        rd = g.get_training_readiness(d) or []
        rd = [x for x in (rd if isinstance(rd, list) else [rd]) if isinstance(x, dict) and num(x.get("score"))]
        if rd:
            x = max(rd, key=lambda x: str(x.get("timestamp") or ""))
            out["ready"] = {"v": round(num(x["score"])), "lvl": str(x.get("level") or "").upper()}
    except Exception:
        pass
    try:
        ts = g.get_training_status(d) or {}
        v = pick(ts, "mostRecentVO2Max", "generic", "vo2MaxPreciseValue") or pick(ts, "mostRecentVO2Max", "generic", "vo2MaxValue")
        if num(v):
            out["vo2"] = {"v": round(num(v), 1)}
        devs = pick(ts, "mostRecentTrainingStatus", "latestTrainingStatusData") or {}
        devs = [x for x in devs.values() if isinstance(x, dict)] if isinstance(devs, dict) else []
        devs.sort(key=lambda x: not x.get("primaryTrainingDevice"))
        for x in devs:
            word = str(x.get("trainingStatusFeedbackPhrase") or "").upper().rsplit("_", 1)[0]
            if word and not word.startswith("NO_STATUS"):
                out["tstat"] = {"st": word}
                break
    except Exception:
        pass
    if "vo2" not in out:
        try:
            mm = g.get_max_metrics(d) or []
            for x in (mm if isinstance(mm, list) else [mm]):
                v = pick(x, "generic", "vo2MaxPreciseValue") or pick(x, "generic", "vo2MaxValue")
                if num(v):
                    out["vo2"] = {"v": round(num(v), 1)}
                    break
        except Exception:
            pass
    try:
        fa = g.get_fitnessage_data(d) or {}
        if num(fa.get("fitnessAge")):
            out["fage"] = {"v": round(num(fa["fitnessAge"])), "age": round(num(fa["chronologicalAge"])) if num(fa.get("chronologicalAge")) else None}
    except Exception:
        pass
    try:
        sp = g.get_spo2_data(d) or {}
        v = num(sp.get("avgSleepSpO2")) or num(sp.get("averageSpO2"))
        if v:
            out["spo2"] = {"v": round(v), "low": round(num(sp["lowestSpO2"])) if num(sp.get("lowestSpO2")) else None}
    except Exception:
        pass
    try:
        rp = g.get_respiration_data(d) or {}
        v = num(rp.get("avgSleepRespirationValue")) or num(rp.get("avgWakingRespirationValue"))
        if v:
            out["resp"] = {"v": round(v)}
    except Exception:
        pass
    for x in out.values():
        x["d"] = d
        for k in [k for k, v in x.items() if v in (None, "")]:
            del x[k]
    return out


def keep_insights(old, new, today):
    """Today's insights, and the last one of each kind from the past week (two months for
    VO2 max and fitness age) where today has none yet."""
    def first(k):
        return (today - datetime.timedelta(days=INS_KEEP.get(k, INS_DAYS) - 1)).isoformat()
    out = {k: v for k, v in (old or {}).items() if isinstance(v, dict) and str(v.get("d", "")) >= first(k)}
    out.update(new)
    return out


INS_NAMES = (("hrv", "HRV"), ("ready", "readiness"), ("vo2", "VO2 max"), ("fage", "fitness age"),
             ("tstat", "training status"), ("spo2", "pulse ox"), ("resp", "breathing"))


def due(prev, sent_at):
    """Whether this run should be a full one for someone: an hour since the last
    full run, a fresh sign-in from the Manager, nothing kept yet, or a last run
    that failed (so the next one catches up on whatever it missed), or the
    explorer's activity history not fetched yet."""
    if prev.get("failed") or not prev.get("gactsAt") or any(not a.get("hx") for a in prev.get("gacts") or []) or prev.get("signedAt") != sent_at or not prev.get("fullAt") or not (prev.get("hist") or prev.get("days")):
        return True
    try:
        last = datetime.datetime.fromisoformat(str(prev["fullAt"]).replace("Z", "+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds() >= FULL_EVERY * 60
    except ValueError:
        return True


# ------------------------------------------- Strava against Garmin ----
# For someone on both (paired by first name), a full run lists their Garmin
# activities for the same span Strava covers and matches the two by start time.
# The log gets counts only; which activities are on one side only goes into the
# locked file, where the Manager's copy can read it.
GARMIN_ACTS = {}

# ------------------------------------------------ the explorer's history ----
# For the Fitness Explorer, everything Garmin and Strava keep about each
# activity: the full summary, the route at full detail, the second-by-second
# heart rate, speed, elevation and the rest, laps, heart rate zones, weather,
# gear and strength sets; from Strava the full record and its streams.
#
# That is far too much to carry through the public fitness branch on every
# run, so it travels once: each run puts only what it fetched new into
# explore-delta.enc (locked with GARMIN_KEY), which the job keeps as a run
# artifact for 90 days, and the Manager collects every one into the copy on
# the PC, which keeps everything for good. Between runs this job keeps only a
# light list of activities and what it has already handed over (hx, hp, hd).
ROUTES_PER_RUN = 50     # Garmin activities a run whose route and second-by-second numbers are fetched (one request each)
DETAILS_PER_RUN = 15    # Garmin activities a run whose laps, zones, weather, gear and sets are fetched (3 to 5 requests each)
STRAVA_EXTRAS_PER_RUN = 70   # Strava requests a full run for the rest: who gave kudos, comments, photos, and each segment's own record
STRAVA_DETAILS_PER_RUN = 40   # Strava activities a full run whose full record and streams are fetched (two requests each; Strava allows 100 a quarter hour)
# Who an activity belongs to and its privacy settings: never kept.
RAW_SKIP = {"ownerId", "ownerDisplayName", "ownerFullName", "ownerProfileImageUrlSmall", "ownerProfileImageUrlMedium",
            "ownerProfileImageUrlLarge", "userRoles", "privacy", "athlete", "userProfilePk", "userProfileId"}
DELTA = {"garmin": {}, "strava": {}}    # what this run hands over: side -> person id -> {name, color, acts: {id: row}}


def compact(v):
    """An answer from Garmin or Strava, whole, without empty values or who it belongs to; floats rounded."""
    if isinstance(v, float):
        return round(v, 5)
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if k in RAW_SKIP:
                continue
            x = compact(x)
            if x not in (None, "", [], {}):
                out[k] = x
        return out
    if isinstance(v, list):
        return [compact(x) for x in v]
    return v


def hand_over(side, who, row, **parts):
    """Add to this run's delta: the activity's light row plus the heavy parts fetched for it."""
    p = DELTA[side].setdefault(who.get("id"), {"name": who.get("name", ""), "color": who.get("color"), "acts": {}})
    d = p["acts"].setdefault(str(row["id"]), {})
    d.update({k: v for k, v in row.items() if k not in ("hx", "hp", "hd")})
    d.update({k: v for k, v in parts.items() if v not in (None, "", [], {})})


def garmin_act_row(a):
    t = str(a.get("startTimeLocal") or "")[:19].replace(" ", "T")
    r = {"id": a.get("activityId"), "t": t, "k": pick(a, "activityType", "typeKey") or "", "n": str(a.get("activityName") or "")[:60],
         "m": a.get("distance"), "s": a.get("duration"), "mv": a.get("movingDuration"), "e": a.get("elevationGain"),
         "hr": a.get("averageHR"), "hrx": a.get("maxHR"), "cal": a.get("calories"), "st": a.get("steps"),
         "sp": a.get("averageSpeed"), "spx": a.get("maxSpeed"), "te": a.get("aerobicTrainingEffect"), "tea": a.get("anaerobicTrainingEffect"),
         "v": a.get("vO2MaxValue"), "lat": a.get("startLatitude"), "lon": a.get("startLongitude"), "poly": bool(a.get("hasPolyline")),
         "loc": a.get("locationName")}
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items() if v not in (None, "")}


def light(r):
    """What this job keeps between runs: the row and its flags, never the heavy parts."""
    return {k: v for k, v in r.items() if k not in ("x", "pts", "dx", "streams", "extra")}


def garmin_streams(g, acts, who):
    """Route at full detail and the second-by-second numbers, from one request per activity."""
    asked = 0
    for r in sorted(acts, key=lambda q: q.get("t", ""), reverse=True):
        if asked >= ROUTES_PER_RUN:
            break
        if r.get("hp"):
            continue
        asked += 1
        try:
            det = g.get_activity_details(str(r["id"]), maxchart=2000, maxpoly=4000) or {}
        except Exception:
            continue                                          # tried again next run
        pts = []
        for q in pick(det, "geoPolylineDTO", "polyline") or []:
            if isinstance(q, dict) and isinstance(q.get("lat"), (int, float)) and isinstance(q.get("lon"), (int, float)):
                pts.append([round(q["lat"], 6), round(q["lon"], 6)] + ([round(q["altitude"], 1)] if isinstance(q.get("altitude"), (int, float)) else []))
        keys = [[d.get("key"), pick(d, "unit", "key")] for d in sorted(det.get("metricDescriptors") or [], key=lambda d: d.get("metricsIndex", 0))]
        rows = [[round(v, 5) if isinstance(v, float) else v for v in (m.get("metrics") or [])] for m in det.get("activityDetailMetrics") or []]
        r["hp"] = 1
        hand_over("garmin", who, r, pts=pts, streams={"keys": keys, "rows": rows} if rows else None)
    return asked


def garmin_details(g, acts, who):
    """Laps, time in each heart rate zone, the weather, the gear, and for strength
    work every set; each Garmin's whole answer. One request each."""
    asked = 0
    for r in sorted(acts, key=lambda q: q.get("t", ""), reverse=True):
        if asked >= DETAILS_PER_RUN:
            break
        if r.get("hd"):
            continue
        asked += 1
        i, k, dx = str(r["id"]), str(r.get("k") or ""), {}
        def ask(name, f):
            try:
                v = compact(f())
                if v not in (None, "", [], {}):
                    dx[name] = v
            except Exception:
                pass
        ask("zones", lambda: g.get_activity_hr_in_timezones(i))
        ask("splits", lambda: g.get_activity_splits(i))
        ask("typed", lambda: g.get_activity_typed_splits(i))
        if r.get("lat") is not None:
            ask("weather", lambda: g.get_activity_weather(i))
        ask("gear", lambda: g.get_activity_gear(i))
        if any(w in k for w in ("strength", "hiit", "cardio", "fitness_equipment", "training", "yoga", "pilates")):
            ask("sets", lambda: g.get_activity_exercise_sets(i))
        r["hd"] = 1
        hand_over("garmin", who, r, dx=dict(dx, at=iso_utc()))
    return asked


def garmin_history(g, prev_acts, today, who):
    """Every activity, kept by id. The whole list the first time (and once more for
    anything kept before the full summaries were); the last 30 days after that.
    A summary new or changed since the last run is handed over whole."""
    cache = {str(a["id"]): light(a) for a in prev_acts or [] if isinstance(a, dict) and a.get("id") is not None}
    for a in cache.values():                                  # kept before everything was handed over whole: fetch it all again
        if not a.get("hx"):
            a.pop("hp", None); a.pop("hd", None)
    since = "2000-01-01" if not cache or any(not a.get("hx") for a in cache.values()) else (today - datetime.timedelta(days=30)).isoformat()
    for a in g.get_activities_by_date(since, today.isoformat()) or []:
        r = garmin_act_row(a)
        if r.get("id") is None:
            continue
        old = cache.get(str(r["id"])) or {}
        for q in ("hp", "hd"):
            if old.get(q):
                r[q] = old[q]
        flags = ("hx", "hp", "hd")
        if not old.get("hx") or {k: v for k, v in old.items() if k not in flags} != {k: v for k, v in r.items() if k not in flags}:
            hand_over("garmin", who, r, x=compact(a))
        r["hx"] = 1
        cache[str(r["id"])] = r
    return sorted(cache.values(), key=lambda q: q.get("t", ""))


def strava_act_row(q):
    r = {"id": q.get("id"), "t": str(q.get("start_date_local") or "")[:19], "k": q.get("sport_type") or q.get("type") or "", "n": str(q.get("name") or "")[:60],
         "m": q.get("distance"), "s": q.get("moving_time"), "el": q.get("elapsed_time"), "e": q.get("total_elevation_gain"),
         "sp": q.get("average_speed"), "spx": q.get("max_speed"), "hr": q.get("average_heartrate"), "hrx": q.get("max_heartrate"),
         "cad": q.get("average_cadence"), "w": q.get("average_watts"), "kj": q.get("kilojoules"), "ku": q.get("kudos_count"),
         "ach": q.get("achievement_count"), "pr": q.get("pr_count"), "tr": q.get("trainer") or None, "co": q.get("commute") or None,
         "man": q.get("manual") or None, "poly": pick(q, "map", "summary_polyline") or None}
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items() if v not in (None, "")}


def first_name(x):
    return str(x or "").strip().lower().split(" ")[0]


def garmin_activities(g, start, end):
    out = []
    for a in g.get_activities_by_date(start.isoformat(), end.isoformat()) or []:
        t = str(a.get("startTimeLocal") or "")[:19]
        if t:
            out.append({"t": t.replace(" ", "T"), "k": pick(a, "activityType", "typeKey") or "", "m": round(a.get("distance") or 0)})
    return out


def compare(strava_acts, garmin_acts):
    """Match by start time, within three minutes, each activity used once."""
    def at(t):
        return datetime.datetime.fromisoformat(str(t)[:19])
    left = [dict(a, at=at(a["t"])) for a in garmin_acts]
    both, only_s = 0, []
    for q in strava_acts:
        t = at(q.get("start_date_local") or "")
        hit = min(left, key=lambda a: abs((a["at"] - t).total_seconds()), default=None)
        if hit is not None and abs((hit["at"] - t).total_seconds()) <= 180:
            both += 1
            left.remove(hit)
        else:
            only_s.append({"t": str(q.get("start_date_local"))[:16], "k": q.get("sport_type") or q.get("type") or "",
                           "m": round(q.get("distance") or 0), "manual": bool(q.get("manual"))})
    only_g = [{"t": a["t"][:16], "k": a["k"], "m": a["m"]} for a in left]
    return {"both": both, "onlyStrava": only_s, "onlyGarmin": only_g}


def merge(old, new, today):
    """The history, newest first: new rows replace old ones for the same day."""
    first = (today - datetime.timedelta(days=HISTORY - 1)).isoformat()
    by = {r["d"]: r for r in old if isinstance(r, dict) and r.get("d")}
    by.update({r["d"]: r for r in new})
    return [by[d] for d in sorted(by, reverse=True) if d >= first]


def garmin(sent, kept, today, tz, out, renewed, on_strava=()):
    from garminconnect import Garmin
    ok = 0
    for n, x in enumerate(sent.get("accounts", []), 1):
        # A token renewed by an earlier run is newer than the one the Manager
        # sent, unless the person has signed in again since (a new signedAt).
        prev = kept.get(x.get("id")) or {}
        token = prev["tokens"] if prev.get("signedAt") == x.get("signedAt") and prev.get("tokens") else x.get("tokens")
        person = {"id": x.get("id"), "name": x.get("name", ""), "color": x.get("color", "#4da3ff"), "days": [], "hist": []}
        entry = dict(x, tokens=token, gacts=prev.get("gacts") or [], gactsAt=prev.get("gactsAt", ""))
        hist = prev.get("hist") or prev.get("days") or []
        have = {r.get("d") for r in hist if isinstance(r, dict)}
        recent = [(today - datetime.timedelta(days=k)).isoformat() for k in range(DAYS)]
        older = [d for d in [(today - datetime.timedelta(days=k)).isoformat() for k in range(DAYS, HISTORY)] if d not in have][:BACKFILL]
        full = due(prev, x.get("signedAt"))
        entry["fullAt"] = prev.get("fullAt", "") if prev.get("signedAt") == x.get("signedAt") else ""
        try:
            g = Garmin()
            g.login(token)
            rows, sleep_raw = fetch(g, recent + older if full else recent[:1], recent[0])
            hist = merge(hist, rows, today)
            person["today"] = dict(detail(g, recent[0], tz, sleep_raw), d=recent[0])
            fresh = insights(g, recent[0]) if full else {}
            if full:
                try:
                    entry["gacts"] = garmin_history(g, entry["gacts"], today, x)
                    entry["gactsAt"] = iso_utc()
                except Exception as ex:
                    print("garmin %d: activity history not fetched (%s)" % (n, type(ex).__name__))
            entry["gacts"] = [light(a) for a in entry["gacts"]]
            if any(not a.get("hp") or not a.get("hd") for a in entry["gacts"]):
                try:
                    routes, more = garmin_streams(g, entry["gacts"], x), garmin_details(g, entry["gacts"], x)
                    print("garmin %d: %d activities; %d more with route and second-by-second numbers (%d done), %d more with laps, zones, weather, gear and sets (%d done)" %
                          (n, len(entry["gacts"]), routes, sum(1 for a in entry["gacts"] if a.get("hp")), more, sum(1 for a in entry["gacts"] if a.get("hd"))))
                except Exception as ex:
                    print("garmin %d: activity details not fetched (%s)" % (n, type(ex).__name__))
            if full:
                if first_name(x.get("name")) in on_strava:
                    since = min(datetime.date(today.year, 1, 1), today - datetime.timedelta(days=14)).isoformat()
                    GARMIN_ACTS[first_name(x.get("name"))] = [{"t": a["t"], "k": a.get("k", ""), "m": round(a.get("m") or 0)}
                                                              for a in entry["gacts"] if a.get("t", "") >= since]
            person["ins"] = keep_insights(prev.get("ins"), fresh, today)
            entry["tokens"] = g.client.dumps()
            person["ok"], person["okAt"] = True, iso_utc()
            if full:
                entry["fullAt"] = person["okAt"]
            ok += 1
            t = person["today"]
            print("garmin %d: ok (%s), %d days kept; today: %d battery points, %d stress points, %d sleep stages" %
                  (n, "full" if full else "today only", len(hist), len(t.get("bbc", [])), len(t.get("stc", [])), len(t.get("stages", []))))
            if full:
                print("garmin %d: watch has %s" % (n, ", ".join(nm for k, nm in INS_NAMES if k in fresh) or "none of the insights today"))
        except Exception as ex:
            print("garmin %d: FAILED (%s)" % (n, type(ex).__name__))
            entry["failed"] = True                           # the next run is a full one
            hist = merge(hist, [], today)                    # the board dims these and says since when
            person["ok"], person["okAt"] = False, prev.get("okAt", "")
            if prev.get("today"):
                person["today"] = prev["today"]
            person["ins"] = keep_insights(prev.get("ins"), {}, today)
        person["hist"] = hist
        person["days"] = [r for r in hist if r["d"] in recent]
        entry.pop("days", None)
        entry.update(hist=hist, okAt=person["okAt"], today=person.get("today"), ins=person.get("ins") or {})
        if not entry.get("fullAt"):
            entry.pop("fullAt", None)
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


def strava(sent, kept, today, tz, out, renewed, full=True):
    # Each person signs in through their own Strava app; an older STRAVA secret has one app for everyone.
    accounts = [dict(x, client_id=x.get("client_id") or sent.get("client_id"), client_secret=x.get("client_secret") or sent.get("client_secret"))
                for x in sent.get("accounts") or [] if x.get("refresh")]
    accounts = [x for x in accounts if x["client_id"] and x["client_secret"]]
    if not accounts:
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
        entry = {"id": x.get("id"), "signedAt": x.get("signedAt"), "refresh": refresh, "efforts": prev.get("efforts") or {}, "okAt": prev.get("okAt", ""),
                 "sacts": prev.get("sacts") or [], "segs": prev.get("segs") or []}
        if not full and prev.get("signedAt") == x.get("signedAt") and prev.get("last") and prev.get("okAt"):
            person.update(prev["last"])
            person["ok"], person["okAt"] = True, prev["okAt"]
            entry["last"] = prev["last"]
            ok += 1
            print("strava %d: ok, kept from the last full run" % n)
            out["strava"]["people"].append(person)
            renewed["strava"].append(entry)
            continue
        try:
            tok = http_json("https://www.strava.com/oauth/token", {"client_id": x["client_id"], "client_secret": x["client_secret"],
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
            # The explorer's history: every activity ever, the first time (and once more for
            # anything kept before summaries were handed over whole); this run's after that.
            cache = {str(a["id"]): light(a) for a in entry["sacts"] if isinstance(a, dict) and a.get("id") is not None}
            for a in cache.values():
                if not a.get("hx"):
                    a.pop("hd", None)
            older = []
            if not cache or any(not a.get("hx") for a in cache.values()):
                for page in range(1, 11):
                    got = http_json("%s/athlete/activities?before=%d&per_page=200&page=%d" % (STRAVA_API, int(after), page), token=access)
                    older += got
                    if len(got) < 200:
                        break
            who = {"id": x.get("id"), "name": x.get("name", ""), "color": x.get("color")}
            for q in older + acts:
                r = strava_act_row(q)
                if r.get("id") is None:
                    continue
                old = cache.get(str(r["id"])) or {}
                for q in ("hd", "segs", "hxe"):
                    if q in old:
                        r[q] = old[q]
                flags = ("hx", "hd", "segs", "hxe")
                if not old.get("hx") or {k: v for k, v in old.items() if k not in flags} != {k: v for k, v in r.items() if k not in flags}:
                    hand_over("strava", who, r, x=compact(q))
                r["hx"] = 1
                cache[str(r["id"])] = r
            # Each activity's full record and its streams, newest first, ten a full run.
            more = 0
            for r in sorted(cache.values(), key=lambda q: q.get("t", ""), reverse=True):
                if more >= STRAVA_DETAILS_PER_RUN:
                    break
                if (r.get("hd") and "segs" in r) or r.get("man"):
                    continue
                more += 1
                try:
                    full_rec = compact(http_json("%s/activities/%s?include_all_efforts=true" % (STRAVA_API, r["id"]), token=access))
                except Exception:
                    break                                     # likely the rate limit: next run
                try:
                    streams = compact(http_json("%s/activities/%s/streams?keys=time,distance,latlng,altitude,velocity_smooth,heartrate,cadence,watts,temp,moving,grade_smooth&key_by_type=true"
                                                % (STRAVA_API, r["id"]), token=access))
                except Exception:
                    streams = None
                r["hd"] = 1
                r["segs"] = sorted({pick(q, "segment", "id") for q in full_rec.get("segment_efforts") or [] if pick(q, "segment", "id")})
                hand_over("strava", who, r, dx=dict(full_rec, at=iso_utc()), streams=streams)
            entry["sacts"] = sorted(cache.values(), key=lambda q: q.get("t", ""))
            print("strava %d: %d activities kept for the explorer, %d with full records (%d this run)" % (n, len(entry["sacts"]), sum(1 for a in entry["sacts"] if a.get("hd")), more))
            # The rest of what Strava keeps about the person: profile, totals, zones, gear, clubs, saved routes and starred segments.
            prof, got = {}, 0
            def get(name, path):
                nonlocal got
                try:
                    v = compact(http_json(STRAVA_API + path, token=access))
                    if v not in (None, "", [], {}):
                        prof[name] = v
                        got += 1
                except Exception:
                    pass
            get("athlete", "/athlete")
            aid = pick(prof, "athlete", "id")
            if aid:
                get("stats", "/athletes/%s/stats" % aid)
                get("routes", "/athletes/%s/routes?per_page=200" % aid)
            get("zones", "/athlete/zones")
            get("clubs", "/athlete/clubs?per_page=200")
            get("starred", "/segments/starred?per_page=200")
            for gid in [b.get("id") for b in (pick(prof, "athlete", "bikes") or []) + (pick(prof, "athlete", "shoes") or []) if isinstance(b, dict)][:20]:
                get("gear_" + str(gid), "/gear/%s" % gid)
            if prof:
                DELTA["strava"].setdefault(x.get("id"), {"name": x.get("name", ""), "color": x.get("color"), "acts": {}})["profile"] = dict(prof, at=iso_utc())
            print("strava %d: profile, totals, zones, gear, clubs and routes: %d parts" % (n, got))
            # The rest, while Strava still answers: who gave kudos, the comments, the photos
            # (each activity once), and every segment's own record (each segment once).
            budget, extras, segs_got = STRAVA_EXTRAS_PER_RUN, 0, {}
            done_segs = set(entry["segs"])
            try:
                for r in sorted(cache.values(), key=lambda q: q.get("t", ""), reverse=True):
                    if budget < 3:
                        break
                    if r.get("hxe") or r.get("man"):
                        continue
                    ex = {}
                    for name, path in (("kudoers", "/activities/%s/kudos?per_page=200"), ("comments", "/activities/%s/comments?per_page=200"),
                                       ("photos", "/activities/%s/photos?size=2048&photo_sources=true")):
                        budget -= 1
                        v = compact(http_json(STRAVA_API + path % r["id"], token=access))
                        if v not in (None, "", [], {}):
                            ex[name] = v
                    r["hxe"] = 1
                    extras += 1
                    hand_over("strava", who, r, extra=dict(ex, at=iso_utc()))
                for sid in sorted({sg for r in cache.values() for sg in r.get("segs") or []} - done_segs):
                    if budget < 1:
                        break
                    budget -= 1
                    segs_got[str(sid)] = compact(http_json("%s/segments/%s" % (STRAVA_API, sid), token=access))
                    done_segs.add(sid)
            except Exception as ex2:
                print("strava %d: the rest stopped for this run (%s)" % (n, type(ex2).__name__))
            entry["segs"] = sorted(done_segs)
            entry["sacts"] = sorted(cache.values(), key=lambda q: q.get("t", ""))
            if segs_got:
                DELTA["strava"].setdefault(x.get("id"), {"name": x.get("name", ""), "color": x.get("color"), "acts": {}})["segments"] = segs_got
            left = sum(1 for r in cache.values() if not r.get("hxe") and not r.get("man")) + len({sg for r in cache.values() for sg in r.get("segs") or []} - done_segs)
            print("strava %d: kudos, comments and photos for %d more activities, %d more segments; %d left" % (n, extras, len(segs_got), left))
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
            ga = GARMIN_ACTS.get(first_name(x.get("name")))
            if ga is None:
                print("strava %d: not compared with Garmin this run (%s)" % (n, "no name to pair by" if not first_name(x.get("name")) else "no Garmin list for that first name"))
            else:
                c = compare(acts, ga)
                out.setdefault("compare", {})[x.get("id")] = dict(c, name=x.get("name", ""), at=iso_utc())
                print("strava %d vs garmin: in both %d; only in Strava %d (%d typed in); only in Garmin %d" %
                      (n, c["both"], len(c["onlyStrava"]), sum(1 for a in c["onlyStrava"] if a["manual"]), len(c["onlyGarmin"])))
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
    ap.add_argument("--explore-out", default="")
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
        on_strava = {first_name(x.get("name")) for x in sent_strava.get("accounts") or [] if x.get("refresh") and first_name(x.get("name"))}
        ok_g, n_g = garmin(sent, {x.get("id"): x for x in old.get("accounts", []) if isinstance(x, dict)}, today, tz, out, renewed, on_strava)
    # Strava follows Garmin's hourly rhythm: refreshed when the run is full for anyone, or when there is no Garmin at all.
    full = not sent.get("accounts") or any(due(x, x.get("signedAt")) for x in old.get("accounts", []) if isinstance(x, dict)) or not old.get("accounts")
    ok_s, n_s = strava(sent_strava, {x.get("id"): x for x in old.get("strava", []) if isinstance(x, dict)}, today, tz, out, renewed, full)

    open(a.out, "w", encoding="ascii").write(lock(out, board) + "\n")
    open(a.tokens_out, "w", encoding="ascii").write(lock(renewed, gkey) + "\n")
    n_delta = sum(len(p["acts"]) + ("profile" in p) + ("segments" in p) for side in DELTA.values() for p in side.values())
    if a.explore_out and n_delta:
        delta = {"at": out["at"], "garmin": [dict(p, id=i, acts=list(p["acts"].values())) for i, p in DELTA["garmin"].items()],
                 "strava": [dict(p, id=i, acts=list(p["acts"].values())) for i, p in DELTA["strava"].items()]}
        open(a.explore_out, "w", encoding="ascii").write(lock(delta, gkey) + "\n")
        print("explorer: %d activities handed over this run" % n_delta)
    print("garmin: %d of %d accounts fetched; strava: %d of %d" % (ok_g, n_g, ok_s, n_s))
    if n_g + n_s and not ok_g + ok_s:
        sys.exit(1)


if __name__ == "__main__":
    main()
