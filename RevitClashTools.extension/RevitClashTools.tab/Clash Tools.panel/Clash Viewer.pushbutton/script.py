# -*- coding: utf-8 -*-
"""Import a Navisworks Clash Detective XML report, resolve each clash side
to a real Revit element, then GROUP the clashed elements by a parameter of
your choosing (source file, category, family, type, system classification,
system type/name, workset, level, clash test, or any custom parameter) and
color each group in the active view.

Workflow
--------
1. Map each source model in the report to a loaded Revit link (or host).
2. Resolve Elements - matches each clash side to a Revit element.
3. Group by <parameter> -> Build Groups. One color group is created per
   distinct parameter value across all resolved clashed elements.
4. Edit the auto-assigned color per group / untick groups to skip them.
5. Apply Colors. Reset View clears everything.

Matching strategy
-----------------
Independent of Navisworks "Write Report" export options. Per clash item,
resolved against the mapped (linked or host) document in this order:
  1. Numeric element-id (the "Element Id" smarttag, or a trailing
     "[12345]" in the item name) -> GetElement(ElementId(n)). This is
     the reliable key for Revit-authored models.
  2. GUID property -> GetElement(uniqueId), then the IFC_GUID parameter.
     Note Navisworks' own <objectattribute>GUID is a computed hash, not a
     Revit UniqueId, so this mainly helps IFC-sourced models.
Use "Inspect Raw XML" on a selected row to see what an unmatched clash
actually contains.

Known limitation (linked elements)
----------------------------------
The Revit API supports true per-element graphic overrides for
host-document elements. For linked-model elements it only supports
overrides at the link-instance level (one color per link). So when a
single link holds clashed elements belonging to more than one color
group, that link is colored by its dominant group and a warning lists
the affected links. Host elements always get exact per-group colors.
"Isolate link to clash elems" narrows each mapped link to just its
matched clash elements via SetVisibleElements.
"""

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("System.Windows.Forms")

# NOTE: the DataGrids are bound to System.Collections.Generic.List rather than
# ObservableCollection on purpose. ObservableCollection lives in the WindowsBase
# assembly, which some IronPython/pyRevit builds fail to resolve ("Cannot import
# name ObservableCollection"). List is in mscorlib (always loaded) and is fine
# for WPF binding here: each collection is populated once and assigned to
# ItemsSource, never mutated after binding, so change-notification is not needed.
from System.Collections.Generic import List

from pyrevit import revit, DB, forms, script

import clash_parser

doc = revit.doc
uidoc = revit.uidoc
view = doc.ActiveView
output = script.get_output()
logger = script.get_logger()

# Ordered so auto-assignment cycles through visually distinct hues first.
COLOR_ORDER = [
    "Red", "Blue", "Green", "Orange", "Magenta",
    "Cyan", "Yellow", "Purple", "Lime", "Pink",
    "Teal", "Brown", "Gray",
]
COLOR_PRESETS = {
    "Red": DB.Color(255, 0, 0),
    "Blue": DB.Color(0, 100, 255),
    "Green": DB.Color(0, 200, 0),
    "Orange": DB.Color(255, 140, 0),
    "Magenta": DB.Color(230, 0, 230),
    "Cyan": DB.Color(0, 200, 200),
    "Yellow": DB.Color(230, 210, 0),
    "Purple": DB.Color(130, 0, 200),
    "Lime": DB.Color(160, 230, 0),
    "Pink": DB.Color(255, 120, 180),
    "Teal": DB.Color(0, 150, 136),
    "Brown": DB.Color(140, 80, 20),
    "Gray": DB.Color(130, 130, 130),
}

GROUP_KEYS = [
    "Source File",
    "Category",
    "Family",
    "Type",
    "System Classification",
    "System Type",
    "System Name",
    "Workset",
    "Level",
    "Clash Test",
    "Custom Parameter",
]


# --------------------------------------------------------------------------
# Graphic override helpers
# --------------------------------------------------------------------------
_solid_fill_cache = {}


def get_solid_fill_pattern_id(document):
    key = document.PathName or document.Title
    if key in _solid_fill_cache:
        return _solid_fill_cache[key]
    result = DB.ElementId.InvalidElementId
    for f in DB.FilteredElementCollector(document).OfClass(DB.FillPatternElement):
        if f.GetFillPattern().IsSolidFill:
            result = f.Id
            break
    _solid_fill_cache[key] = result
    return result


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


