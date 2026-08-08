"""BIM data extraction: hierarchy, property/quantity sets, materials, types,
units, classifications, connections and systems — everything the geometry API
doesn't cover.

All functions take a webifc.Model. Express IDs are plain ints throughout, so
results compose with model.get_line / model.flat_mesh directly. Robustness
contract: definitions this module doesn't model come back under a "raw" key,
dangling references become {"dangling": id}, cyclic data becomes
{"cycle": id} — data is flagged, never silently dropped, and one bad entity
never aborts a whole extraction.

One-shot dump:

    import webifc
    from webifc import bim
    with webifc.open("model.ifc") as m:
        data = bim.extract(m)   # tree + per-element BIM data + connections + units
"""

from __future__ import annotations

from ._webifc import LABEL, WebIfcError

# (type name, relating_arg, related_arg) — argument positions after the shared
# IfcRoot prefix; identical across IFC2X3 / IFC4 / IFC4X3 for these types.
_CONNECTION_SPECS = [
    ("IFCRELVOIDSELEMENT", 4, 5),                    # host element -> opening
    ("IFCRELFILLSELEMENT", 4, 5),                    # opening -> filling element
    ("IFCRELPROJECTSELEMENT", 4, 5),                 # host element -> projection
    ("IFCRELCONNECTSELEMENTS", 5, 6),
    ("IFCRELCONNECTSPATHELEMENTS", 5, 6),
    ("IFCRELCONNECTSWITHREALIZINGELEMENTS", 5, 6),
    ("IFCRELCONNECTSPORTS", 4, 5),
    ("IFCRELCONNECTSPORTTOELEMENT", 4, 5),
    ("IFCRELCOVERSBLDGELEMENTS", 4, 5),
    ("IFCRELCOVERSSPACES", 4, 5),
    ("IFCRELSERVICESBUILDINGS", 4, 5),
]

_SPACE_BOUNDARY_TYPES = [
    "IFCRELSPACEBOUNDARY",
    "IFCRELSPACEBOUNDARY1STLEVEL",
    "IFCRELSPACEBOUNDARY2NDLEVEL",
]

_GROUP_ASSIGNMENT_TYPES = ["IFCRELASSIGNSTOGROUP", "IFCRELASSIGNSTOGROUPBYFACTOR"]


def _scan(model, rel_type, arg_a=4, arg_b=5):
    """scan_relationship tolerant of types absent from the model's schema
    (IFC4-only names on an IFC2X3 file)."""
    try:
        return model.scan_relationship(rel_type, arg_a, arg_b)
    except WebIfcError:
        return []


def unwrap(v):
    """Collapse get_line value wrappers to plain Python values.

    {"type": REAL, "value": x} -> x; typed measures (LABEL wrappers like
    IFCLENGTHMEASURE(2.4)) -> 2.4; REF wrappers -> the express ID (int);
    lists recurse. Enums arrive already unwrapped (True/False/None/str).
    The measure's type name is dropped here — project units come from units().
    """
    if isinstance(v, dict):
        if v.get("type") == LABEL:
            return unwrap(v.get("value"))
        return unwrap(v.get("value")) if "value" in v else v
    if isinstance(v, list):
        return [unwrap(x) for x in v]
    return v


def _safe_line(model, eid):
    try:
        return model.get_line(int(eid))
    except (WebIfcError, TypeError, ValueError):
        return None


def _args_of(model, eid):
    line = _safe_line(model, eid)
    return line["arguments"] if line else None


def _arg(args, i):
    return unwrap(args[i]) if args and len(args) > i else None


def _as_list(refs):
    """Flatten a scan result value (None | int | possibly-nested list) to ints."""
    if refs is None:
        return []
    if isinstance(refs, int):
        return [refs]
    out = []
    for r in refs:
        out.extend(_as_list(r))
    return out


# ---- property / quantity sets ---------------------------------------------


