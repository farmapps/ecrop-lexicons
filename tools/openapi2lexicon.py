#!/usr/bin/env python3
"""Convert the schemas of the AgroConnect eCrop OpenAPI document into atproto Lexicons.

Usage: openapi2lexicon.py <ecrop.yaml> <output lexicons dir>

Mapping rules:
- every schema in components/schemas becomes a named def (lowerCamelCase name);
  allOf compositions are flattened into a single object.
- the core business schemas go into <PREFIX>.defs, the geo / OGC API Features
  schemas into <PREFIX>.geo.defs.
- the resources that have their own CRUD operations become record lexicons
  (record = the *Details schema; the server assigned PrimaryId is replaced by the AT-URI).
- floats are not part of the atproto data model: every float becomes an integer
  and the containing object gets an integer `scaleFactor` property, the power of 10
  to apply: realValue = value * 10^scaleFactor.
- nested arrays (GeoJSON coordinates, bbox, interval) are not allowed by every
  Lexicon implementation, so they are wrapped in objects.
"""

import json
import os
import re
import sys

import yaml

PREFIX = "org.farmapps.temp.ecrop"
CORE = f"{PREFIX}.defs"
GEO = f"{PREFIX}.geo.defs"

GEO_SCHEMAS = {
    "CoordRefSysType", "JsonFgTimeType", "PositionType", "PointType", "LineStringType",
    "PolygonType", "MultiPolygonType", "PlotGeometryType", "LandingPage",
    "ConformanceDeclaration", "CollectionExtentType", "Collection", "Collections",
    "PlotFeatureProperties", "PlotFeatureTopLevel", "PlotFeature", "PlotFeatureCollection",
}

RECORDS = {  # record lexicon name -> OpenAPI schema used as record body
    "grower": "GrowerDetails",
    "supplier": "SupplierDetails",
    "contractor": "ContractorDetails",
    "productionLocation": "ProductionLocationDetails",
    "crop": "CropDetails",
    "plot": "PlotDetails",
    "task": "TaskDetails",
    "inboundDeliveryByGrower": "InboundDeliveryDetailsGrower",
    "inboundDeliveryBySupplier": "InboundDeliveryDetails",
    "plotFeature": "PlotFeatureTopLevel",
}

SCALE_FACTOR = {
    "type": "integer",
    "description": "Power of 10 exponent to apply to the integer value(s) of this object: "
                   "realValue = value * 10^scaleFactor. E.g. value 125 with scaleFactor -2 is 1.25.",
}


def def_name(schema_name):
    return schema_name[0].lower() + schema_name[1:]


def ref_name(schema_name):
    return def_name(schema_name)


