# Glance Digital Wall Calendar

The display on the kitchen wall: clock, weather, the family calendar, photos,
scores and the Chore Factory chore list, laid out however the household
likes.

## How it works

| | |
|---|---|
| `index.html` | the display page, served by GitHub Pages |
| `layout.enc` | the layout (screens, the modules on each, devices), encrypted |
| `photos/` | picture files for the Photos module (these are public, like everything here) |
| `tools/build_calendar.py` | fetches the calendars and the photo albums' picture lists, and writes the encrypted data file |
| `.github/workflows/calendar.yml` | runs that every half hour |
| branch `data` | holds `calendar.enc`, the encrypted data file, and nothing else |

This repository is public, so nothing about the household is stored in it as
readable text. The layout, including any text and web addresses typed into
modules, is published only as `layout.enc`. Calendar events, the home
location and the albums' picture addresses are published only inside
`calendar.enc`. Both are encrypted; the key is in the address each display
opens (after the `#`, a part of an address that browsers never send to a
server) and in the repository's secrets. The calendar addresses, location and
album links themselves are repository secrets.

Weather comes from Open-Meteo or the US National Weather Service, whichever
the layout names. Scores come from ESPN's public schedule feed. The chore
list comes from the Chore Factory feed.

## Screens, modules and devices

A layout has up to four screens. A screen holds modules (date and time,
weather, forecast, calendar, Chore Factory, photos, sports, radar or web picture,
countdown, text), each placed by percentages of the screen so one layout fits
any resolution. A device is one display; it shows the screens assigned to it
and rotates through them if there is more than one. A display chooses its
device with `&device=<id>` in its address.

## Changing things

Everything is edited in the Glance canvas, a local editor on the home PC
(started from the Server Hub; it is not part of this repository). Its Publish
button encrypts the layout and sends it here. Displays check every ten
minutes and apply a changed layout by themselves.