def _read_property(model, session, pid, seen):
    """One IfcProperty -> (name, value). Complex properties nest as dicts."""
    if pid in seen:
        return None, {"cycle": int(pid)}
    seen = seen | {pid}
    line = _safe_line(model, pid)
    if line is None:
        return None, {"dangling": int(pid)}
    tname = session.type_name(line["type"]).upper()
    args = line["arguments"]
    name = _arg(args, 0)
    if tname in ("IFCPROPERTYSINGLEVALUE", "IFCPROPERTYENUMERATEDVALUE", "IFCPROPERTYLISTVALUE"):
        return name, _arg(args, 2)
    if tname == "IFCPROPERTYBOUNDEDVALUE":
        return name, {"upper": _arg(args, 2), "lower": _arg(args, 3)}
    if tname == "IFCCOMPLEXPROPERTY":
        # (Name, Description, UsageName, HasProperties)
        nested = {}
        for sub in _as_list(_arg(args, 3)):
            k, v = _read_property(model, session, sub, seen)
            nested[k] = v
        return name, nested
    return name, {"raw": line}


def _read_quantity(model, session, qid, seen):
    """One IfcPhysicalQuantity -> (name, value)."""
    if qid in seen:
        return None, {"cycle": int(qid)}
    seen = seen | {qid}
    line = _safe_line(model, qid)
    if line is None:
        return None, {"dangling": int(qid)}
    tname = session.type_name(line["type"]).upper()
    args = line["arguments"]
    name = _arg(args, 0)
    if tname == "IFCPHYSICALCOMPLEXQUANTITY":
        # (Name, Description, HasQuantities, Discrimination, Quality, Usage)
        nested = {}
        for sub in _as_list(_arg(args, 2)):
            k, v = _read_quantity(model, session, sub, seen)
            nested[k] = v
        return name, nested
    # IfcQuantityLength/Area/Volume/Count/Weight/Time: (Name, Description, Unit, <value>[, Formula])
    return name, _arg(args, 3)


def property_definition(model, def_id):
    """Resolve one property definition: {"id", "kind": "pset"|"qto"|"dangling"|
    "other", "name", "properties"|"quantities"|"raw"}.

    Predefined psets (IfcDoorLiningProperties etc.) come back kind "other"
    with the raw line, so no data is lost.
    """
    session = model.session
    line = _safe_line(model, def_id)
    if line is None:
        return {"id": def_id, "kind": "dangling"}
    tname = session.type_name(line["type"]).upper()
    args = line["arguments"]
    name = _arg(args, 2)
    if tname == "IFCPROPERTYSET":
        props = {}
        for pid in _as_list(_arg(args, 4)):
            k, v = _read_property(model, session, pid, frozenset())
            props[k] = v
        return {"id": int(def_id), "kind": "pset", "name": name, "properties": props}
    if tname == "IFCELEMENTQUANTITY":
        # (GlobalId, OwnerHistory, Name, Description, MethodOfMeasurement, Quantities)
        quantities = {}
        for qid in _as_list(_arg(args, 5)):
            k, v = _read_quantity(model, session, qid, frozenset())
            quantities[k] = v
        return {"id": int(def_id), "kind": "qto", "name": name, "quantities": quantities}
    return {"id": int(def_id), "kind": "other", "name": name, "raw": line}


def psets_index(model):
    """element express ID -> [property-definition express IDs].

    Handles IFC4's IfcPropertySetDefinitionSet (a set-valued
    RelatingPropertyDefinition) by flattening it.
    """
    index = {}
    for _rel, related, definition in _scan(model, "IFCRELDEFINESBYPROPERTIES", 4, 5):
        defs = _as_list(definition)
        if not defs:
            continue
        for eid in _as_list(related):
            index.setdefault(eid, []).extend(defs)
    return index


