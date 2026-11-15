"""Fetch the family's Garmin numbers and write the locked fitness file for Glance Digital Wall Calendar.

Run by the scheduled GitHub job (.github/workflows/fitness.yml). It reads three
secrets from the environment:

  GARMIN_TOKENS  who is tracked and each person's Garmin sign-in token, sent by
                 the Manager's Fitness tab when someone signs in there
  GARMIN_KEY     the key the renewed tokens are kept locked with between runs.
                 It is not the display key: that one is in the TV's address.
  BOARD_KEY      the display key, which locks the numbers the board reads

and writes two files for the "fitness" branch: fitness.enc (steps, body
battery and sleep, locked with the display key) and tokens.enc (the tokens as
Garmin renewed them, locked with GARMIN_KEY).

Each person keeps a history of up to 90 days. Every run fetches the last
seven days again (today keeps changing, and a watch can sync late) and fills
in up to BACKFILL older days not fetched yet, so a new person's history fills
in over a few runs without one long run. The history rides along in
tokens.enc between runs and is published in fitness.enc for the display.

Garmin's own Health API is for businesses only, so this uses the unofficial
garminconnect library; Garmin can change things and break it.

This script must never print a name, a number or a token: the job's log is
public. It prints counts and nothing else.

    python build_fitness.py --old-tokens old.enc --out fitness.enc --tokens-out tokens.enc
"""
import os, sys, json, base64, argparse, datetime

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DAYS = 7          # fetched again on every run
HISTORY = 90      # days kept per person
BACKFILL = 21     # older days filled in per run
KEEP = ("d", "steps", "goal", "bb", "bbHigh", "sleep", "sleepH")


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


def fetch(g, dates):
    days = []
    for d in dates:
        row = {"d": d}
        try:
            s = g.get_user_summary(d) or {}
            row.update({"steps": s.get("totalSteps"), "goal": s.get("dailyStepGoal"), "bb": s.get("bodyBatteryMostRecentValue"),
                        "bbHigh": s.get("bodyBatteryHighestValue"), "bbLow": s.get("bodyBatteryLowestValue")})
        except Exception:
            pass
        try:
            sl = g.get_sleep_data(d) or {}
            row["sleep"] = pick(sl, "dailySleepDTO", "sleepScores", "overall", "value")
            secs = pick(sl, "dailySleepDTO", "sleepTimeSeconds")
            row["sleepH"] = round(secs / 3600.0, 1) if isinstance(secs, (int, float)) else None
        except Exception:
            pass
        days.append({k: v for k, v in row.items() if k in KEEP and v is not None})
    return days


def merge(old, new, today):
    """The history, newest first: new rows replace old ones for the same day."""
    first = (today - datetime.timedelta(days=HISTORY - 1)).isoformat()
    by = {r["d"]: r for r in old if isinstance(r, dict) and r.get("d")}
    by.update({r["d"]: r for r in new})
    return [by[d] for d in sorted(by, reverse=True) if d >= first]


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
    old = {}
    if a.old_tokens and os.path.exists(a.old_tokens):
        try:
            old = unlock(open(a.old_tokens, encoding="ascii").read(), gkey)
        except Exception:
            old = {}                                  # unreadable or a different key: start from what was sent
    kept = {x.get("id"): x for x in old.get("accounts", []) if isinstance(x, dict)}

    from garminconnect import Garmin
    today = datetime.date.today()
    tz = os.environ.get("TZ_NAME")
    if tz:
        try:
            from zoneinfo import ZoneInfo
            today = datetime.datetime.now(ZoneInfo(tz)).date()
        except Exception:
            pass
    out = {"at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "people": []}
    renewed = {"accounts": []}
    ok = 0
    for n, x in enumerate(sent.get("accounts", []), 1):
        # A token renewed by an earlier run is newer than the one the Manager
        # sent, unless the person has signed in again since (a new signedAt).
        prev = kept.get(x.get("id"))
        token = prev["tokens"] if prev and prev.get("signedAt") == x.get("signedAt") and prev.get("tokens") else x.get("tokens")
        person = {"id": x.get("id"), "name": x.get("name", ""), "color": x.get("color", "#4da3ff"), "days": [], "hist": []}
        entry = dict(x, tokens=token)
        hist = (prev or {}).get("hist") or (prev or {}).get("days") or []
        have = {r.get("d") for r in hist if isinstance(r, dict)}
        recent = [(today - datetime.timedelta(days=k)).isoformat() for k in range(DAYS)]
        older = [(today - datetime.timedelta(days=k)).isoformat() for k in range(DAYS, HISTORY)]
        older = [d for d in older if d not in have][:BACKFILL]
        try:
            g = Garmin()
            g.login(token)
            hist = merge(hist, fetch(g, recent + older), today)
            entry["tokens"] = g.client.dumps()
            ok += 1
            print("account %d: ok, %d days kept" % (n, len(hist)))
        except Exception as ex:
            print("account %d: FAILED (%s)" % (n, type(ex).__name__))
            hist = merge(hist, [], today)                    # keep showing the last numbers, not a hole
        person["hist"] = hist
        person["days"] = [r for r in hist if r["d"] in recent]
        entry.pop("days", None)
        entry["hist"] = hist
        renewed["accounts"].append(entry)
        out["people"].append(person)

    open(a.out, "w", encoding="ascii").write(lock(out, board) + "\n")
    open(a.tokens_out, "w", encoding="ascii").write(lock(renewed, gkey) + "\n")
    print("%d of %d accounts fetched" % (ok, len(sent.get("accounts", []))))
    if sent.get("accounts") and not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
