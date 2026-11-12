from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    FOUND = "found"
    NO_MATCH = "no_match"
    NO_DOCUMENT = "no_document"
    NO_SECTION = "no_section"
    UNREADABLE = "unreadable"
    UNAVAILABLE = "unavailable"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Candidates[T]:
    state: State
    items: tuple[T, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class Product:
    name: str
    ingredients: tuple[str, ...]
    form: str = ""
    routes: tuple[str, ...] = ()
    strengths: tuple[str, ...] = ()
    applications: tuple[str, ...] = ()


@dataclass(frozen=True)
class Document:
    source: str
    url: str
    state: State
    text: str = ""
    date: str = ""
    version: str = ""
    section: str = "Contraindications"

    @property
    def has_text(self) -> bool:
        return self.state == State.FOUND and bool(self.text.strip())


@dataclass(frozen=True)
class DailyCandidate:
    set_id: str
    title: str
    date: str
    version: str


@dataclass(frozen=True)
class DailyOption:
    candidate: DailyCandidate
    products: tuple[Product, ...]
    document: Document


@dataclass(frozen=True)
class FdaCandidate:
    application: str
    product: Product
    label_url: str = ""
    date: str = ""
    version: str = ""
    marketing_status: str = ""

    @property
    def card_url(self) -> str:
        return (
            "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm"
            f"?event=overview.process&ApplNo={self.application[3:]}"
        )


@dataclass(frozen=True)
class Search:
    daily: Candidates[DailyOption]
    fda: Candidates[FdaCandidate]
    fda_records: Candidates[FdaCandidate]
    fda_documents: dict[str, Document]


@dataclass(frozen=True)
class Result:
    products: tuple[Product, ...]
    daily: Document | None = None
    fda: Document | None = None
    fda_candidate: FdaCandidate | None = None
    fda_match: State = State.NO_MATCH
