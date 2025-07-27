# data.cons.fyi

This repository ([github.com/consfyi/data](https://github.com/consfyi/data)) contains all the furry convention data for [cons.fyi](https://cons.fyi).

If there is an issue with data accuracy, you may [file an issue here](https://github.com/consfyi/data/issues/new?template=missing-or-incorrect-convention.md).

## Usage policy

Attribution is not required but appreciated. This helps us keep our data up to date, which in turn helps everyone else!

Please note that some data is imported from [FanCons.com](https://fancons.com) and is annotated as such. Usage restrictions may apply.

## Data model

### Input

Each convention is modeled by a `Con` record in ([github.com/consfyi/data](https://github.com/consfyi/data)).

```typescript
/// Event is a specific instance of a convention.
interface Event {
  /// Globally unique ID. It should include the convention name, e.g. `rainfurrest-2016`.
  id: string;

  /// The human-readable name of the convention instance, e.g. "RainFurrest 2016".
  name: string;

  /// Link to the convention website.
  url: string;

  /// The start date of the convention instance. Should be in yyyy-MM-dd format.
  startDate: string;

  /// The end date of the convention instance, inclusive. Should be in yyyy-MM-dd format.
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

### Output

The following files are generated at `https://data.cons.fyi`:
- [/active.json](/active.json): A JSON file containing all current or upcoming events.
- [/calendar.ics](/calendar.ics): All the active events in an ICS calendar.
- [/cons/](/cons/): JSON files of every `Con` record.
- [/cons.json](/cons.json): IDs of all `Con`s.
- [/events/](/events/): JSON files of every `Event` record, extracted from `Con` records.
- [/events.json](/events.json): IDs of all `Event`s.

`Event`s will have the following additional fields materialized:
- `conId: string`: The ID of the con this corresponds to.
- `timezone: string`: The IANA timezone ID, if `latLng` is present.