# --------------------------------------------------------------------------
# Parameter reading for grouping
# --------------------------------------------------------------------------
def _param_str(el, bip):
    try:
        p = el.get_Parameter(bip)
    except Exception:
        return None
    if p is None:
        return None
    try:
        return p.AsValueString() or p.AsString()
    except Exception:
        return None


def get_group_value(el, document, key, source_file, test_name, custom_name):
    """Return the grouping value (string) for a resolved element."""
    if key == "Source File":
        return source_file or "(unknown source)"
    if key == "Clash Test":
        return test_name or "(no test)"
    if el is None:
        return "(element not found)"
    if key == "Category":
        return el.Category.Name if el.Category else "(no category)"
    if key == "Family":
        return _param_str(el, DB.BuiltInParameter.ELEM_FAMILY_PARAM) or "(no family)"
    if key == "Type":
        return _param_str(el, DB.BuiltInParameter.ELEM_TYPE_PARAM) or "(no type)"
    if key == "System Classification":
        return (
            _param_str(el, DB.BuiltInParameter.RBS_SYSTEM_CLASSIFICATION_PARAM)
            or "(no system classification)"
        )
    if key == "System Type":
        return (
            _param_str(el, DB.BuiltInParameter.RBS_SYSTEM_TYPE_PARAM)
            or "(no system type)"
        )
    if key == "System Name":
        return (
            _param_str(el, DB.BuiltInParameter.RBS_SYSTEM_NAME_PARAM)
            or "(no system name)"
        )
    if key == "Workset":
        try:
            if document.IsWorkshared:
                ws = document.GetWorksetTable().GetWorkset(el.WorksetId)
                return ws.Name
        except Exception:
            pass
        return "(not workshared)"
    if key == "Level":
        try:
            lvl_id = el.LevelId
            if lvl_id and lvl_id != DB.ElementId.InvalidElementId:
                lvl = document.GetElement(lvl_id)
                if lvl is not None:
                    return lvl.Name
        except Exception:
            pass
        return _param_str(el, DB.BuiltInParameter.LEVEL_PARAM) or "(no level)"
    if key == "Custom Parameter":
        if not custom_name:
            return "(no parameter name entered)"
        try:
            p = el.LookupParameter(custom_name)
        except Exception:
            p = None
        if p is None:
            return "(parameter '{}' not found)".format(custom_name)
        try:
            v = p.AsValueString() or p.AsString()
        except Exception:
            v = None
        return v or "(empty)"
    return "(unknown grouping)"


# --------------------------------------------------------------------------
# Element resolution
# --------------------------------------------------------------------------
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
    # Element Id first: Navisworks exports the Revit ElementId in a
    # smarttag, and it resolves exactly in the mapped (linked) document.
    # The <objectattribute>GUID that Navisworks writes is its own computed
    # hash, NOT a Revit UniqueId, so it only helps for IFC-sourced models
    # (matched against the IFC_GUID parameter) - hence it's the fallback.
    eid = resolve_by_numeric_id(document, item.numeric_id)
    if eid is not None:
        return eid, "id"
    eid = resolve_by_guid(document, item.guid)
    if eid is not None:
        return eid, "guid"
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


# --------------------------------------------------------------------------
# View-model rows
# --------------------------------------------------------------------------
class ModelRow(object):
    def __init__(self, source_file):
        self.SourceFile = source_file
        self.LinkChoice = None
        self.Isolate = True
        self.target_doc = None
        self.link_instance = None


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
        self.raw_xml = pair.raw_xml


class GroupRow(object):
    def __init__(self, group_value, count, color_choice):
        self.GroupValue = group_value
        self.Count = count
        self.ColorChoice = color_choice
        self.Include = True


