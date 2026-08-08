"""BIM data extraction: hierarchy, psets/quantities, materials, types,
connections, systems. Counts and values are pinned against the fixtures
(verified independently by grepping the IFC source)."""

import webifc
from webifc import bim
from conftest import fixture_path


def test_spatial_tree_structure(fzk_model):
    tree = bim.spatial_tree(fzk_model)
    assert len(tree) == 1
    project = tree[0]
    assert project["type"] == "IfcProject" and project["name"] == "Projekt-FZK-Haus"
    site = project["children"][0]
    building = site["children"][0]
    assert site["type"] == "IfcSite" and building["type"] == "IfcBuilding"
    storeys = building["children"]
    assert [s["name"] for s in storeys] == ["Erdgeschoss", "Dachgeschoss"]
    assert [len(s["elements"]) for s in storeys] == [38, 58]
    # the 7 spaces hang off the storeys via IfcRelAggregates
    spaces = [c for s in storeys for c in s["children"] if c["type"] == "IfcSpace"]
    assert len(spaces) == 7


def test_container_of_matches_tree(fzk_model):
    index = bim.container_of(fzk_model)
    tree = bim.spatial_tree(fzk_model)
    storeys = tree[0]["children"][0]["children"][0]["children"]
    for storey in storeys:
        for eid in storey["elements"]:
            assert index[eid] == storey["express_id"]


def test_wall_property_sets(fzk_model):
    wall = int(fzk_model.ids_of_type("IFCWALLSTANDARDCASE")[0])
    props = bim.properties(fzk_model, wall)
    assert props["psets"]["Pset_WallCommon"]["ThermalTransmittance"] == 1.5
    base = props["qtos"]["BaseQuantities"]
    assert base["Width"] == 0.24
    assert base["Height"] == 2.5


def test_space_quantities(fzk_model):
    for sid in fzk_model.ids_of_type("IFCSPACE"):
        qtos = bim.properties(fzk_model, int(sid))["qtos"]
        assert "BaseQuantities" in qtos
        assert qtos["BaseQuantities"]["Height"] == 2.5
        break


def test_materials_layer_set(fzk_model):
    wall = int(fzk_model.ids_of_type("IFCWALLSTANDARDCASE")[0])
    mat = bim.materials(fzk_model, wall)
    assert mat["kind"] == "layer_set"
    assert mat["layers"][0]["thickness"] == 0.24
    assert mat["layers"][0]["material"]["name"].startswith("Leichtbeton")


def test_type_objects(fzk_model):
    index = bim.types_index(fzk_model)
    assert len(index) > 0
    wall = int(fzk_model.ids_of_type("IFCWALLSTANDARDCASE")[0])
    t = bim.type_of(fzk_model, wall, index=index)
    assert t is not None and t["type"] == "IfcWallType"


def test_connections_counts(fzk_model):
    conns = bim.connections(fzk_model)
    by_type = {}
    for c in conns:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    # pinned against grep of the fixture
    assert by_type["IfcRelVoidsElement"] == 17
    assert by_type["IfcRelFillsElement"] == 16
    assert by_type["IfcRelConnectsPathElements"] == 16
    assert by_type["IfcRelSpaceBoundary"] == 81


def test_space_boundaries_have_flags(fzk_model):
    boundaries = [c for c in bim.connections(fzk_model) if c["type"].startswith("IfcRelSpaceBoundary")]
    assert boundaries
    flagged = [b for b in boundaries if b["physical_or_virtual"] is not None]
    assert flagged, "boundary level flags missing"
    assert all(b["physical_or_virtual"] in ("PHYSICAL", "VIRTUAL", "NOTDEFINED") for b in flagged)


def test_fills_pair_with_voids(fzk_model):
    """Every filled opening must be a voided opening of some host."""
    voids = {c["related"] for c in bim.connections(fzk_model) if c["type"] == "IfcRelVoidsElement"}
    fills = [c for c in bim.connections(fzk_model) if c["type"] == "IfcRelFillsElement"]
    assert fills
    for f in fills:
        assert f["relating"] in voids


def test_duplex_scale():
    with webifc.open(fixture_path("duplex.ifc")) as m:
        index = bim.psets_index(m)
        assert len(index) > 100
        tree = bim.spatial_tree(m)
        storeys = [c for c in tree[0]["children"][0]["children"][0]["children"] if c["type"] == "IfcBuildingStorey"]
        assert len(storeys) == 4
        boundaries = [c for c in bim.connections(m) if c["type"].startswith("IfcRelSpaceBoundary")]
        assert len(boundaries) == 265


