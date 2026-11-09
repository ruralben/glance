# Glance Digital Wall Calendar

The display on the kitchen TV: clock, weather, the family calendar and the
Chore Factory "Next Up" list, on one page.

## How it works

| | |
|---|---|
| `index.html` | the display page, served by GitHub Pages |
| `config.json` | the layout: screens, the modules on each, devices, weather source |
| `photos/` | picture files for the Photos module (these are public, like everything here) |
| `tools/build_calendar.py` | fetches the calendars and the photo album's picture list, and writes the encrypted data file |
| `.github/workflows/calendar.yml` | runs that every half hour |
| branch `data` | holds `calendar.enc`, the encrypted data file, and nothing else |

This repository is public, so nothing private is stored in it as readable
text. The calendar addresses, the home location and the photo album link are
repository secrets. Calendar events, the location and the album's picture
addresses are published only inside `calendar.enc`, which is encrypted; the
key is in the address each display opens (after the `#`, a part of an address
that browsers never send to a server) and in the repository's secrets.

Weather comes from Open-Meteo or the US National Weather Service, whichever
the layout names. The chore list comes from the Chore Factory feed.

## Screens, modules and devices

A layout has up to four screens. A screen holds modules (date and time,
weather, forecast, calendar, Next Up, photos, text, countdown), each placed by
percentages of the screen so one layout fits any resolution. A device is one
display; it shows the screens assigned to it and rotates through them if there
is more than one. A display chooses its device with `&device=<id>` in its
address.

## Changing things

Everything is edited in the Glance canvas, a local editor on the home PC
(started from the Server Hub; it is not part of this repository). It writes
`config.json` and the private settings, and its Publish button sends them
here. Displays check every ten minutes and apply a changed layout by
themselves.
