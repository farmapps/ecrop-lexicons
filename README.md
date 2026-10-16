# eCrop atproto Lexicons

atproto Lexicons for the [AgroConnect eCrop API](https://github.com/AgroConnectNL/eCrop/blob/main/openapi/ecrop.yaml)
(OpenAPI 1.1.0, eCrop standard 1.0.2), generated from `openapi/ecrop.yaml`.

| Lexicon | Contents |
|---|---|
| `org.farmapps.temp.ecrop.defs` | All business / shared schemas as named defs (ids, code, measure, characteristic, address, party, crop, plot, task, operation, allocation, product, equipment, worker, problem details, link, JSON Patch, …) |
| `org.farmapps.temp.ecrop.geo.defs` | GeoJSON / JSON-FG / OGC API Features schemas (position, point, lineString, polygon, multiPolygon, plot feature (collection), collection, extent, …) |
| `org.farmapps.temp.ecrop.grower` `.supplier` `.contractor` `.productionLocation` `.crop` `.plot` `.task` `.inboundDeliveryByGrower` `.inboundDeliveryBySupplier` `.plotFeature` | Record types (`key: tid`) for the API resources. The record body is the `*Details` schema; the server-assigned `PrimaryId` is replaced by the record's AT-URI. |

Def names are the OpenAPI schema names in lowerCamelCase (`MeasureType` → `#measureType`).

## Mapping rules

**Floats → integers + `scaleFactor`.** The atproto data model has no floats. Each float property becomes an
integer, and the object that contains it gets an integer `scaleFactor`: the exponent of a power of 10,
so `realValue = value × 10^scaleFactor`.

| Schema | Scaled fields | `scaleFactor` |
|---|---|---|
| `measureType` | `content` | required |
| `plantSpecies` | `precentageMix` | optional, default 0 |
| `pointType`, `lineStringType`, `polygonType`, `multiPolygonType` | every coordinate | required, one per geometry |
| `bboxType` | `values` | required |

Example: `{"content": 0.9, "unitCode": …}` → `{"content": 9, "scaleFactor": -1, "unitCode": …}`.

**No nested arrays.** Not every Lexicon implementation supports nested arrays, so they are wrapped in objects:

| OpenAPI | Lexicon |
|---|---|
| position `[x, y, z?]` | `positionType {x, y, z?}` |
| Polygon `coordinates: [[pos]]` | `coordinates: [linearRingType {positions: [positionType]}]` |
| MultiPolygon `coordinates: [[[pos]]]` | `coordinates: [polygonCoordinatesType {rings: [linearRingType]}]` |
| extent `bbox: [[n]]` | `bbox: [bboxType {values: [int], scaleFactor}]` |
| `interval: [start\|null, end\|null]` | `timeIntervalType {start?, end?}` |

**oneOf / unions.**
- `PlotGeometryType` (Polygon | MultiPolygon) becomes an inline closed `union` on `place` and `geometry`, with `$type` as the discriminator. Lexicon does not allow a union as a named def.
- `CharacteristicType` value `oneOf` becomes optional `stringValue` … `measureValue` fields. "Exactly one of them" is stated in the description, because Lexicon cannot enforce it.
- Feature `id` (string | number) becomes a string. `featureType` (string | string[]) becomes string[].

**Reserved field names.** The goat linter flags `type` and `id`, so they are renamed. The original name is noted in
each field's description ("OpenAPI property name: …"):

| OpenAPI | Lexicon |
|---|---|
| `id` (PrimaryId) | `primaryId` |
| `Worker.id` / `Collection.id` / feature `id` | `workerId` / `collectionId` / `featureId` |
| GeoJSON `type` (`Point`, `Feature`, …) | `geoJsonType` (`const`) |
| `type` of plot / operation / allocation / equipment / problem / link | `plotType` / `operationType` / `allocationType` / `equipmentType` / `problemType` / `mediaType` |

**Other.**
- `allOf` is flattened.
- `enum` becomes `knownValues`, or `const` when there is a single value.
- `date-time` becomes `format: datetime`, `uri` becomes `format: uri`, and `NsidType` becomes `format: nsid`.
- Formats and patterns Lexicon has no equivalent for (date, time, uuid, year pattern) are kept in the description.
- Primitive and enum schemas (`dateType`, `plotType`, …) are named `string` / `integer` defs, referenced with `ref`.
- `nullable` is kept only on required fields (`plotFeature.geometry`). Optional + nullable is redundant in Lexicon.
- `uniqueItems` and string `pattern` cannot be expressed in Lexicon.

## Tools

```sh
# regenerate the lexicons
python3 tools/openapi2lexicon.py openapi/ecrop.yaml lexicons

# convert an eCrop JSON payload into a record (scales floats, reshapes geometry, renames fields)
python3 tools/openapi2record.py lexicons org.farmapps.temp.ecrop.plot payload.json

# test: goat lint + parse, validate all OpenAPI examples as records, negative tests
GOAT=/path/to/goat tests/run.sh
```

`tests/run.sh` needs [goat](https://github.com/bluesky-social/goat), `jq` and Python 3 with PyYAML.
`examples/openapi/` holds the examples taken from the OpenAPI document. `examples/records/` holds them converted to
records, and `examples/map.tsv` says which record type each example is validated against.