def _definition_bundle(model, def_ids, memo=None):
    out = {"psets": {}, "qtos": {}, "other": []}
    for def_id in def_ids:
        if memo is not None and def_id in memo:
            d = memo[def_id]
        else:
            d = property_definition(model, def_id)
            if memo is not None:
                memo[def_id] = d
        if d["kind"] == "pset":
            out["psets"][d["name"]] = d["properties"]
        elif d["kind"] == "qto":
            out["qtos"][d["name"]] = d["quantities"]
        elif d["kind"] == "dangling":
            out["other"].append(d)
        else:
            out["other"].append(d)
    return out


def properties(model, eid, index=None):
    """All psets/qtos of one element: {"psets": {name: {...}}, "qtos":
    {name: {...}}, "other": [raw definitions]}."""
    if index is None:
        index = psets_index(model)
    return _definition_bundle(model, index.get(int(eid), []))


# ---- types & materials ------------------------------------------------------


def types_index(model):
    """element express ID -> type-object express ID (IfcRelDefinesByType)."""
    index = {}
    for _rel, related, type_id in _scan(model, "IFCRELDEFINESBYTYPE", 4, 5):
        tids = _as_list(type_id)
        if not tids:
            continue
        for eid in _as_list(related):
            index[eid] = tids[0]
    return index


def type_of(model, eid, index=None, memo=None):
    """Type object of an element: {"id", "type", "name", "psets", "qtos",
    "other"} or None. IfcTypeObject.HasPropertySets is argument 5 in every
    schema revision; predefined psets and quantity sets on the type survive
    under "qtos"/"other"."""
    session = model.session
    if index is None:
        index = types_index(model)
    tid = index.get(int(eid))
    if tid is None:
        return None
    line = _safe_line(model, tid)
    if line is None:
        return {"id": tid, "kind": "dangling"}
    args = line["arguments"]
    bundle = _definition_bundle(model, _as_list(_arg(args, 5)), memo=memo)
    return {
        "id": int(tid),
        "type": session.type_name(line["type"]),
        "name": _arg(args, 2),
        "psets": bundle["psets"],
        "qtos": bundle["qtos"],
        "other": bundle["other"],
    }


def _resolve_material(model, session, mid, seen=frozenset()):
    if mid is None:
        return None
    if mid in seen:
        return {"kind": "cycle", "id": int(mid)}
    seen = seen | {mid}
    line = _safe_line(model, mid)
    if line is None:
        return {"kind": "dangling", "id": mid}
    tname = session.type_name(line["type"]).upper()
    args = line["arguments"]
    if tname == "IFCMATERIAL":
        return {"kind": "material", "id": int(mid), "name": _arg(args, 0)}
    if tname == "IFCMATERIALLIST":
        return {
            "kind": "list",
            "id": int(mid),
            "materials": [_resolve_material(model, session, m, seen) for m in _as_list(_arg(args, 0))],
        }
    if tname == "IFCMATERIALLAYERSETUSAGE":
        return _resolve_material(model, session, _arg(args, 0), seen)
    if tname == "IFCMATERIALLAYERSET":
        return {
            "kind": "layer_set",
            "id": int(mid),
            "name": _arg(args, 1),
            "layers": [_resolve_material(model, session, l, seen) for l in _as_list(_arg(args, 0))],
        }
    if tname == "IFCMATERIALLAYER":
        material = _arg(args, 0)
        return {
            "kind": "layer",
            "id": int(mid),
            "material": _resolve_material(model, session, material, seen) if material else None,
            "thickness": _arg(args, 1),
        }
    if tname == "IFCMATERIALPROFILESETUSAGE":
        return _resolve_material(model, session, _arg(args, 0), seen)
    if tname == "IFCMATERIALPROFILESET":
        return {
            "kind": "profile_set",
            "id": int(mid),
            "name": _arg(args, 0),
            "profiles": [_resolve_material(model, session, p, seen) for p in _as_list(_arg(args, 2))],
        }
    if tname == "IFCMATERIALPROFILE":
        material = _arg(args, 2)
        return {
            "kind": "profile",
            "id": int(mid),
            "name": _arg(args, 0),
            "material": _resolve_material(model, session, material, seen) if material else None,
        }
    if tname == "IFCMATERIALCONSTITUENTSET":
        return {
            "kind": "constituent_set",
            "id": int(mid),
            "name": _arg(args, 0),
            "constituents": [_resolve_material(model, session, c, seen) for c in _as_list(_arg(args, 2))],
        }
    if tname == "IFCMATERIALCONSTITUENT":
        material = _arg(args, 2)
        return {
            "kind": "constituent",
            "id": int(mid),
            "name": _arg(args, 0),
            "material": _resolve_material(model, session, material, seen) if material else None,
        }
    return {"kind": "other", "id": int(mid), "raw": line}


