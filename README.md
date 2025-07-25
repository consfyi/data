# data.cons.fyi

This repository contains all the convention data for [cons.fyi](https://cons.fyi).

The following files are materialized:
- `/active.json`: A JSON file containing all current or upcoming cons. `timezone` will be materialized in this file.
- `/index.json`: Names of all the cons in `/cons`.
- `/calendar.ics`: All the active events in an ICS calendar.
- `/cons/$ID.json`: JSON files of every con.

## Usage of data from FanCons.com

Note that data regularly gets imported from FanCons.com for updates. This data is indicated by `"source": "fancons.com"` in the JSON file.

However, any con that has already been imported will not be touched, unless the con has been canceled.
