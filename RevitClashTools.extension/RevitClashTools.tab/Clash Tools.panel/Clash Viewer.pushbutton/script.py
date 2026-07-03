# -*- coding: utf-8 -*-
"""Import a Navisworks Clash Detective XML report, let the user say which
Revit link (or the host model) each clash side came from, and color /
isolate the matched elements in the active view.

Matching strategy
------------------
Which per-item properties Navisworks writes into the report depends on
checkboxes in its "Write Report" dialog, so this tries several clues in
order of reliability rather than depending on any single one:

  1. A GUID-like property (Revit's Element.UniqueId, or an IFC GUID) -
     resolved via `document.GetElement(guid)` directly, or against a
     per-document IFC_GUID parameter cache built on first use.
  2. A bare numeric element-id property (e.g. "Element ID"/"Item ID") -
     resolved via `document.GetElement(ElementId(int(value)))`.
  3. A trailing "[12345]"/"(12345)" in the item's own display name -
     Revit's Navisworks exporter bakes the ElementId into item names by
     default, so this survives even a report with no item properties
     exported at all.

Use "Resolve GUIDs" to see the match rate, and "Inspect Raw XML" on a
selected row to see exactly what that clash contains if matching fails -
that raw XML is the fastest way to extend the parser for a report style
it doesn't already handle.

Known limitation
-----------------
Per-element color overrides are fully supported for host-document
elements. For linked-model elements, the Revit API only supports view
overrides at the *link instance* granularity (the whole link gets one
color). To still highlight only the clashing elements within a link,
this tool uses RevitLinkInstance.SetVisibleElements to hide everything
in that link except the matched clash elements, then applies the link's
color - so visually you get per-clash highlighting, just not mixed
colors within the same link at the same time.
"""

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("System.Windows.Forms")

from System.Windows import Window
from System.Collections.ObjectModel import ObservableCollection
from System.Collections.Generic import List

from pyrevit import revit, DB, forms, script

import clash_parser

doc = revit.doc
uidoc = revit.uidoc
view = doc.ActiveView
output = script.get_output()
logger = script.get_logger()

COLOR_PRESETS = {
    "Red": DB.Color(255, 0, 0),
    "Green": DB.Color(0, 200, 0),
    "Blue": DB.Color(0, 100, 255),
    "Orange": DB.Color(255, 140, 0),
    "Magenta": DB.Color(230, 0, 230),
    "Cyan": DB.Color(0, 200, 200),
    "Yellow": DB.Color(230, 210, 0),
    "Purple": DB.Color(130, 0, 200),
}


def get_solid_fill_pattern_id(document):
    fp = (
        DB.FilteredElementCollector(document)
        .OfClass(DB.FillPatternElement)
        .ToElements()
    )
    for f in fp:
        if f.GetFillPattern().IsSolidFill:
            return f.Id
    return DB.ElementId.InvalidElementId


def build_ogs(document, color):
    ogs = DB.OverrideGraphicSettings()
    solid_id = get_solid_fill_pattern_id(document)
    ogs.SetProjectionLineColor(color)
    ogs.SetCutLineColor(color)
    if solid_id != DB.ElementId.InvalidElementId:
        ogs.SetSurfaceForegroundPatternId(solid_id)
        ogs.SetSurfaceForegroundPatternColor(color)
        ogs.SetCutForegroundPatternId(solid_id)
        ogs.SetCutForegroundPatternColor(color)
    return ogs


class ModelRow(object):
    def __init__(self, source_file):
        self.SourceFile = source_file
        self.LinkChoice = None
        self.ColorChoice = "Red"
        self.Isolate = True
        self.target_doc = None  # resolved DB.Document
        self.link_instance = None  # resolved DB.RevitLinkInstance or None (=host)


class ClashRow(object):
    def __init__(self, pair):
        self.TestName = pair.test_name
        self.ResultName = pair.result_name
        self.Status = pair.status
        self.NameA = pair.item_a.name
        self.SourceA = pair.item_a.source_file
        self.NameB = pair.item_b.name
        self.SourceB = pair.item_b.source_file
        self.MatchedA = ""
        self.MatchedB = ""
        self.pair = pair
        self.id_a = None
        self.id_b = None
        self.doc_a = None
        self.doc_b = None
        self.raw_xml = pair.raw_xml


