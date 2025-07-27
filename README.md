# data.cons.fyi

This repository contains all the convention data for [cons.fyi](https://cons.fyi).

Each convention is modeled by a **con** record, that contains a list of **events** that describes a specific instance of a convention.

```typescript
/// Event is a specific instance of a convention.
interface Event {
  /// Globally unique ID. It should include the convention name, e.g. `rainfurrest-2016`.
  id: string;

  /// The human-readable name of the convention instance, e.g. "RainFurrest 2016".
  name: string;

  /// Link to the convention website.
  url: string;

  /// The start date of the convention.
  startDate: string;

  /// The end date of the convention, inclusive.
  endDate: string;

  /// The human-readable location. This may be a virtual location e.g. "VRChat".
  location: string;

  /// The country, if any. This may be unset for e.g. virtual conventions.
  country?: string;

  /// The GPS coordinates of the venue, if any. This may be unset for e.g. virtual conventions.
  latLng?: [number, number];

  /// Sources this data is from.
  sources?: string[];
}

/// Con is a collection of events describing a convention.
interface Con {
  /// The human-readable name for the convention.
  name: string;

  /// All instances of the convention.
  events: Event[];
}
```

## Files

The following files are available at `https://data.cons.fyi`:
- `/active.json`: A JSON file containing all current or upcoming events.
- `/cons.json`: Names of all the cons in `/cons`.
- `/events.json`: Names of all the events in `/events`.
- `/calendar.ics`: All the active events in an ICS calendar.
- `/cons/$ID.json`: JSON files of every con.
- `/events/$ID.json`: JSON files of every event.

Events will have the following fields materialized:
- `relatedEventIds`: The IDs of all other events for the convention.
- `timezone`: The IANA timezone ID, if `latLng` is present.

## Usage of data from FanCons.com

Note that data regularly gets imported from [FanCons.com](https://fancons.com) for updates. Usage restrictions may apply for any con data that is annotated as being sourced from FanCons.com.
