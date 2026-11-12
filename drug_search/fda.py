import json
import re
from dataclasses import replace
from io import BytesIO
from urllib.parse import urlsplit, urlunsplit

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .models import Candidates, Document, FdaCandidate, Product, State
from .transport import SourceError, fetch, source_search


API = "https://api.fda.gov/drug/drugsfda.json"
APPLICATION = re.compile(r"(?:NDA|ANDA|BLA)[0-9]{6}")
PAGE_SIZE = 30
LABEL_HOSTS = {"accessdata.fda.gov", "www.accessdata.fda.gov", "www.fda.gov", "fda.gov"}


def normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def label_url(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme in {"http", "https"} and parts.netloc in LABEL_HOSTS
            and parts.path.lower().endswith(".pdf")):
        return urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))
    return ""


def date_text(date: str) -> str:
    return f"{date[:4]}-{date[4:6]}-{date[6:]}" if re.fullmatch(r"[0-9]{8}", date) else date


def approved_label(submissions: list) -> tuple[str, str, str]:
    documents = []
    for submission in submissions:
        if submission.get("submission_status") != "AP":
            continue
        for entry in submission.get("application_docs") or []:
            if (entry.get("type") or "").casefold() != "label":
                continue
            if url := label_url(entry.get("url") or ""):
                approval = submission.get("submission_status_date") or ""
                date = entry.get("date") or approval
                version = " ".join(str(submission[key]) for key in ("submission_type", "submission_number")
                                   if submission.get(key))
                documents.append((approval or date, date, url, version))
    if not documents:
        return "", "", ""
    _, date, url, version = max(documents)
    return url, date_text(date), version


def products_from(application: dict) -> tuple[FdaCandidate, ...]:
    number = application.get("application_number", "")
    if not APPLICATION.fullmatch(number):
        return ()
    url, date, version = approved_label(application.get("submissions") or [])
    grouped: dict[tuple, FdaCandidate] = {}
    for entry in application.get("products") or []:
        name = entry.get("brand_name")
        if not name:
            continue
        active = entry.get("active_ingredients") or []
        ingredients = tuple(item["name"] for item in active if item.get("name"))
        strengths = tuple(item["strength"].split(" **", 1)[0] for item in active if item.get("strength"))
        form, route = entry.get("dosage_form") or "", entry.get("route") or ""
        status = entry.get("marketing_status") or ""
        key = (name, ingredients, form, route, status)
        if key in grouped:
            previous = grouped[key].product
            strengths = tuple(dict.fromkeys((*previous.strengths, *strengths)))
        product = Product(name, ingredients, form, (route,) if route else (), strengths, (number,))
        grouped[key] = FdaCandidate(number, product, url, date, version, status)
    return tuple(grouped.values())


def search(query: str, *, application: bool = False) -> Candidates[FdaCandidate]:
    def run():
        quoted = json.dumps(query)
        if application:
            expression = f"application_number:{quoted}"
        else:
            expression = f"products.brand_name:{quoted} OR products.active_ingredients.name:{quoted}"
        response = json.loads(fetch(API, {"search": expression, "limit": PAGE_SIZE}, empty_404=True))
        items = [candidate for entry in response["results"] for candidate in products_from(entry)]
        if not application:
            wanted = f" {normalize(query)} "
            items = [
                item for item in items
                if wanted in f" {normalize(item.product.name)} "
                or any(wanted in f" {normalize(name)} " for name in item.product.ingredients)
            ]
        total = response.get("meta", {}).get("results", {}).get("total", 0)
        return Candidates(
            State.FOUND if items else State.NO_MATCH, tuple(items), total > PAGE_SIZE,
        )

    return source_search(run)


FULL = re.compile(r"(?im)^[ \t]*FULL PRESCRIBING INFORMATION\s*:?\s*$")
SECTION = re.compile(r"(?im)^[ \t]*4[ \t]*[.:-]?[ \t]*CONTRAINDICATIONS[ \t]*[:.]?[ \t]*$")
NEXT = re.compile(
    r"(?im)^[ \t]*5[ \t]*[.:-]?[ \t]*WARNINGS(?:[ \t]+AND[ \t]+PRECAUTIONS)?[ \t]*[:.]?[ \t]*$"
)
LEGACY_SECTION = re.compile(r"(?im)^[ \t]*CONTRAINDICATIONS[ \t]*:?[ \t]*$")
LEGACY_NEXT = re.compile(
    r"(?im)^[ \t]*(?:WARNINGS(?:[ \t]+AND[ \t]+PRECAUTIONS)?|PRECAUTIONS|"
    r"ADVERSE REACTIONS|DOSAGE AND ADMINISTRATION)[ \t]*:?[ \t]*$"
)


def pdf_section(text: str) -> str:
    """Extract a bounded section. A table-of-contents entry has no body."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if full := FULL.search(text):
        text = text[full.end():]
    for heading, following in ((SECTION, NEXT), (LEGACY_SECTION, LEGACY_NEXT)):
        for start in reversed(list(heading.finditer(text))):
            end = following.search(text, start.end())
            if end is None:
                continue
            body = text[start.end():end.start()].strip()
            if len(re.findall(r"[A-Za-z]+", body)) >= 1:
                return body
    return ""


def document(candidate: FdaCandidate) -> Document:
    record = Document("Drugs@FDA", candidate.label_url, State.NO_DOCUMENT, date=candidate.date,
                      version=candidate.version)
    if not candidate.label_url:
        return record
    try:
        content = fetch(candidate.label_url)
    except SourceError:
        return replace(record, state=State.UNAVAILABLE)
    try:
        pdf = PdfReader(BytesIO(content))
        pages = []
        for page in pdf.pages:
            lines = (page.extract_text() or "").strip().splitlines()
            if lines and re.fullmatch(r"[0-9]{1,3}", lines[-1].strip()):
                lines.pop()
            pages.append("\n".join(lines))
    except (PdfReadError, ValueError, TypeError, OSError, KeyError):
        return replace(record, state=State.UNREADABLE)
    text = "\n".join(pages)
    if not text.strip():
        return replace(record, state=State.UNREADABLE)
    section = pdf_section(text)
    return replace(record, state=State.FOUND if section else State.NO_SECTION, text=section)
