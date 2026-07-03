# -*- coding: utf-8 -*-
"""Flexible parser for Navisworks Clash Detective XML reports.

Navisworks XML report tag names have drifted a bit across versions, and
- more importantly - which per-item *properties* get written out is
controlled by checkboxes in the "Write Report" dialog, so you cannot
depend on any single field (like a GUID smarttag) always being present.

To stay useful no matter what was checked when the report was written,
this collects, for each clash side:
  - name:       the leaf item's display name
  - source_file: the top-most path item (the model the element came from)
  - guid:       any smarttag/property whose name contains "guid" (Revit
                UniqueId, or an IFC GUID)
  - numeric_id: any smarttag/property that looks like a bare element id
                (name contains "id" but not "guid", value is digits), OR
                failing that, a trailing "[12345]" / "(12345)" pulled out
                of the item's own name - Revit's Navisworks exporter
                bakes the ElementId into the item name by default, so
                this works even when no item properties were exported
                at all.

script.py tries guid first, then numeric_id, so a report is usable as
long as *either* piece of information survived the export.
"""

import re
import xml.etree.ElementTree as ET


GUID_TAG_HINTS = ("guid",)  # property names containing any of these (lowercased)
ID_TAG_HINTS = ("elementid", "element id", "revit id", "item id", "id")
NAME_ATTR = "name"

TRAILING_ID_RE = re.compile(r"[\[\(]\s*(\d{3,10})\s*[\]\)]\s*$")


class ClashItem(object):
    def __init__(self, name, source_file, guid, numeric_id):
        self.name = name
        self.source_file = source_file
        self.guid = guid
        self.numeric_id = numeric_id

    def __repr__(self):
        return "ClashItem(name={!r}, source={!r}, guid={!r}, numeric_id={!r})".format(
            self.name, self.source_file, self.guid, self.numeric_id
        )


class ClashPair(object):
    def __init__(self, test_name, result_name, status, item_a, item_b, raw_xml=None):
        self.test_name = test_name
        self.result_name = result_name
        self.status = status
        self.item_a = item_a
        self.item_b = item_b
        self.raw_xml = raw_xml


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _all_properties(node):
    """Yield (name, value) for every property-like element under node -
    covers <smarttag name=.. value=..>, <property name=..><value>..</value>
    </property>, and plain attribute-bearing tags, since report styles vary."""
    for el in node.iter():
        tag = _local(el.tag)
        if tag in ("smarttag", "property", "userdata"):
            name = el.get(NAME_ATTR) or el.get("Name") or ""
            value = el.get("value") or el.get("Value")
            if value is None:
                # value may live in a child <value> element or as text
                value_el = None
                for child in el:
                    if _local(child.tag) == "value":
                        value_el = child
                        break
                value = (value_el.text if value_el is not None else el.text) or ""
            yield name.strip(), value.strip()


def _find_guid(properties):
    for name, value in properties:
        lname = name.lower()
        if any(h in lname for h in GUID_TAG_HINTS) and value:
            return value.strip("{}")
    return None


def _find_numeric_id(properties):
    for name, value in properties:
        lname = name.lower()
        if "guid" in lname:
            continue
        if any(h in lname for h in ID_TAG_HINTS):
            digits = "".join(ch for ch in value if ch.isdigit())
            if digits and digits == value.strip("-"):
                return digits
    return None


def _path_names(node):
    """Collect the chain of <item name="..."> ancestors/descendants under a
    clash side, in document order. First = source file, last = element."""
    names = []
    for item in node.iter():
        if _local(item.tag) not in ("item", "pathitem"):
            continue
        nm = item.get(NAME_ATTR) or item.get("Name")
        if nm:
            names.append(nm)
    return names


def _parse_side(node):
    names = _path_names(node)
    source_file = names[0] if names else None
    elem_name = names[-1] if names else node.get(NAME_ATTR, "unknown")

    properties = list(_all_properties(node))
    guid = _find_guid(properties)
    numeric_id = _find_numeric_id(properties)

    if not numeric_id:
        m = TRAILING_ID_RE.search(elem_name)
        if m:
            numeric_id = m.group(1)

    return ClashItem(elem_name, source_file, guid, numeric_id)


def _find_two_sides(clashresult_node):
    """Find the two per-side nodes inside a <clashresult>."""
    sides = []
    for child in clashresult_node:
        tag = _local(child.tag)
        if tag in ("object1", "object2", "clashobject"):
            sides.append(child)
        elif tag in ("clashresultpair", "clashresultpaths"):
            for sub in child:
                if _local(sub.tag) in ("object1", "object2", "clashobject", "item"):
                    sides.append(sub)
    if len(sides) >= 2:
        return sides[0], sides[1]
    return None, None


def parse_clash_report(xml_path):
    """Returns a list of ClashPair."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    pairs = []
    for test_node in root.iter():
        if _local(test_node.tag) != "clashtest":
            continue
        test_name = test_node.get(NAME_ATTR, "Unnamed Test")

        for result_node in test_node.iter():
            if _local(result_node.tag) != "clashresult":
                continue
            result_name = result_node.get(NAME_ATTR, "")
            status = result_node.get("status", "")

            side_a, side_b = _find_two_sides(result_node)
            if side_a is None or side_b is None:
                continue

            item_a = _parse_side(side_a)
            item_b = _parse_side(side_b)
            try:
                raw_xml = ET.tostring(result_node, encoding="unicode")
            except Exception:
                raw_xml = None
            pairs.append(
                ClashPair(test_name, result_name, status, item_a, item_b, raw_xml)
            )

    return pairs


def distinct_source_files(pairs):
    files = set()
    for p in pairs:
        for item in (p.item_a, p.item_b):
            if item.source_file:
                files.add(item.source_file)
    return sorted(files)