def materials_index(model):
    """element/type express ID -> material definition express ID."""
    index = {}
    for _rel, related, mat in _scan(model, "IFCRELASSOCIATESMATERIAL", 4, 5):
        mids = _as_list(mat)
        if not mids:
            continue
        for eid in _as_list(related):
            index[eid] = mids[0]
    return index


def materials(model, eid, index=None):
    """Resolved material of an element (layer sets with thicknesses, lists,
    profiles, constituents), or None."""
    if index is None:
        index = materials_index(model)
    mid = index.get(int(eid))
    if mid is None:
        return None
    return _resolve_material(model, model.session, mid)


# ---- units ------------------------------------------------------------------

_SI_PREFIX_FACTORS = {
    "EXA": 1e18, "PETA": 1e15, "TERA": 1e12, "GIGA": 1e9, "MEGA": 1e6,
    "KILO": 1e3, "HECTO": 1e2, "DECA": 1e1, None: 1.0, "DECI": 1e-1,
    "CENTI": 1e-2, "MILLI": 1e-3, "MICRO": 1e-6, "NANO": 1e-9,
    "PICO": 1e-12, "FEMTO": 1e-15, "ATTO": 1e-18,
}


def units(model):
    """Project units: {unit_type: {"name", "prefix", "factor"|...}}.

    Resolves IfcProject.UnitsInContext (argument 8 in every schema revision):
    SI units carry an exact factor; conversion-based units carry the
    conversion value; anything else keeps the raw line.
    """
    session = model.session
    projects = model.ids_of_type("IFCPROJECT")
    if not len(projects):
        return {}
    args = _args_of(model, int(projects[0]))
    assignment = _arg(args, 8)
    if not assignment:
        return {}
    ua_args = _args_of(model, assignment)
    out = {}
    for uid in _as_list(_arg(ua_args, 0)):
        line = _safe_line(model, uid)
        if line is None:
            continue
        tname = session.type_name(line["type"]).upper()
        uargs = line["arguments"]
        if tname == "IFCSIUNIT":
            # (Dimensions, UnitType, Prefix, Name)
            prefix = _arg(uargs, 2)
            out[_arg(uargs, 1)] = {
                "name": _arg(uargs, 3),
                "prefix": prefix,
                "factor": _SI_PREFIX_FACTORS.get(prefix, 1.0),
            }
        elif tname == "IFCCONVERSIONBASEDUNIT":
            # (Dimensions, UnitType, Name, ConversionFactor -> IfcMeasureWithUnit)
            factor = None
            mwu = _args_of(model, _arg(uargs, 3)) if _arg(uargs, 3) else None
            if mwu:
                factor = _arg(mwu, 0)
            out[_arg(uargs, 1)] = {"name": _arg(uargs, 2), "prefix": None, "factor": factor}
        elif tname == "IFCMONETARYUNIT":
            out["MONETARYUNIT"] = {"name": _arg(uargs, 0), "prefix": None, "factor": None}
        else:
            out.setdefault("derived", []).append({"id": uid, "raw": line})
    return out


# ---- hierarchy --------------------------------------------------------------


