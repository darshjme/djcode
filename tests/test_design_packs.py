"""Verify installable design resources and safe, explicit pack selection."""

import re
import xml.etree.ElementTree as ET
from importlib import resources
from pathlib import PurePosixPath

import pytest

from djcode.design_packs import get_example, get_pack, list_packs

PACK_IDS = {
    "dashboard",
    "settings",
    "command-palette",
    "onboarding-auth",
    "data-table",
    "usage-billing",
    "empty-error",
}
SECTIONS = (
    "Example",
    "Layout and responsiveness",
    "States",
    "Accessibility and keyboard",
    "Implementation",
    "Verification",
    "Sources and license",
)


def test_catalog_is_complete_and_independent():
    catalog = list_packs()
    assert len(catalog) == 7
    assert {pack["id"] for pack in catalog} == PACK_IDS
    for pack in catalog:
        assert isinstance(pack["title"], str) and pack["title"].strip()
        assert isinstance(pack["summary"], str) and len(pack["summary"].strip()) >= 20
    catalog[0]["title"] = "caller mutation"
    catalog.clear()
    assert len(list_packs()) == 7
    assert all(pack["title"] != "caller mutation" for pack in list_packs())


@pytest.mark.parametrize("pack_id", sorted(PACK_IDS))
def test_full_markdown_has_usable_sections_and_bundled_example(pack_id):
    markdown = get_pack(pack_id)
    resource_dir = resources.files("djcode").joinpath("design_patterns")
    assert markdown == resource_dir.joinpath(pack_id + ".md").read_text(encoding="utf-8")
    assert markdown.startswith("# ")
    assert len(markdown.split()) >= 200, "Pack should provide implementation context, not a stub"
    for section in SECTIONS:
        match = re.search(
            r"^## " + re.escape(section) + r"\s*\n(.*?)(?=^## |\Z)", markdown, re.M | re.S
        )
        assert match, f"Missing {section} section in {pack_id}"
        assert len(match.group(1).strip()) >= 30, f"Empty {section} section in {pack_id}"
    references = re.findall(r"!?\[[^\]]*\]\(([^\s)]+\.svg)\)", markdown)
    assert references, "Each pack must reference its installed visual example"
    for reference in references:
        path = PurePosixPath(reference)
        assert not path.is_absolute() and ".." not in path.parts
        assert len(path.parts) == 1 and path.name == pack_id + ".svg"
        assert resource_dir.joinpath(path.name).is_file()


@pytest.mark.parametrize("pack_id", sorted(PACK_IDS))
def test_svg_is_self_contained_and_accessibly_described(pack_id):
    svg = (
        resources.files("djcode")
        .joinpath("design_patterns", pack_id + ".svg")
        .read_text(encoding="utf-8")
    )
    assert get_example(pack_id) == svg
    root = ET.fromstring(svg)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.get("viewBox"), "SVG must scale to the user's viewport"
    assert root.find("{http://www.w3.org/2000/svg}title") is not None
    assert root.find("{http://www.w3.org/2000/svg}desc") is not None
    for node in root.iter():
        assert node.tag.rsplit("}", 1)[-1] not in {"script", "foreignObject", "image"}
        for attribute, value in node.attrib.items():
            assert not attribute.lower().startswith("on"), (
                "Event handlers do not belong in static examples"
            )
            if attribute.rsplit("}", 1)[-1] == "href":
                assert value.startswith("#"), "An SVG must not load external resources"
    assert not re.search(r"url\(\s*['\"]?(?:https?:|//|data:)", svg, re.I)
    assert "@import" not in svg


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "unknown",
        "../dashboard",
        "../../config.json",
        "/tmp/dashboard",
        "dashboard.svg",
        "dashboard.md",
        "dashboard/../settings",
        "https://example.com",
        "dashboard\x00",
        None,
        7,
        [],
        {},
    ],
)
@pytest.mark.parametrize("getter", [get_pack, get_example])
def test_invalid_identifiers_never_select_files(invalid, getter):
    with pytest.raises(ValueError) as caught:
        getter(invalid)
    message = str(caught.value)
    assert "dashboard" in message and "settings" in message