class Converter:
    def __init__(self, spec):
        self.schemas = spec["components"]["schemas"]

    # ---------------------------------------------------------------- helpers
    def file_of(self, schema_name):
        return GEO if schema_name in GEO_SCHEMAS or schema_name in GEO_EXTRA else CORE

    def ref(self, schema_name, current_file, description=None):
        target = self.file_of(schema_name)
        r = f"#{ref_name(schema_name)}" if target == current_file else f"{target}#{ref_name(schema_name)}"
        out = {"type": "ref", "ref": r}
        if description:
            out["description"] = description
        return out

    @staticmethod
    def describe(schema):
        d = schema.get("description") or schema.get("title")
        return d.replace("\r\n", "\n").strip() if isinstance(d, str) else None

    @staticmethod
    def ref_target(schema):
        """Return the schema name for {$ref} or {allOf: [{$ref}]} shapes, else None."""
        if "$ref" in schema:
            return schema["$ref"].split("/")[-1]
        all_of = schema.get("allOf")
        if all_of and len(all_of) == 1 and "$ref" in all_of[0]:
            return all_of[0]["$ref"].split("/")[-1]
        return None

    def flatten(self, schema):
        """Merge allOf parts (recursively resolving $refs) into one object schema."""
        props, required, nullable = {}, [], []
        parts = [schema]
        while parts:
            part = parts.pop(0)
            if "$ref" in part:
                parts.insert(0, self.schemas[part["$ref"].split("/")[-1]])
                continue
            if "allOf" in part:
                parts[0:0] = part["allOf"]
            for k, v in part.get("properties", {}).items():
                props[k] = v
                if isinstance(v, dict) and v.get("nullable"):
                    nullable.append(k)
            required += [r for r in part.get("required", []) if r not in required]
        return props, required, nullable

    # ------------------------------------------------------------ conversion
    def field(self, schema, current_file):
        """Convert a property / items schema to a Lexicon field type."""
        desc = self.describe(schema) if "$ref" not in schema else None
        target = self.ref_target(schema)
        if target:
            return self.ref(target, current_file, desc)
        if "oneOf" in schema:
            raise ValueError(f"unhandled oneOf: {schema}")
        t = schema.get("type")
        if t is None and "enum" in schema:
            t = "string"
        if t is None and not schema.get("properties"):
            out = {"type": "unknown"}
        elif t == "object" or "properties" in schema:
            out = self.object(schema, current_file)
        elif t == "array":
            out = {"type": "array", "items": self.field(schema["items"], current_file)}
            if "minItems" in schema:
                out["minLength"] = schema["minItems"]
            if "maxItems" in schema:
                out["maxLength"] = schema["maxItems"]
        elif t == "boolean":
            out = {"type": "boolean"}
        elif t == "integer" or (t == "number" and schema.get("format") in ("int32", "int64")):
            out = {"type": "integer"}
            for k in ("minimum", "maximum"):
                if k in schema:
                    out[k] = schema[k]
        elif t == "number":
            # Floats are handled by the containing object (see object()).
            out = {"type": "integer"}
        elif t == "string":
            out = {"type": "string"}
            fmt = schema.get("format")
            if fmt == "date-time":
                out["format"] = "datetime"
            elif fmt == "uri":
                out["format"] = "uri"
            if "enum" in schema:
                if len(schema["enum"]) == 1:
                    out["const"] = schema["enum"][0]
                else:
                    out["knownValues"] = schema["enum"]
            if "minLength" in schema:
                out["minLength"] = schema["minLength"]
            if "default" in schema:
                out["default"] = schema["default"]
            notes = []
            if fmt and fmt not in ("date-time", "uri"):
                notes.append(f"Format: {fmt}.")
            if schema.get("pattern") and "format" not in out:
                notes.append(f"Pattern: {schema['pattern']}")
            if notes:
                desc = " ".join(([desc] if desc else []) + notes)
        else:
            raise ValueError(f"unhandled schema type {t}: {schema}")
        if desc and "description" not in out:
            out = {"type": out.pop("type"), "description": desc, **out}
        return out

    def object(self, schema, current_file):
        props, required, nullable = self.flatten(schema)
        out = {"type": "object"}
        desc = self.describe(schema)
        if desc:
            out["description"] = desc
        properties = {}
        floats = []
        for name, p in props.items():
            if isinstance(p, dict) and p.get("type") == "number" and p.get("format") not in ("int32", "int64"):
                floats.append(name)
                f = self.field(p, current_file)
                f["description"] = ((f.get("description", "") + " ").lstrip() +
                                    "Integer mantissa, scaled by scaleFactor (originally a float).")
                properties[name] = f
            else:
                properties[name] = self.field(p, current_file)
        if floats:
            # scaleFactor is required when a scaled value is required, otherwise it defaults to 0.
            if any(f in required for f in floats):
                properties["scaleFactor"] = dict(SCALE_FACTOR)
                required = required + ["scaleFactor"]
            else:
                properties["scaleFactor"] = dict(SCALE_FACTOR, default=0)
        # Optional + nullable is redundant in Lexicon: absence already means "no value".
        nullable = [n for n in nullable if n in required]
        if required:
            out["required"] = required
        if nullable:
            out["nullable"] = nullable
        out["properties"] = properties
        return out

    def named(self, name, current_file):
        """Convert a component schema into a named def."""
        s = self.schemas[name]
        if name in OVERRIDES:
            return OVERRIDES[name](self, s, current_file)
        if s.get("type") == "array":
            return self.field(s, current_file)
        if "properties" in s or "allOf" in s or s.get("type") == "object":
            return self.object(s, current_file)
        return self.field(s, current_file)


