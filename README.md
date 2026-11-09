# The Daily

The display on the kitchen TV: clock, weather, the family calendar and the
Chore Factory "Next Up" list, on one page.

## How it works

| | |
|---|---|
| `index.html` | the board itself, served by GitHub Pages |
| `config.json` | which blocks show, the photo list, where the chore list comes from |
| `photos/` | pictures shown behind the forecast (these are public, like everything here) |
| `tools/build_calendar.py` | fetches the calendars and writes the encrypted data file |
| `.github/workflows/calendar.yml` | runs that every half hour |
| branch `data` | holds `calendar.enc`, the encrypted data file, and nothing else |

This repository is public, so nothing private is stored in it as readable
text. The calendar addresses and the home location are repository secrets.
The calendar events are published only as `calendar.enc`, which is encrypted;
the key is in the address the TV opens (after the `#`, a part of an address
that browsers never send to a server) and in the repository's secrets.

Weather comes straight from Open-Meteo. The chore list comes from the Chore
Factory feed.

## Changing things

- **Hide or show a block:** set it to `false` or `true` under `blocks` in `config.json`.
- **Photos:** put image files in `photos/` and list their names under `photos` in `config.json`.
- **Calendars or location:** edit `private/settings.txt` on the home PC (it is not in this repository) and run `update.cmd`.

The TV checks hourly for a changed page and reloads itself.