def test_extract_end_to_end(fzk_model):
    data = bim.extract(fzk_model)
    assert data["schema"] == "IFC4"
    assert data["spatial_tree"][0]["name"] == "Projekt-FZK-Haus"
    wall = int(fzk_model.ids_of_type("IFCWALLSTANDARDCASE")[0])
    entry = data["elements"][wall]
    assert entry["type"] == "IfcWallStandardCase"
    assert entry["psets"]["Pset_WallCommon"]["ThermalTransmittance"] == 1.5
    assert entry["materials"]["kind"] == "layer_set"
    assert entry["container"] is not None
    # spaces are elements too, with their quantities
    space = int(fzk_model.ids_of_type("IFCSPACE")[0])
    assert data["elements"][space]["qtos"]["BaseQuantities"]["Height"] == 2.5
    assert len(data["connections"]) == 17 + 16 + 16 + 81


# ---- review-driven coverage -------------------------------------------------

HEADER_IFC4 = (
    "ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION((''),'2;1');\n"
    "FILE_NAME('','',(''),(''),'','','');\nFILE_SCHEMA(('IFC4'));\nENDSEC;\nDATA;\n"
)


def _write_model(tmp_path, body, name="synthetic.ifc"):
    p = tmp_path / name
    p.write_text(HEADER_IFC4 + body + "ENDSEC;\nEND-ISO-10303-21;\n")
    return p


def test_ifc4_property_set_definition_set(tmp_path):
    """IFC4 allows a SET-valued RelatingPropertyDefinition — both wire forms."""
    body = (
        "#1=IFCPROJECT('3MD_HkJ6X2EwpfIbCFm0g_',$,'P',$,$,$,$,$,$);\n"
        "#2=IFCWALL('3MD_HkJ6X2EwpfIbCFm0g1',$,'W',$,$,$,$,$,$);\n"
        "#3=IFCPROPERTYSINGLEVALUE('Fire',$,IFCLABEL('F30'),$);\n"
        "#4=IFCPROPERTYSET('3MD_HkJ6X2EwpfIbCFm0g2',$,'Pset_A',$,(#3));\n"
        "#5=IFCPROPERTYSET('3MD_HkJ6X2EwpfIbCFm0g3',$,'Pset_B',$,(#3));\n"
        "#6=IFCRELDEFINESBYPROPERTIES('3MD_HkJ6X2EwpfIbCFm0g4',$,$,$,(#2),(#4,#5));\n"
        "#7=IFCWALL('3MD_HkJ6X2EwpfIbCFm0g5',$,'W2',$,$,$,$,$,$);\n"
        "#8=IFCRELDEFINESBYPROPERTIES('3MD_HkJ6X2EwpfIbCFm0g6',$,$,$,(#7),IFCPROPERTYSETDEFINITIONSET((#4)));\n"
    )
    with webifc.open(_write_model(tmp_path, body)) as m:
        props = bim.properties(m, 2)
        assert set(props["psets"]) == {"Pset_A", "Pset_B"}
        assert props["psets"]["Pset_A"]["Fire"] == "F30"
        props2 = bim.properties(m, 7)
        assert props2["psets"]["Pset_A"]["Fire"] == "F30"
        data = bim.extract(m)  # must not raise
        assert data["elements"][2]["psets"]["Pset_B"]["Fire"] == "F30"


def test_dangling_refs_do_not_abort(tmp_path):
    body = (
        "#1=IFCPROJECT('3MD_HkJ6X2EwpfIbCFm0g_',$,'P',$,$,$,$,$,$);\n"
        "#2=IFCWALL('3MD_HkJ6X2EwpfIbCFm0g1',$,'W',$,$,$,$,$,$);\n"
        "#6=IFCRELDEFINESBYPROPERTIES('3MD_HkJ6X2EwpfIbCFm0g4',$,$,$,(#2),#999);\n"
        "#7=IFCRELAGGREGATES('3MD_HkJ6X2EwpfIbCFm0g5',$,$,$,#1,(#888));\n"
    )
    with webifc.open(_write_model(tmp_path, body)) as m:
        props = bim.properties(m, 2)
        assert props["other"] and props["other"][0]["kind"] == "dangling"
        data = bim.extract(m)  # dangling pset + dangling aggregate child
        assert 2 in data["elements"]


def test_unknown_type_name_raises(fzk_model):
    import pytest

    with pytest.raises(webifc.WebIfcError):
        fzk_model.scan_relationship("IFCRELTOTALLYMADEUP", 4, 5)