# ------------------------------------------------------------------ overrides
# Schemas whose OpenAPI shape cannot be expressed 1:1 in Lexicon.

def _characteristic(conv, s, f):
    """allOf + oneOf of value properties -> one object with optional value fields."""
    base, value_options = s["allOf"]
    merged = {"properties": dict(base["properties"]), "required": base["required"]}
    names = []
    for option in value_options["oneOf"]:
        merged["properties"].update(option["properties"])
        names += list(option["properties"])
    obj = conv.object(merged, f)
    obj["description"] = (conv.describe(s) or "") + \
        f". Exactly one of {', '.join(names)} must be present."
    return obj


def _position(conv, s, f):
    return {
        "type": "object",
        "description": "A position (GeoJSON [x, y] or [x, y, z]) as integers. The order and meaning of the axes "
                       "depend on the CRS (CRS84: x=lon, y=lat; EPSG:28992: x, y in meters). The real coordinate "
                       "value is value * 10^scaleFactor, where scaleFactor is set on the enclosing geometry.",
        "required": ["x", "y"],
        "properties": {
            "x": {"type": "integer", "description": "First axis, scaled by scaleFactor of the geometry."},
            "y": {"type": "integer", "description": "Second axis, scaled by scaleFactor of the geometry."},
            "z": {"type": "integer", "description": "Optional third axis, scaled by scaleFactor of the geometry."},
        },
    }


def _geometry(kind, coordinates, description):
    def build(conv, s, f):
        return {
            "type": "object",
            "description": description,
            "required": ["type", "scaleFactor", "coordinates"],
            "properties": {
                "type": {"type": "string", "const": kind},
                "scaleFactor": dict(SCALE_FACTOR, description=SCALE_FACTOR["description"] +
                                    " Applies to every coordinate of this geometry, e.g. -7 for CRS84 degrees."),
                "coordinates": coordinates,
            },
        }
    return build


def plot_geometry_union(conv, f, description):
    """PlotGeometryType (oneOf + discriminator) as an inline union: Lexicon does not allow named union defs."""
    return {
        "type": "union",
        "description": description,
        "refs": [conv.ref("PolygonType", f)["ref"], conv.ref("MultiPolygonType", f)["ref"]],
        "closed": True,
    }


def _json_fg_time(conv, s, f):
    return {
        "type": "object",
        "description": "JSON-FG time: an instant (date or timestamp) and/or an interval.",
        "properties": {
            "date": conv.ref("DateType", f),
            "timestamp": conv.ref("DateTimeType", f),
            "interval": {"type": "ref", "ref": "#timeIntervalType"},
        },
    }