def decomposition(model):
    """Every IfcRelAggregates/IfcRelNests edge: {parent: [children]}.

    Includes both spatial decomposition (project/site/building/storey) and
    element decomposition (stairs into flights, roofs into slabs, ports
    nested in elements)."""
    children = {}
    for tname in ("IFCRELAGGREGATES", "IFCRELNESTS"):
        for _rel, relating, related in _scan(model, tname, 4, 5):
            parents = _as_list(relating)
            if not parents:
                continue
            children.setdefault(parents[0], []).extend(_as_list(related))
    return children


def spatial_tree(model):
    """The project decomposition tree with contained elements.

    Node: {"express_id", "type", "name", "guid", "children": [nodes],
    "elements": [express IDs]}. Children come from IfcRelAggregates /
    IfcRelNests, elements from IfcRelContainedInSpatialStructure.
    Element-level decomposition (stair flights etc.) is exposed separately
    via decomposition() and inherited containers in extract()."""
    session = model.session
    children = decomposition(model)
    contained = {}
    for _rel, elements, structure in _scan(model, "IFCRELCONTAINEDINSPATIALSTRUCTURE", 4, 5):
        structures = _as_list(structure)
        if not structures:
            continue
        contained.setdefault(structures[0], []).extend(_as_list(elements))

    def build(eid, seen):
        if eid in seen:  # defensive: malformed models can create cycles
            return None
        seen = seen | {eid}
        line = _safe_line(model, eid)
        if line is None:
            return None
        args = line["arguments"]
        node = {
            "express_id": int(eid),
            "type": session.type_name(line["type"]),
            "name": _arg(args, 2),
            "guid": _arg(args, 0),
            "children": [],
            "elements": sorted(contained.get(int(eid), [])),
        }
        for child in sorted(children.get(int(eid), [])):
            built = build(child, seen)
            if built is not None:
                node["children"].append(built)
        return node

    roots = [int(i) for i in model.ids_of_type("IFCPROJECT")]
    return [n for n in (build(r, frozenset()) for r in roots) if n is not None]


def container_of(model, eid=None):
    """element express ID -> containing spatial structure express ID (dict for
    the whole model, or a single ID when eid is given). Direct containment
    only; extract() additionally inherits containers through decomposition."""
    index = {}
    for _rel, elements, structure in _scan(model, "IFCRELCONTAINEDINSPATIALSTRUCTURE", 4, 5):
        structures = _as_list(structure)
        if not structures:
            continue
        for e in _as_list(elements):
            index[e] = structures[0]
    if eid is not None:
        return index.get(int(eid))
    return index


# ---- classifications & documents --------------------------------------------


def classifications(model):
    """element express ID -> [classification references].

    Each reference: {"id", "identification", "name", "location"} for
    IfcClassificationReference (IFC2X3 stores identification at index 1 as
    ItemReference; the layout is the same), raw line otherwise."""
    session = model.session
    index = {}
    for _rel, related, ref in _scan(model, "IFCRELASSOCIATESCLASSIFICATION", 4, 5):
        refs = _as_list(ref)
        if not refs:
            continue
        line = _safe_line(model, refs[0])
        if line is None:
            entry = {"id": refs[0], "kind": "dangling"}
        elif session.type_name(line["type"]).upper() == "IFCCLASSIFICATIONREFERENCE":
            args = line["arguments"]
            entry = {
                "id": refs[0],
                "location": _arg(args, 0),
                "identification": _arg(args, 1),
                "name": _arg(args, 2),
            }
        else:
            entry = {"id": refs[0], "raw": line}
        for eid in _as_list(related):
            index.setdefault(eid, []).append(entry)
    return index


# ---- connections & systems --------------------------------------------------


