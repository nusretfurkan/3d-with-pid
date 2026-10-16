"""STEP assembly export and reimport through OCCT's XCAF document model.

The assembly tree is ``root -> group -> part``. The root is named after the
project (a project without placed geometry writes the root product alone, with
no components and no shape content); each group is an entity (instanced at
its placement) or a connection (identity location); each part is one
component shape, named by its stable
component ID ``<semantic id>#<component key>``. Product and instance names
carry the same IDs, part colours come from the appearance palette, lengths are
written in millimetres, and the header description records the source
snapshot identity. Reimport returns located world shapes for verification.

Files are not byte-reproducible: OCCT numbers assembly-usage IDs from a
process-wide counter, orders colour styles by memory address and stamps the
write time. Equality is therefore checked on the reimported structure.
"""

from __future__ import annotations

import re

from OCP.APIHeaderSection import APIHeaderSection_MakeHeader
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.Interface import Interface_Static
from OCP.Quantity import Quantity_Color, Quantity_TypeOfColor
from OCP.STEPCAFControl import STEPCAFControl_Reader, STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_StepModelType
from OCP.TCollection import TCollection_ExtendedString, TCollection_HAsciiString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool, XCAFDoc_ShapeTool

from . import kernel

__all__ = ["StepError", "read_step", "write_step"]

class StepError(RuntimeError):
    """STEP writing or reading failed."""


def _new_document():
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)
    return doc


def _set_name(label, name):
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))


def _name(label) -> str | None:
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        return str(attr.Get().ToExtString())
    return None


def write_step(path, header: dict, root_name: str, groups: list) -> dict:
    """Write ``groups`` as a named STEP assembly in millimetres.

    ``groups`` items: ``{name, matrix: 3x4 mm placement or None, parts: [{name, shape, rgb}]}``.
    ``header``: ``{name, description, author, organization, originating_system}``.
    Returns a summary of what was written.
    """
    doc = _new_document()
    shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    colors = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    XCAFDoc_ShapeTool.SetAutoNaming_s(False)
    root = shapes.NewShape()
    _set_name(root, root_name)
    part_count = 0
    for group in groups:
        group_label = shapes.NewShape()
        _set_name(group_label, group["name"])
        for part in group["parts"]:
            label = shapes.AddShape(part["shape"], False, False)
            _set_name(label, part["name"])
            r, g, b = part["rgb"]
            colors.SetColor(label, Quantity_Color(r, g, b, Quantity_TypeOfColor.Quantity_TOC_sRGB),
                            XCAFDoc_ColorType.XCAFDoc_ColorSurf)
            instance = shapes.AddComponent(group_label, label, TopLoc_Location())
            _set_name(instance, part["name"])
            part_count += 1
        matrix = group.get("matrix")
        location = TopLoc_Location() if matrix is None else TopLoc_Location(kernel.trsf(matrix))
        instance = shapes.AddComponent(root, group_label, location)
        _set_name(instance, group["name"])
    shapes.UpdateAssemblies()

    Interface_Static.SetCVal_s("write.step.unit", "MM")
    Interface_Static.SetCVal_s("write.step.schema", "AP214IS")
    Interface_Static.SetIVal_s("write.surfacecurve.mode", 1)
    Interface_Static.SetIVal_s("write.precision.mode", 0)
    Interface_Static.SetIVal_s("write.stepcaf.subshapes.name", 1)
    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    writer.SetColorMode(True)
    writer.SetLayerMode(False)
    if not writer.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs):
        raise StepError("STEP transfer failed")
    made = APIHeaderSection_MakeHeader(writer.ChangeWriter().Model())
    made.SetName(TCollection_HAsciiString(header["name"]))
    made.SetDescriptionValue(1, TCollection_HAsciiString(header["description"]))
    made.SetAuthorValue(1, TCollection_HAsciiString(header["author"]))
    made.SetOrganizationValue(1, TCollection_HAsciiString(header["organization"]))
    made.SetOriginatingSystem(TCollection_HAsciiString(header["originating_system"]))
    made.SetPreprocessorVersion(TCollection_HAsciiString(header["originating_system"]))
    made.SetAuthorisation(TCollection_HAsciiString(""))
    status = writer.Write(str(path))
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise StepError(f"STEP write returned {status}")
    return {"groups": len(groups), "parts": part_count}