def find_ifc_guid_map(document):
    cache = {}
    for el in DB.FilteredElementCollector(document).WhereElementIsNotElementType():
        p = el.get_Parameter(DB.BuiltInParameter.IFC_GUID)
        if p is None:
            continue
        val = p.AsString()
        if val:
            cache[val] = el.Id
    return cache


_ifc_cache = {}


def resolve_by_guid(document, guid):
    if not guid:
        return None
    try:
        el = document.GetElement(guid)
        if el is not None:
            return el.Id
    except Exception:
        pass

    key = document.PathName or document.Title
    if key not in _ifc_cache:
        _ifc_cache[key] = find_ifc_guid_map(document)
    return _ifc_cache[key].get(guid)


def resolve_by_numeric_id(document, numeric_id):
    if not numeric_id:
        return None
    try:
        eid = DB.ElementId(int(numeric_id))
    except Exception:
        return None
    el = document.GetElement(eid)
    return el.Id if el is not None else None


def resolve_item(document, item):
    """Try every available clue on the item, in order of reliability."""
    eid = resolve_by_guid(document, item.guid)
    if eid is not None:
        return eid, "guid"
    eid = resolve_by_numeric_id(document, item.numeric_id)
    if eid is not None:
        return eid, "id"
    return None, None


def guess_link_for_source(source_file, links):
    if not source_file:
        return None
    base = source_file.lower()
    for dotext in (".nwc", ".rvt", ".ifc", ".nwd"):
        base = base.replace(dotext, "")
    for li in links:
        link_name = li.Name.lower()
        for dotext in (".nwc", ".rvt", ".ifc", ".nwd", ".rvt (loaded)"):
            link_name = link_name.replace(dotext, "")
        if base in link_name or link_name in base:
            return li
    return None