def test_decomposition_and_container_inheritance():
    """duplex: stair flights are parts of IfcStair and inherit its storey."""
    with webifc.open(fixture_path("duplex.ifc")) as m:
        data = bim.extract(m)
        stairs = [int(i) for i in m.ids_of_type("IFCSTAIR")]
        assert stairs
        decomposed = [s for s in stairs if s in data["decomposition"]]
        assert decomposed, "duplex stairs are decomposed"
        stair = decomposed[0]
        for part in data["decomposition"][stair]:
            assert data["elements"][part]["part_of"] == stair
            assert data["elements"][part]["container"] == data["elements"][stair]["container"]
            assert data["elements"][part]["container"] is not None


def test_type_object_keeps_non_pset_definitions(fzk_model):
    """FZK door types carry predefined lining/panel property definitions."""
    doors = [int(i) for i in fzk_model.ids_of_type("IFCDOOR")]
    assert doors
    found_other = False
    tindex = bim.types_index(fzk_model)
    for d in doors:
        t = bim.type_of(fzk_model, d, index=tindex)
        if t and (t["other"] or t["qtos"]):
            found_other = True
            break
    assert found_other, "non-pset type definitions were dropped"


def test_units(fzk_model):
    u = bim.units(fzk_model)
    assert u["LENGTHUNIT"]["name"] == "METRE"
    assert u["LENGTHUNIT"]["factor"] == 1.0


def test_boundary_internal_external_flags(fzk_model):
    boundaries = [c for c in bim.connections(fzk_model) if c["type"].startswith("IfcRelSpaceBoundary")]
    vals = {b["internal_or_external"] for b in boundaries}
    assert vals & {"INTERNAL", "EXTERNAL", "EXTERNAL_EARTH", "NOTDEFINED"}


def test_ifc4x3_smoke():
    """Structure-level smoke on IFC4X3: tree, decomposition, psets of a few
    elements. (Full extract() on ~500k-entity infrastructure models is a
    batch operation, not a test.)"""
    with webifc.open(fixture_path("Viadotto Acerno.ifc")) as m:
        assert m.schema.startswith("IFC4X3")
        tree = bim.spatial_tree(m)
        assert tree and tree[0]["type"] == "IfcProject"
        assert bim.decomposition(m)
        index = bim.psets_index(m)
        assert index
        some = next(iter(index))
        assert bim.properties(m, some, index=index) is not None


def test_relationship_catch_all_is_complete(fzk_model):
    """Every IfcRel type present in the model is either semantically handled
    or dumped by relationships(exclude_handled=True) — nothing can vanish."""
    present = {t.upper() for t in bim.present_rel_types(fzk_model)}
    other = {t.upper() for t in bim.relationships(fzk_model, exclude_handled=True)}
    assert present == (present & bim.HANDLED_REL_TYPES) | other
    # FZK is fully covered by the semantic helpers
    assert other == set()
    # and the raw dump agrees with the pinned instance counts
    dump = bim.relationships(fzk_model, types=["IFCRELVOIDSELEMENT"])
    assert len(dump["IfcRelVoidsElement"]) == 17


def test_unhandled_relationship_survives_in_extract(tmp_path):
    """A rel type with no semantic helper lands in other_relationships."""
    body = (
        "#1=IFCPROJECT('3MD_HkJ6X2EwpfIbCFm0g_',$,'P',$,$,$,$,$,$);\n"
        "#2=IFCWALL('3MD_HkJ6X2EwpfIbCFm0g1',$,'W',$,$,$,$,$,$);\n"
        "#3=IFCBUILDING('3MD_HkJ6X2EwpfIbCFm0g2',$,'B',$,$,$,$,$,$,$,$,$);\n"
        "#4=IFCRELASSIGNSTOPRODUCT('3MD_HkJ6X2EwpfIbCFm0g3',$,$,$,(#2),$,#3);\n"
    )
    with webifc.open(_write_model(tmp_path, body)) as m:
        data = bim.extract(m)
        other = data["other_relationships"]
        assert "IfcRelAssignsToProduct" in other
        assert other["IfcRelAssignsToProduct"][0]["ID"] == 4


def test_systems_and_ports_advanced_model():
    """MEP model: group assignments and port connections (pinned via grep:
    240 IfcRelAssignsToGroup, 3640 port-port, 7638 port-element)."""
    with webifc.open(fixture_path("advanced_model.ifc")) as m:
        sys_map = bim.systems(m)
        assert sys_map
        assert sum(len(s["members"]) for s in sys_map.values()) >= 240
        conns = bim.connections(m)
        by_type = {}
        for c in conns:
            by_type[c["type"]] = by_type.get(c["type"], 0) + 1
        assert by_type["IfcRelConnectsPorts"] == 3640
        assert by_type["IfcRelConnectsPortToElement"] == 7638
        assert by_type["IfcRelServicesBuildings"] == 240