class ResolvedElem(object):
    """A single clash side that resolved to a Revit element."""
    def __init__(self, eid, document, link_instance, source_file, test_name):
        self.eid = eid
        self.doc = document
        self.link_instance = link_instance  # None => host
        self.source_file = source_file
        self.test_name = test_name


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class ClashViewerWindow(forms.WPFWindow):
    def __init__(self, xaml_path, pairs, links):
        forms.WPFWindow.__init__(self, xaml_path)
        self.pairs = pairs
        self.links = links
        self.link_names = ["<< HOST MODEL >>"] + [li.Name for li in links]

        self.resolved = []            # list[ResolvedElem]
        self.group_rows = None        # List[GroupRow]
        self.group_by_value = {}      # group value -> GroupRow
        self.active_group_key = None
        self.active_custom_name = ""

        sources = clash_parser.distinct_source_files(pairs)
        self.model_rows = List[object]()
        for src in sources:
            row = ModelRow(src)
            guessed = guess_link_for_source(src, links)
            row.LinkChoice = guessed.Name if guessed else self.link_names[0]
            self.model_rows.Add(row)

        self.clash_rows = List[object]()
        for pair in pairs:
            self.clash_rows.Add(ClashRow(pair))

        self.SummaryText = (
            "{} clash(es) parsed across {} test(s), {} source model(s) found. "
            "Map sources -> Resolve -> choose a Group by -> Build Groups -> "
            "Apply Colors.".format(
                len(pairs),
                len(set(p.test_name for p in pairs)),
                len(sources),
            )
        )
        self.DataContext = self

        self.ModelsGrid.ItemsSource = self.model_rows
        self.ClashesGrid.ItemsSource = self.clash_rows

        self.LinkColumn.ItemsSource = self.link_names
        self.LinkColumn.SelectedItemBinding = self._binding("LinkChoice")

        self.GroupColorColumn.ItemsSource = COLOR_ORDER
        self.GroupColorColumn.SelectedItemBinding = self._binding("ColorChoice")

        self.GroupByCombo.ItemsSource = GROUP_KEYS
        self.GroupByCombo.SelectedIndex = 0

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
                        "Link '{}' is not loaded - can't resolve elements in "
                        "it.".format(li.Name)
                    )
                    return None, None
                return link_doc, li
        return None, None

    # -- Step: resolve --------------------------------------------------
    def resolve_click(self, sender, args):
        row_by_source = {r.SourceFile: r for r in self.model_rows}
        for r in row_by_source.values():
            r.target_doc, r.link_instance = self._row_target(r)

        self.resolved = []
        matched, total = 0, 0
        for crow in self.clash_rows:
            for side_letter in ("a", "b"):
                item = getattr(crow.pair, "item_" + side_letter)
                total += 1
                row = row_by_source.get(item.source_file)
                if row is None or row.target_doc is None:
                    setattr(crow, "Matched" + side_letter.upper(), "no target")
                    continue
                eid, via = resolve_item(row.target_doc, item)
                setattr(
                    crow, "Matched" + side_letter.upper(),
                    "yes ({})".format(via) if eid else "no",
                )
                if eid:
                    matched += 1
                    self.resolved.append(
                        ResolvedElem(
                            eid, row.target_doc, row.link_instance,
                            item.source_file, crow.pair.test_name,
                        )
                    )

        self.ClashesGrid.Items.Refresh()
        msg = "Resolved {}/{} clash-side elements.".format(matched, total)
        if matched == 0:
            msg += (
                "\n\nNothing resolved. Check the source->link mapping above, "
                "then use 'Inspect Raw XML' on a row to see what identifiers "
                "the report carries."
            )
        else:
            msg += "\n\nNow pick a 'Group by' and click Build Groups."
        forms.alert(msg)

    # -- Step: build groups --------------------------------------------
    def build_groups_click(self, sender, args):
        if not self.resolved:
            forms.alert("Resolve elements first (step 2).")
            return

        key = self.GroupByCombo.SelectedItem or GROUP_KEYS[0]
        custom_name = (self.CustomParamBox.Text or "").strip()
        self.active_group_key = key
        self.active_custom_name = custom_name

        counts = {}
        order = []
        for rel in self.resolved:
            el = rel.doc.GetElement(rel.eid)
            val = get_group_value(
                el, rel.doc, key, rel.source_file, rel.test_name, custom_name
            )
            if val not in counts:
                counts[val] = 0
                order.append(val)
            counts[val] += 1

        self.group_rows = List[object]()
        self.group_by_value = {}
        for i, val in enumerate(sorted(order, key=lambda v: (-counts[v], v))):
            color = COLOR_ORDER[i % len(COLOR_ORDER)]
            grow = GroupRow(val, counts[val], color)
            self.group_rows.Add(grow)
            self.group_by_value[val] = grow

        self.GroupsGrid.ItemsSource = self.group_rows
        forms.alert(
            "Built {} group(s) by '{}'. Adjust colors / includes, then "
            "Apply Colors.".format(len(order), key)
        )

    # -- Step: apply ----------------------------------------------------
    def apply_click(self, sender, args):
        if not self.group_rows:
            forms.alert("Build groups first (step 3).")
            return

        isolate_by_link = {}
        for r in self.model_rows:
            if r.link_instance is not None:
                isolate_by_link[r.link_instance] = r.Isolate

        host_buckets = {}   # color_name -> set(ElementId)
        link_color = {}     # link_instance -> {color_name: set(ElementId)}
        link_visible = {}   # link_instance -> set(ElementId)

        for rel in self.resolved:
            el = rel.doc.GetElement(rel.eid)
            val = get_group_value(
                el, rel.doc, self.active_group_key,
                rel.source_file, rel.test_name, self.active_custom_name,
            )
            grow = self.group_by_value.get(val)
            if grow is None or not grow.Include:
                continue
            color_name = grow.ColorChoice
            if rel.link_instance is None:
                host_buckets.setdefault(color_name, set()).add(rel.eid)
            else:
                li = rel.link_instance
                link_color.setdefault(li, {}).setdefault(color_name, set()).add(rel.eid)
                link_visible.setdefault(li, set()).add(rel.eid)

        mixed_links = []
        with revit.Transaction("Apply Clash Group Colors"):
            for color_name, ids in host_buckets.items():
                ogs = build_ogs(doc, COLOR_PRESETS[color_name])
                for eid in ids:
                    view.SetElementOverrides(eid, ogs)

            for li, colormap in link_color.items():
                link_doc = li.GetLinkDocument()
                # dominant color = the one covering the most elements
                dominant = max(colormap.items(), key=lambda kv: len(kv[1]))[0]
                if len(colormap) > 1:
                    mixed_links.append((li.Name, len(colormap), dominant))
                ogs = build_ogs(link_doc, COLOR_PRESETS[dominant])
                view.SetElementOverrides(li.Id, ogs)
                if isolate_by_link.get(li, False):
                    id_collection = List[DB.ElementId]()
                    for eid in link_visible[li]:
                        id_collection.Add(eid)
                    li.SetVisibleElements(id_collection)

        host_count = sum(len(v) for v in host_buckets.values())
        output.print_md(
            "**Applied colors** grouped by `{}`: "
            "{} host element(s), {} link(s).".format(
                self.active_group_key, host_count, len(link_color)
            )
        )
        if mixed_links:
            lines = "\n".join(
                "- `{}`: {} groups present, colored by dominant '{}'".format(
                    name, n, dom
                )
                for name, n, dom in mixed_links
            )
            output.print_md(
                "**Note - links with mixed groups** (Revit only allows one "
                "override color per link, so these used their dominant "
                "group's color):\n" + lines
            )
            output.show()

    # -- Utilities ------------------------------------------------------
    def inspect_click(self, sender, args):
        selected = self.ClashesGrid.SelectedItem
        if selected is None:
            forms.alert("Select a row in the clashes grid first.")
            return
        raw = getattr(selected, "raw_xml", None) or "(no raw XML captured)"
        output.print_md("```xml\n{}\n```".format(raw))
        output.show()

    def reset_click(self, sender, args):
        blank = DB.OverrideGraphicSettings()
        with revit.Transaction("Reset Clash Overrides"):
            for li in self.links:
                li.SetVisibleElements(None)
                view.SetElementOverrides(li.Id, blank)
            for rel in self.resolved:
                if rel.link_instance is None:
                    view.SetElementOverrides(rel.eid, blank)
        forms.alert("View overrides and link visibility reset.")

    def close_click(self, sender, args):
        self.Close()


def main():
    xml_path = forms.pick_file(
        file_ext="xml", title="Select Navisworks Clash Report (XML)"
    )
    if not xml_path:
        script.exit()

    pairs = clash_parser.parse_clash_report(xml_path)
    if not pairs:
        # Dump the report's actual structure to the output window so an
        # unrecognized layout can be diagnosed instead of guessed at.
        try:
            diag = clash_parser.describe_structure(xml_path)
        except Exception as ex:
            diag = "Could not analyze the file: {}".format(ex)
        output.print_md(
            "### No clashes could be parsed\n"
            "The file didn't match any clash layout the parser recognizes. "
            "The structure census below shows what's actually in it - send "
            "it over (or paste the `<clashresult>` sample) so the parser can "
            "be matched to your export.\n"
        )
        output.print_md("```\n{}\n```".format(diag))
        output.show()
        forms.alert(
            "No clashes could be parsed. A structure report was written to "
            "the pyRevit output window - share that so the parser can be "
            "tuned to this report's format.",
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
