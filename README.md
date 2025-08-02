# data.cons.fyi

This is the data for [cons.fyi](https://cons.fyi). It was generated at <span id="timestamp"></span><script type="module">document.getElementById('timestamp').textContent = new Date(await (await fetch('/timestamp')).text()).toString();</script>.

If there is an issue with data accuracy, you may [file an issue here](https://github.com/consfyi/data/issues/new?template=missing-or-incorrect-convention.md).

## Usage policy

Attribution is not required but appreciated. This helps us keep our data up to date, which in turn helps everyone else!

## Data sources

- The majority of data is imported from [FanCons.com](https://fancons.com) and is annotated as such. Usage restrictions may apply.

- Conventions that use [ConCat](https://concat.app) will have data imported directly from their ConCat instance.

## Data model

### Input

Each convention series is modeled by a `Series` record in [github.com/consfyi/data](https://github.com/consfyi/data), one record per `.json` file. The name of the file is the unique ID of the convention series.

```typescript
/// Series is a collection of events describing a convention series.
interface Series {
  /// The human-readable name for the convention series.
  name: string;

  /// All instances of the convention series.
  events: Event[];
}

/// Event is a specific instance of a convention.
interface Event {
  /// Unique ID across all events, including events in other series.
  /// It should include the convention name, e.g. `rainfurrest-2016`.
  id: string;

  /// The human-readable name of the convention instance, e.g. "RainFurrest 2016".
  name: string;

  /// Link to the convention website.
  url: string;

  /// The start date of the convention instance.
  /// Should be in ISO 8601 yyyy-MM-dd format.
  startDate: string;

  /// The end date of the convention instance, inclusive.
  /// Should be in ISO 8601 yyyy-MM-dd format.
  endDate: string;

  /// The human-readable name of the venue.
  venue: string;

  /// The address of the venue, if it is a physical venue.
  /// The granularity of this is not specified, it does not need to be exact. It should include the name of the country.
  address?: string;

  /// The country as an ISO 3166-1 alpha-2 code.
  /// This may be unset for e.g. virtual conventions.
  country?: string;

  /// The GPS coordinates of the venue.
  /// This may be unset for e.g. virtual conventions.
  latLng?: [number, number];

  /// If the convention instance has been canceled.
  canceled?: boolean,

  /// The number of attendees for historical cons.
  attendance?: number,

  /// Sources this data is from.
  sources?: string[];
}
```

### Output

The following files are materialized at `https://data.cons.fyi`:
- [/current.json](/current.json): All current and upcoming events as a list, with 7 days of leading history.
- [/last.json](/last.json): The most recent event of each convention as a list.
- [/calendar.ics](/calendar.ics): All the active events as an ICS calendar.
- [/events/](/events/): JSON files of every `Event` record, extracted from `Series` records.
- [/events.json](/events.json): IDs of all `Event`s.
- [/series/](/series/): JSON files of every `Series` record.
- [/series.json](/series.json): IDs of all `Series`.
- [/timestamp](/timestamp): Timestamp for when this data was materialized.

They will be emitted as materialized records which will contain additional details:

```typescript
/// MaterializedSeries is a materialized version of Series.
interface MaterializedSeries extends Series {
  /// All instances of the convention.
  events: MaterializedEvent[];
}

/// MaterializedEvent is a materialized version of Event.
interface MaterializedEvent extends Event {
  /// The ID of the series this corresponds to.
  seriesId: string;

  /// The IANA timezone ID.
  timezone?: string;

  /// The attendance of the previous event in the series.
  previousAttendance?: number;
}
```