class ClashViewerWindow(forms.WPFWindow):
    def __init__(self, xaml_path, pairs, links):
        forms.WPFWindow.__init__(self, xaml_path)
        self.pairs = pairs
        self.links = links
        self.link_names = ["<< HOST MODEL >>"] + [li.Name for li in links]

        sources = clash_parser.distinct_source_files(pairs)
        self.model_rows = ObservableCollection[object]()
        for src in sources:
            row = ModelRow(src)
            guessed = guess_link_for_source(src, links)
            row.LinkChoice = guessed.Name if guessed else self.link_names[0]
            self.model_rows.Add(row)

        self.clash_rows = ObservableCollection[object]()
        for pair in pairs:
            self.clash_rows.Add(ClashRow(pair))

        self.DataContext = self
        self.SummaryText = "{} clash(es) parsed across {} test(s), {} source model(s) found.".format(
            len(pairs),
            len(set(p.test_name for p in pairs)),
            len(sources),
        )

        self.ModelsGrid.ItemsSource = self.model_rows
        self.ClashesGrid.ItemsSource = self.clash_rows

        self.LinkColumn.ItemsSource = self.link_names
        self.LinkColumn.SelectedItemBinding = self._binding("LinkChoice")
        self.ColorColumn.ItemsSource = list(COLOR_PRESETS.keys())
        self.ColorColumn.SelectedItemBinding = self._binding("ColorChoice")

    def _binding(self, path):
        from System.Windows.Data import Binding

        return Binding(path)

    def _row_target(self, row):
        if row.LinkChoice == self.link_names[0]:
            return doc, None
        for li in self.links:
            if li.Name == row.LinkChoice:
                link_doc = li.GetLinkDocument()
                if link_doc is None:
                    forms.alert(
                        "Link '{}' is not loaded - can't resolve elements in it.".format(li.Name)
                    )
                    return None, None
                return link_doc, li
        return None, None

    def resolve_click(self, sender, args):
        row_by_source = {r.SourceFile: r for r in self.model_rows}
        for r in row_by_source.values():
            r.target_doc, r.link_instance = self._row_target(r)

        matched, total = 0, 0
        unmatched_sample = None
        for crow in self.clash_rows:
            for side_letter in ("a", "b"):
                item = getattr(crow.pair, "item_" + side_letter)
                total += 1
                row = row_by_source.get(item.source_file)
                if row is None or row.target_doc is None:
                    setattr(crow, "Matched" + side_letter.upper(), "no target")
                    continue
                eid, via = resolve_item(row.target_doc, item)
                setattr(crow, "id_" + side_letter, eid)
                setattr(crow, "doc_" + side_letter, row.target_doc)
                setattr(
                    crow,
                    "Matched" + side_letter.upper(),
                    "yes ({})".format(via) if eid else "no",
                )
                if eid:
                    matched += 1
                elif unmatched_sample is None:
                    unmatched_sample = crow

        self.ClashesGrid.Items.Refresh()
        msg = "Resolved {}/{} clash-side elements.".format(matched, total)
        if matched < total:
            msg += (
                "\n\nSome items had neither a GUID nor an element-id property, "
                "and no [id] suffix in the item name. Select an unmatched row "
                "and click 'Inspect Raw XML' to see exactly what that clash "
                "contains, then send it over so the parser can be tuned to it."
            )
        forms.alert(msg)

    def inspect_click(self, sender, args):
        selected = self.ClashesGrid.SelectedItem
        if selected is None:
            forms.alert("Select a row in the clashes grid first.")
            return
        raw = getattr(selected, "raw_xml", None) or "(no raw XML captured)"
        output.print_md("```xml\n{}\n```".format(raw))
        output.show()

    def apply_click(self, sender, args):
        row_by_source = {r.SourceFile: r for r in self.model_rows}

        # bucket matched ids by target: host doc, or per link instance
        host_ids = {}  # color_name -> set(ElementId)
        link_ids = {}  # link_instance -> (color_name, set(ElementId), isolate_flag)

        for crow in self.clash_rows:
            for side_letter in ("a", "b"):
                item = getattr(crow.pair, "item_" + side_letter)
                eid = getattr(crow, "id_" + side_letter)
                if eid is None:
                    continue
                row = row_by_source.get(item.source_file)
                if row is None:
                    continue
                if row.link_instance is None:
                    host_ids.setdefault(row.ColorChoice, set()).add(eid)
                else:
                    bucket = link_ids.setdefault(
                        row.link_instance, [row.ColorChoice, set(), row.Isolate]
                    )
                    bucket[1].add(eid)

        with revit.Transaction("Apply Clash Overrides"):
            for color_name, ids in host_ids.items():
                ogs = build_ogs(doc, COLOR_PRESETS[color_name])
                for eid in ids:
                    view.SetElementOverrides(eid, ogs)

            for li, (color_name, ids, isolate) in link_ids.items():
                link_doc = li.GetLinkDocument()
                ogs = build_ogs(link_doc, COLOR_PRESETS[color_name])
                view.SetElementOverrides(li.Id, ogs)
                if isolate:
                    id_collection = DB.List[DB.ElementId]()
                    for eid in ids:
                        id_collection.Add(eid)
                    li.SetVisibleElements(id_collection)

        output.print_md(
            "Applied overrides: **{} host element(s)**, **{} link(s)**.".format(
                sum(len(v) for v in host_ids.values()), len(link_ids)
            )
        )

    def reset_click(self, sender, args):
        with revit.Transaction("Reset Clash Overrides"):
            for li in self.links:
                li.SetVisibleElements(None)
                view.SetElementOverrides(li.Id, DB.OverrideGraphicSettings())
            for crow in self.clash_rows:
                for side_letter in ("a", "b"):
                    eid = getattr(crow, "id_" + side_letter)
                    doc_side = getattr(crow, "doc_" + side_letter)
                    if eid is not None and doc_side is doc:
                        view.SetElementOverrides(eid, DB.OverrideGraphicSettings())
        forms.alert("View overrides and link visibility reset.")

    def close_click(self, sender, args):
        self.Close()


def main():
    xml_path = forms.pick_file(file_ext="xml", title="Select Navisworks Clash Report (XML)")
    if not xml_path:
        script.exit()

    pairs = clash_parser.parse_clash_report(xml_path)
    if not pairs:
        forms.alert(
            "No clashes could be parsed from that file. Confirm it was exported "
            "as an XML report from Clash Detective (Report > Write Report > XML).",
            exitscript=True,
        )

    links = list(
        DB.FilteredElementCollector(doc)
        .OfClass(DB.RevitLinkInstance)
        .WhereElementIsNotElementType()
    )

    xaml_path = script.get_bundle_file("ClashUI.xaml")
    win = ClashViewerWindow(xaml_path, pairs, links)
    win.ShowDialog()


if __name__ == "__main__":
    main()
