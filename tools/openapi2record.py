#!/usr/bin/env python3
"""Convert an eCrop OpenAPI JSON instance into an atproto record of the generated Lexicons.

Usage: openapi2record.py <lexicons dir> <record nsid> <instance.json|->

The conversion is driven by the Lexicon schema:
- floats in integer fields are scaled; the containing object gets the smallest
  `scaleFactor` (power of 10 exponent) that represents all its values exactly;
- GeoJSON coordinate arrays become position / ring objects;
- renamed properties (see "OpenAPI property name" in the descriptions) are mapped back;
- the server assigned `id` (PrimaryId) of the top level resource is dropped,
  in atproto the AT-URI of the record identifies it.
"""

import json
import os
import re
import sys
from decimal import Decimal

RENAME_NOTE = re.compile(r"\(OpenAPI property name: '([^']+)'\)")


class Catalog:
    def __init__(self, root):
        self.docs = {}
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f.endswith(".json"):
                    with open(os.path.join(dirpath, f)) as fh:
                        doc = json.load(fh)
                    self.docs[doc["id"]] = doc

    def resolve(self, ref, current):
        nsid, _, name = ref.partition("#")
        nsid = nsid or current
        return self.docs[nsid]["defs"][name or "main"], nsid, name or "main"


def decimals(value):
    exp = Decimal(repr(value)).normalize().as_tuple().exponent
    return max(0, -exp)


def scale(value, dec):
    return int((Decimal(repr(value)) * (Decimal(10) ** dec)).to_integral_value())


def numbers(data):
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        yield data
    elif isinstance(data, list):
        for d in data:
            yield from numbers(d)


class Converter:
    def __init__(self, catalog):
        self.cat = catalog

    def convert(self, data, schema, nsid, name=None, dec=0):
        t = schema["type"]
        if t == "ref":
            target, tnsid, tname = self.cat.resolve(schema["ref"], nsid)
            return self.convert(data, target, tnsid, tname, dec)
        if data is None:
            return None
        if t == "union":
            for ref in schema["refs"]:
                target, tnsid, tname = self.cat.resolve(ref, nsid)
                const = target["properties"].get("geoJsonType", {}).get("const")
                if const == data.get("type"):
                    out = self.convert(data, target, tnsid, tname)
                    return {"$type": f"{tnsid}#{tname}", **out}
            raise ValueError(f"no union member for {data.get('type')}")
        if t == "object":
            return self.object(data, schema, nsid, name, dec)
        if t == "array":
            if not isinstance(data, list):
                data = [data]
            return [self.convert(d, schema["items"], nsid, None, dec) for d in data]
        if t == "integer":
            return scale(data, dec) if dec else int(data)
        if t == "string" and not isinstance(data, str):
            return str(data)
        return data

    def object(self, data, schema, nsid, name, dec):
        props = schema["properties"]
        # Wrapper objects for GeoJSON arrays.
        if name == "positionType":
            return {k: scale(v, dec) for k, v in zip(("x", "y", "z"), data)}
        if name == "linearRingType":
            return {"positions": [self.convert(p, props["positions"]["items"], nsid, None, dec) for p in data]}
        if name == "polygonCoordinatesType":
            return {"rings": [self.convert(r, props["rings"]["items"], nsid, None, dec) for r in data]}
        if name == "bboxType":
            d = max(map(decimals, data))
            return {"values": [scale(v, d) for v in data], "scaleFactor": -d}
        if name == "timeIntervalType":
            return {k: v for k, v in zip(("start", "end"), data) if v is not None}

        source = {}
        for prop, field in props.items():
            m = RENAME_NOTE.search(field.get("description", ""))
            source[prop] = m.group(1) if m else prop

        out = {}
        if "scaleFactor" in props and "scaleFactor" not in data:
            if "coordinates" in props:
                dec = max(map(decimals, numbers(data["coordinates"])), default=0)
            else:
                dec = max((decimals(data[source[p]]) for p, f in props.items()
                           if f["type"] == "integer" and p != "scaleFactor" and source[p] in data), default=0)
            out["scaleFactor"] = -dec
        for prop, field in props.items():
            if source[prop] in data and prop != "scaleFactor":
                out[prop] = self.convert(data[source[prop]], field, nsid, None, dec)
        unknown = set(data) - set(source.values()) - {"scaleFactor"}
        if unknown:
            print(f"note: dropping properties not in the lexicon ({name or 'object'}): {sorted(unknown)}",
                  file=sys.stderr)
        return out


def main(lex_dir, record_nsid, path):
    catalog = Catalog(lex_dir)
    data = json.load(sys.stdin if path == "-" else open(path))
    if isinstance(data.get("id"), dict):  # server assigned PrimaryId
        data.pop("id")
    record_schema = catalog.docs[record_nsid]["defs"]["main"]["record"]
    record = {"$type": record_nsid, **Converter(catalog).convert(data, record_schema, record_nsid)}
    json.dump(record, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
