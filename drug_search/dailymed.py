import json
import re
from xml.etree.ElementTree import Element, ParseError
from uuid import UUID

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .models import Candidates, DailyCandidate, Document, Product, State
from .transport import SourceError, fetch, source_search


BASE = "https://dailymed.nlm.nih.gov/dailymed"
API = f"{BASE}/services/v2"
PAGE_SIZE = 40
NS = {"spl": "urn:hl7-org:v3"}
CONTRAINDICATIONS = "34070-3"
OTC_DO_NOT_USE = "50570-1"


def search(query: str) -> Candidates[DailyCandidate]:
    def run():
        data = json.loads(fetch(f"{API}/spls.json", {"drug_name": query, "pagesize": PAGE_SIZE}))
        items = []
        for row in data["data"]:
            set_id = str(UUID(row["setid"]))
            items.append(DailyCandidate(
                set_id, row["title"], row.get("published_date") or "",
                str(row.get("spl_version") or ""),
            ))
        total = int(data.get("metadata", {}).get("total_elements") or len(items))
        return Candidates(State.FOUND if items else State.NO_MATCH, tuple(items), total > len(items))

    return source_search(run)


def attr(node: Element, path: str, key: str) -> str:
    element = node.find(path, NS)
    return (element.get(key) or "").strip() if element is not None else ""


def clean(value: str) -> str:
    return " ".join(value.split())


def ordered(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def strengths_for(ingredient: Element) -> str:
    value = attr(ingredient, "spl:quantity/spl:numerator", "value")
    unit = attr(ingredient, "spl:quantity/spl:numerator", "unit")
    denominator = attr(ingredient, "spl:quantity/spl:denominator", "value")
    denominator_unit = attr(ingredient, "spl:quantity/spl:denominator", "unit")
    if not value:
        return ""
    strength = clean(f"{value} {unit}")
    if denominator and (denominator != "1" or denominator_unit not in {"", "1"}):
        strength += f" / {denominator} {denominator_unit}".rstrip()
    return strength


def products_from(root: Element) -> tuple[Product, ...]:
    products = []
    for outer in root.findall(".//spl:subject/spl:manufacturedProduct", NS):
        for node in outer.findall("spl:manufacturedProduct", NS):
            name = node.find("spl:name", NS)
            if name is None:
                continue
            active = [item for item in node.findall("spl:ingredient", NS)
                      if (item.get("classCode") or "").startswith("ACT")]
            ingredients = ordered(
                clean(item.findtext("spl:ingredientSubstance/spl:name", "", NS)) for item in active
            )
            if not ingredients:
                ingredients = ordered([clean(node.findtext(
                    "spl:asEntityWithGeneric/spl:genericMedicine/spl:name", "", NS
                ))])
            products.append(Product(
                clean(" ".join(name.itertext())), ingredients,
                attr(node, "spl:formCode", "displayName"),
                ordered(code.get("displayName", "") for code in outer.findall(
                    "spl:consumedIn/spl:substanceAdministration/spl:routeCode", NS
                )),
                ordered(strengths_for(item) for item in active),
                ordered(code.get("extension", "").upper() for code in node.findall(
                    "spl:subjectOf/spl:approval/spl:id", NS
                ) if code.get("root") == "2.16.840.1.113883.3.150"),
            ))
    return tuple(dict.fromkeys(products))


BLOCKS = {"paragraph", "list", "item", "tr", "br"}
CELLS = {"th", "td"}


def narrative(node: Element) -> str:
    """Retain meaningful line breaks and separate table cells in SPL text."""
    def fragments(element):
        if element.text:
            yield re.sub(r"\s+", " ", element.text)
        for child in element:
            name = child.tag.rsplit("}", 1)[-1]
            if name in BLOCKS:
                yield "\n"
            yield from fragments(child)
            if name in BLOCKS:
                yield "\n"
            if name in CELLS:
                yield " | "
            if child.tail:
                yield re.sub(r"\s+", " ", child.tail)

    return "\n".join(line for raw in "".join(fragments(node)).splitlines()
                     if (line := clean(raw).rstrip(" |")))


def parse_label(content: bytes, candidate: DailyCandidate) -> tuple[tuple[Product, ...], Document]:
    root = ElementTree.fromstring(content)
    if attr(root, "spl:setId", "root").lower() != candidate.set_id:
        raise ValueError("Unexpected SPL set ID")
    version = attr(root, "spl:versionNumber", "value")
    if not version.isdecimal():
        version = ""
    url = f"{BASE}/drugInfo.cfm?setid={candidate.set_id}"
    if version:
        url += f"&version={version}"
    effective = attr(root, "spl:effectiveTime", "value")
    date = f"{effective[:4]}-{effective[4:6]}-{effective[6:]}" if len(effective) == 8 else candidate.date
    sections: dict[str, list[str]] = {CONTRAINDICATIONS: [], OTC_DO_NOT_USE: []}
    for section in root.findall(".//spl:section", NS):
        code = attr(section, "spl:code", "code")
        if code in sections:
            for body in section.findall("spl:text", NS):
                if text := narrative(body):
                    sections[code].append(text)
    code = CONTRAINDICATIONS if sections[CONTRAINDICATIONS] else OTC_DO_NOT_USE
    text = "\n\n".join(dict.fromkeys(sections[code]))
    title = "Do not use" if text and code == OTC_DO_NOT_USE else "Contraindications"
    return products_from(root), Document(
        "DailyMed", url, State.FOUND if text else State.NO_SECTION, text, date, version, title
    )


def document(candidate: DailyCandidate) -> tuple[tuple[Product, ...], Document]:
    url = f"{BASE}/drugInfo.cfm?setid={candidate.set_id}"
    try:
        content = fetch(f"{API}/spls/{candidate.set_id}.xml")
    except SourceError:
        return (), Document("DailyMed", url, State.UNAVAILABLE)
    try:
        return parse_label(content, candidate)
    except (ParseError, DefusedXmlException, ValueError):
        return (), Document("DailyMed", url, State.UNREADABLE)