def connections(model):
    """Every element-to-element relationship instance in the model.

    Records: {"type", "id", "relating", "related"}; space boundaries carry
    "physical_or_virtual" / "internal_or_external" flags; IFC4 port
    nestings (IfcRelNests whose related objects are IfcDistributionPort)
    are emitted as IfcRelNests records."""
    session = model.session
    out = []
    for tname, a, b in _CONNECTION_SPECS:
        code = None
        try:
            code = session.type_code(tname)
        except Exception:
            pass
        for rel, relating, related in _scan(model, tname, a, b):
            out.append({
                "type": session.type_name(code) if code else tname,
                "id": int(rel),
                "relating": relating,
                "related": related,
            })
    for tname in _SPACE_BOUNDARY_TYPES:
        for rel, space, element in _scan(model, tname, 4, 5):
            line = _safe_line(model, rel)
            args = line["arguments"] if line else None
            out.append({
                "type": session.type_name(line["type"]) if line else tname,
                "id": int(rel),
                "relating": space,
                "related": element,
                "physical_or_virtual": _arg(args, 7),
                "internal_or_external": _arg(args, 8),
            })
    # IFC4 maps ports to their element via IfcRelNests
    try:
        port_code = session.type_code("IFCDISTRIBUTIONPORT")
    except Exception:
        port_code = 0
    if port_code:
        for rel, relating, related in _scan(model, "IFCRELNESTS", 4, 5):
            ports = [r for r in _as_list(related) if model.is_valid_id(r) and model.line_type(r) == port_code]
            if ports:
                out.append({
                    "type": "IfcRelNests",
                    "id": int(rel),
                    "relating": relating,
                    "related": ports,
                })
    return out


def systems(model):
    """Group memberships: group express ID -> {"name", "type", "members"}.

    Covers IfcRelAssignsToGroup and IfcRelAssignsToGroupByFactor (systems,
    zones, distribution systems)."""
    session = model.session
    out = {}
    # (GlobalId, OwnerHistory, Name, Description, RelatedObjects, RelatedObjectsType, RelatingGroup)
    for tname in _GROUP_ASSIGNMENT_TYPES:
        for _rel, related, group in _scan(model, tname, 4, 6):
            groups = _as_list(group)
            if not groups:
                continue
            gid = groups[0]
            if gid not in out:
                line = _safe_line(model, gid)
                out[gid] = {
                    "name": _arg(line["arguments"], 2) if line else None,
                    "type": session.type_name(line["type"]) if line else None,
                    "members": [],
                }
            out[gid]["members"].extend(_as_list(related))
    return out


# ---- catch-all --------------------------------------------------------------

# Every IfcRel* type a semantic helper consumes; relationships() and
# extract()["other_relationships"] cover the rest, so nothing in the
# relationship layer can be silently missed.
HANDLED_REL_TYPES = frozenset(
    [t for t, _a, _b in _CONNECTION_SPECS]
    + _SPACE_BOUNDARY_TYPES
    + _GROUP_ASSIGNMENT_TYPES
    + [
        "IFCRELDEFINESBYPROPERTIES",
        "IFCRELDEFINESBYTYPE",
        "IFCRELASSOCIATESMATERIAL",
        "IFCRELASSOCIATESCLASSIFICATION",
        "IFCRELAGGREGATES",
        "IFCRELNESTS",
        "IFCRELCONTAINEDINSPATIALSTRUCTURE",
    ]
)


def present_rel_types(model):
    """Names of every IfcRel* type instantiated in this model."""
    session = model.session
    out = []
    for code in model.present_types():
        name = session.type_name(code)
        if name.upper().startswith("IFCREL"):
            out.append(name)
    return sorted(out)


def relationships(model, types=None, exclude_handled=False):
    """Raw dump of relationship instances: {type_name: [get_line dicts]}.

    The guaranteed-complete fallback for IfcRel* types without a semantic
    helper (structural connects, document associations, IfcRelDeclares,
    IFC4X3 positions...). types narrows to specific names; exclude_handled
    drops everything a semantic helper already covers.
    """
    wanted = None if types is None else {t.upper() for t in types}
    out = {}
    for name in present_rel_types(model):
        upper = name.upper()
        if wanted is not None and upper not in wanted:
            continue
        if exclude_handled and upper in HANDLED_REL_TYPES:
            continue
        lines = []
        for eid in model.ids_of_type(upper):
            line = _safe_line(model, int(eid))
            if line is not None:
                lines.append(line)
        out[name] = lines
    return out