def _collection_extent(conv, s, f):
    return {
        "type": "object",
        "properties": {
            "spatial": {
                "type": "object",
                "properties": {
                    "bbox": {"type": "array", "items": {"type": "ref", "ref": "#bboxType"}},
                    "crs": {"type": "string", "format": "uri",
                            "default": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
                },
            },
            "temporal": {
                "type": "object",
                "properties": {
                    "interval": {"type": "array", "items": {"type": "ref", "ref": "#timeIntervalType"}},
                    "trs": {"type": "string", "format": "uri",
                            "default": "http://www.opengis.net/def/crs/OGC/0/GregorianDateTime"},
                },
            },
        },
    }


def _feature(conv, s, f):
    """id: string|number -> string; featureType: string|string[] -> string[]; place/geometry -> union."""
    obj = conv.object({"allOf": [{"properties": {k: v for k, v in s["allOf"][0]["properties"].items()
                                                 if k not in ("id", "featureType", "place", "geometry")},
                                  "required": s["allOf"][0]["required"]},
                                 s["allOf"][1]]}, f)
    props = s["allOf"][0]["properties"]
    new = {}
    for k in props:
        if k == "id":
            new[k] = {"type": "string", "description": "Feature id (a number id is represented as its decimal string)."}
        elif k == "featureType":
            new[k] = {"type": "array", "items": {"type": "string"},
                      "description": "Feature type(s); a single type is a one element array."}
        elif k in ("place", "geometry"):
            new[k] = plot_geometry_union(conv, f, conv.describe(props[k]) +
                                          " Plot geometry types: Polygon or MultiPolygon.")
        else:
            new[k] = obj["properties"][k]
    new.update({k: v for k, v in obj["properties"].items() if k not in new})
    obj["properties"] = new
    obj["nullable"] = ["geometry"]
    return obj


def _feature_collection(conv, s, f):
    obj = conv.object({"allOf": [{"properties": {k: v for k, v in s["allOf"][0]["properties"].items()
                                                 if k != "featureType"},
                                  "required": s["allOf"][0]["required"]},
                                 s["allOf"][1]]}, f)
    obj["properties"]["featureType"] = {"type": "array", "items": {"type": "string"},
                                        "description": "Feature type(s); a single type is a one element array."}
    return obj


def _json_patch(conv, s, f):
    return {
        "type": "array",
        "description": conv.describe(s),
        "minLength": 1,
        "items": {"type": "ref", "ref": "#jsonPatchOperation"},
    }


# Schemas that cannot be a named def and are inlined where they are used.
INLINED = {"PlotGeometryType"}

OVERRIDES = {
    "CharacteristicType": _characteristic,
    "PositionType": _position,
    "PointType": _geometry("Point", {"type": "ref", "ref": "#positionType"}, "GeoJSON Point."),
    "LineStringType": _geometry("LineString",
                                {"type": "array", "minLength": 2, "items": {"type": "ref", "ref": "#positionType"}},
                                "GeoJSON LineString."),
    "PolygonType": _geometry("Polygon",
                             {"type": "array", "minLength": 1, "items": {"type": "ref", "ref": "#linearRingType"}},
                             "GeoJSON Polygon. The first ring is the exterior boundary (counterclockwise), "
                             "every following ring is a hole (clockwise)."),
    "MultiPolygonType": _geometry("MultiPolygon",
                                  {"type": "array", "items": {"type": "ref", "ref": "#polygonCoordinatesType"}},
                                  "GeoJSON MultiPolygon."),
    "JsonFgTimeType": _json_fg_time,
    "CollectionExtentType": _collection_extent,
    "PlotFeatureTopLevel": _feature,
    "PlotFeature": _feature,
    "PlotFeatureCollection": _feature_collection,
    "JsonPatch": _json_patch,
}

# Helper defs that do not exist in the OpenAPI document (needed to avoid nested arrays / null items).
GEO_EXTRA = {
    "linearRingType": {
        "type": "object",
        "description": "A closed LineString with four or more positions; the first and last position are identical.",
        "required": ["positions"],
        "properties": {"positions": {"type": "array", "minLength": 4, "items": {"type": "ref", "ref": "#positionType"}}},
    },
    "polygonCoordinatesType": {
        "type": "object",
        "description": "The rings of one polygon of a MultiPolygon: exterior ring first, then the holes.",
        "required": ["rings"],
        "properties": {"rings": {"type": "array", "minLength": 1, "items": {"type": "ref", "ref": "#linearRingType"}}},
    },
    "bboxType": {
        "type": "object",
        "description": "Bounding box [minx, miny, maxx, maxy] or [minx, miny, minz, maxx, maxy, maxz] as integers; "
                       "real value = value * 10^scaleFactor.",
        "required": ["values", "scaleFactor"],
        "properties": {
            "values": {"type": "array", "minLength": 4, "maxLength": 6, "items": {"type": "integer"}},
            "scaleFactor": dict(SCALE_FACTOR),
        },
    },
    "timeIntervalType": {
        "type": "object",
        "description": "Time interval; an absent start or end means the interval is open at that side "
                       "(null in the OpenAPI [start, end] array).",
        "properties": {
            "start": {"type": "string", "description": "Date or datetime of the start of the interval."},
            "end": {"type": "string", "description": "Date or datetime of the end of the interval."},
        },
    },
}

CORE_EXTRA = {
    "jsonPatchOperation": {
        "type": "object",
        "description": "One RFC 6902 JSON Patch operation. 'value' is required for add, replace and test; "
                       "'from' is required for move and copy.",
        "required": ["op", "path"],
        "properties": {
            "op": {"type": "string", "knownValues": ["add", "remove", "replace", "move", "copy", "test"]},
            "path": {"type": "string"},
            "from": {"type": "string"},
            "value": {"type": "unknown"},
        },
    },
}


def renamed(owner, name, field):
    """Lexicon style: avoid the reserved field names `type` and `id`. Returns the new name or None."""
    if name == "id":
        if field.get("ref", "").endswith("#primaryIdType"):
            return "primaryId"
        for key, new in (("worker", "workerId"), ("collection", "collectionId"), ("eature", "featureId")):
            if key in owner:
                return new
    if name == "type":
        if "const" in field:
            return "geoJsonType"
        for key, new in (("problemDetails", "problemType"), ("link", "mediaType"), ("plot", "plotType"),
                         ("operation", "operationType"), ("llocation", "allocationType"),
                         ("equipment", "equipmentType")):
            if key in owner:
                return new
    if name in ("id", "type"):
        raise ValueError(f"no rename rule for {owner}.{name}")
    return None


def rename_reserved(owner, schema):
    """Recursively rename reserved field names in an object schema (and nested objects/arrays)."""
    if schema.get("type") == "array":
        rename_reserved(owner, schema["items"])
    if schema.get("type") == "record":
        rename_reserved(owner, schema["record"])
    if schema.get("type") != "object":
        return
    new_props = {}
    for name, field in schema["properties"].items():
        rename_reserved(owner, field)
        new = renamed(owner, name, field)
        if new:
            note = f"(OpenAPI property name: '{name}')"
            field["description"] = f"{field['description']} {note}" if field.get("description") else note
            for key in ("required", "nullable"):
                if key in schema:
                    schema[key] = [new if n == name else n for n in schema[key]]
        new_props[new or name] = field
    schema["properties"] = new_props


def lexicon(nsid, defs, description=None):
    for name, d in defs.items():
        rename_reserved(nsid.split(".")[-1] if name == "main" else name, d)
    doc = {"lexicon": 1, "id": nsid}
    if description:
        doc["description"] = description
    doc["defs"] = defs
    return doc


def write(out_dir, doc):
    path = os.path.join(out_dir, *doc["id"].split(".")) + ".json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def main(spec_path, out_dir):
    with open(spec_path) as fh:
        spec = yaml.safe_load(fh)
    conv = Converter(spec)
    version = spec["info"]["version"]

    core, geo = {}, {}
    for name in conv.schemas:
        if name in INLINED:
            continue
        target = core if conv.file_of(name) == CORE else geo
        target[def_name(name)] = conv.named(name, conv.file_of(name))
    core.update(CORE_EXTRA)
    geo.update(GEO_EXTRA)

    written = [
        write(out_dir, lexicon(CORE, core, f"Shared types of the AgroConnect eCrop API {version} (converted from OpenAPI).")),
        write(out_dir, lexicon(GEO, geo, f"Geo (GeoJSON / JSON-FG / OGC API Features) types of the AgroConnect eCrop API {version}.")),
    ]
    for rec, schema_name in RECORDS.items():
        nsid = f"{PREFIX}.{rec}"
        body = conv.named(schema_name, nsid)
        desc = body.pop("description", None) or conv.describe(conv.schemas[schema_name]) or \
            f"A {rec} record (OpenAPI schema {schema_name})."
        main_def = {"type": "record", "description": desc, "key": "tid", "record": body}
        written.append(write(out_dir, lexicon(nsid, {"main": main_def})))
    for p in written:
        print(p)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