_UNIT_PATTERN = re.compile(r"SI_UNIT\s*\(\s*\.MILLI\.\s*,\s*\.METRE\.\s*\)")


def header_facts(text: str) -> dict:
    """Extract unit and identity facts from STEP text."""
    description = re.search(r"FILE_DESCRIPTION\s*\(\s*\(\s*'([^']*)'", text)
    schema = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']*)'", text)

    def literal(match):
        # Part 21 writers wrap long strings; physical line breaks are not part of the value.
        return re.sub(r"\r?\n", "", match.group(1)) if match else None

    return {
        "length_unit_millimetre": bool(_UNIT_PATTERN.search(text)),
        "length_unit_metre_plain": bool(re.search(r"SI_UNIT\s*\(\s*\$\s*,\s*\.METRE\.\s*\)", text)),
        "description": literal(description),
        "schema": literal(schema),
    }


def read_step(path) -> dict:
    """Read a STEP assembly back as ``{root, groups: [{name, location, parts: [{name, product, shape}]}], empty}``.

    ``location`` is the group's 3x4 millimetre matrix; each part ``shape`` is
    located in world millimetres (group location composed with the part's).
    ``empty`` is True only for the one non-assembly root accepted: a named
    product with no components whose shape holds no vertex at all, which is
    how an empty project's STEP (written with no groups) reimports.
    """
    Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")
    Interface_Static.SetIVal_s("read.stepcaf.subshapes.name", 1)
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    reader.SetLayerMode(False)
    status = reader.ReadFile(str(path))
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise StepError(f"STEP read returned {status}")
    doc = _new_document()
    if not reader.Transfer(doc):
        raise StepError("STEP transfer into an XCAF document failed")
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    free = TDF_LabelSequence()
    tool.GetFreeShapes(free)
    if free.Length() != 1:
        raise StepError(f"expected one top-level assembly, found {free.Length()}")
    root = free.Value(1)
    if tool.IsReference_s(root):
        referred = TDF_Label()
        tool.GetReferredShape_s(root, referred)
        root = referred
    if not XCAFDoc_ShapeTool.IsAssembly_s(root):
        # The only accepted non-assembly root: an empty project's product with no components and no geometry
        # at all (OCCT writes it as an empty compound). Anything else is still rejected.
        components = TDF_LabelSequence()
        XCAFDoc_ShapeTool.GetComponents_s(root, components, False)
        shape = XCAFDoc_ShapeTool.GetShape_s(root)
        if components.Length() == 0 and (shape.IsNull() or not TopExp_Explorer(shape, TopAbs_VERTEX).More()):
            return {"root": _name(root), "groups": [], "loose_shapes": 0, "empty": True}
        raise StepError("top-level shape is not an assembly")
    result = {"root": _name(root), "groups": [], "loose_shapes": 0, "empty": False}
    groups = TDF_LabelSequence()
    XCAFDoc_ShapeTool.GetComponents_s(root, groups, False)
    for gi in range(1, groups.Length() + 1):
        instance = groups.Value(gi)
        location = XCAFDoc_ShapeTool.GetLocation_s(instance)
        group_label = TDF_Label()
        XCAFDoc_ShapeTool.GetReferredShape_s(instance, group_label)
        group = {"name": _name(instance), "product": _name(group_label), "location": _matrix(location), "parts": []}
        parts = TDF_LabelSequence()
        XCAFDoc_ShapeTool.GetComponents_s(group_label, parts, False)
        if not XCAFDoc_ShapeTool.IsAssembly_s(group_label):
            result["loose_shapes"] += 1
        for pi in range(1, parts.Length() + 1):
            part_instance = parts.Value(pi)
            part_location = XCAFDoc_ShapeTool.GetLocation_s(part_instance)
            part_label = TDF_Label()
            XCAFDoc_ShapeTool.GetReferredShape_s(part_instance, part_label)
            shape = XCAFDoc_ShapeTool.GetShape_s(part_label)
            world = shape.Moved(location.Multiplied(part_location))
            group["parts"].append({"name": _name(part_instance), "product": _name(part_label),
                                   "location": _matrix(part_location), "shape": world})
        result["groups"].append(group)
    return result


def _matrix(location):
    t = location.Transformation()
    return tuple(tuple(t.Value(i, j) for j in range(1, 5)) for i in range(1, 4))