# ---- one-shot dump ----------------------------------------------------------


def extract(model, include_type_psets=True, include_materials=True):
    """Everything at once: the shape a conversion pipeline consumes.

    {"schema", "units", "spatial_tree", "decomposition", "elements":
    {eid: {"type", "guid", "name", "container", "psets", "qtos",
    "type_object", "materials", "classifications"}}, "connections",
    "systems"}.

    Container inheritance: parts of a decomposed element (stair flights,
    roof slabs) inherit the composite's spatial container, per IFC
    semantics. Elements whose material is associated to their TYPE inherit
    it unless they have their own association. Shared definitions are
    resolved once (memoized)."""
    session = model.session
    pindex = psets_index(model)
    tindex = types_index(model)
    mindex = materials_index(model) if include_materials else {}
    cindex = container_of(model)
    cls_index = classifications(model)
    decomp = decomposition(model)
    tree = spatial_tree(model)

    parent_of = {}
    for parent, parts in decomp.items():
        for p in parts:
            parent_of[p] = parent

    spatial_ids = set()

    def walk(node):
        spatial_ids.add(node["express_id"])
        for c in node["children"]:
            walk(c)

    for root in tree:
        walk(root)

    def inherited_container(eid):
        seen = set()
        cur = eid
        while cur is not None and cur not in seen:
            seen.add(cur)
            if cur in cindex:
                return cindex[cur]
            cur = parent_of.get(cur)
        return None

    element_ids = set()
    for code in session.element_types():
        for eid in model.ids_of_type(code):
            element_ids.add(int(eid))
    element_ids.update(spatial_ids)

    def_memo = {}
    type_memo = {}
    mat_memo = {}

    elements = {}
    for eid in sorted(element_ids):
        line = _safe_line(model, eid)
        if line is None:
            continue
        args = line["arguments"]
        entry = {
            "type": session.type_name(line["type"]),
            "guid": _arg(args, 0),
            "name": _arg(args, 2),
            "container": inherited_container(eid),
        }
        if eid in parent_of:
            entry["part_of"] = parent_of[eid]
        bundle = _definition_bundle(model, pindex.get(eid, []), memo=def_memo)
        entry["psets"] = bundle["psets"]
        entry["qtos"] = bundle["qtos"]
        if bundle["other"]:
            entry["other_definitions"] = bundle["other"]
        if include_type_psets:
            tid = tindex.get(eid)
            if tid is not None and tid in type_memo:
                entry["type_object"] = type_memo[tid]
            else:
                entry["type_object"] = type_of(model, eid, index=tindex, memo=def_memo)
                if tid is not None:
                    type_memo[tid] = entry["type_object"]
        if include_materials:
            # occurrence association wins; otherwise inherit from the type
            mid = mindex.get(eid)
            if mid is None:
                mid = mindex.get(tindex.get(eid))
            if mid is None:
                entry["materials"] = None
            elif mid in mat_memo:
                entry["materials"] = mat_memo[mid]
            else:
                entry["materials"] = _resolve_material(model, session, mid)
                mat_memo[mid] = entry["materials"]
        if eid in cls_index:
            entry["classifications"] = cls_index[eid]
        elements[eid] = entry

    return {
        "schema": model.schema,
        "units": units(model),
        "spatial_tree": tree,
        "decomposition": {k: sorted(v) for k, v in decomp.items()},
        "elements": elements,
        "connections": connections(model),
        "systems": systems(model),
        # raw instances of any relationship type no helper above consumed
        "other_relationships": relationships(model, exclude_handled=True),
    }
