# data.cons.fyi

This repository contains all the convention data for [cons.fyi](https://cons.fyi).

The following files are materialized:
- [data.cons.fyi/active.json](https://data.cons.fyi/active.json): A JSON file containing all current or upcoming cons.

## Usage of data from FanCons.com

Note that data regularly gets imported from FanCons.com for updates. This data is indicated by `source = "fancons.com"` in the TOML file.

However, any con that has already been imported will not be touched, unless the con has been canceled.

For any TOML file annotated with `source = "fancons.com"`, you must be compliant with FanCons.com's terms of use.
