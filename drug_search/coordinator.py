from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor

from . import dailymed, fda
from .models import Candidates, DailyOption, Document, FdaCandidate, Product, Result, Search, State


def search(query: str) -> Search:
    with ThreadPoolExecutor(max_workers=8) as executor:
        daily_task = executor.submit(dailymed.search, query)
        fda_task = executor.submit(fda.search, query)
        daily_records, fda_records = daily_task.result(), fda_task.result()

        # One FDA label can cover multiple candidate products. Read it once.
        unique_fda = {candidate.label_url: candidate for candidate in fda_records.items}
        fda_tasks = {url: executor.submit(fda.document, candidate)
                     for url, candidate in unique_fda.items()}
        daily_tasks = [executor.submit(dailymed.document, candidate) for candidate in daily_records.items]
        daily_options = tuple(DailyOption(candidate, *task.result())
                              for candidate, task in zip(daily_records.items, daily_tasks))
        fda_documents = {url: task.result() for url, task in fda_tasks.items()}

    daily_items = tuple(option for option in daily_options if option.document.has_text)
    fda_items = tuple(candidate for candidate in fda_records.items
                      if fda_documents[candidate.label_url].has_text)
    return Search(
        Candidates(screened_state(daily_records.state, [item.document for item in daily_options]),
                   daily_items, daily_records.truncated),
        Candidates(screened_state(fda_records.state, fda_documents.values()),
                   fda_items, fda_records.truncated),
        fda_records,
        fda_documents,
    )


def screened_state(initial: State, documents: Collection[Document]) -> State:
    if initial != State.FOUND:
        return initial
    if any(document.has_text for document in documents):
        return State.FOUND
    states = {document.state for document in documents}
    for state in (State.UNAVAILABLE, State.UNREADABLE, State.NO_SECTION, State.NO_DOCUMENT):
        if state in states:
            return state
    return State.NO_SECTION


def form_key(form: str) -> tuple[str, ...]:
    return tuple(sorted(fda.normalize(form).split()))


def compatible(spl: Product, candidate: FdaCandidate) -> bool:
    product = candidate.product
    if spl.applications and candidate.application not in spl.applications:
        return False
    names = {fda.normalize(name) for name in spl.ingredients}
    routes = {fda.normalize(route) for route in spl.routes}
    return (
        fda.normalize(spl.name) == fda.normalize(product.name)
        and bool(names)
        and names == {fda.normalize(name) for name in product.ingredients}
        and bool(spl.form)
        and form_key(spl.form) == form_key(product.form)
        and bool(routes)
        and routes == {fda.normalize(route) for route in product.routes}
    )


def match(products: tuple[Product, ...], candidates: Candidates[FdaCandidate]) -> tuple[State, FdaCandidate | None]:
    if candidates.state == State.UNAVAILABLE:
        return State.UNAVAILABLE, None
    # A multi-product document must match all of its presentations. A mere name
    # hit or an FDA application with a different form is insufficient.
    matches = [candidate for candidate in candidates.items
               if products and all(compatible(product, candidate) for product in products)]
    distinct = {}
    for candidate in matches:
        key = (candidate.application, candidate.label_url)
        distinct.setdefault(key, candidate)
    if len(distinct) > 1 or (distinct and candidates.truncated):
        return State.AMBIGUOUS, None
    if distinct:
        return State.FOUND, next(iter(distinct.values()))
    return State.NO_MATCH, None


def for_daily(option: DailyOption, search_result: Search) -> Result:
    products, daily = option.products, option.document
    known_fda = search_result.fda_records
    applications = sorted({number for product in products for number in product.applications
                           if fda.APPLICATION.fullmatch(number)})
    # An application ID from SPL is more precise than the initial name search.
    if len(applications) == 1:
        targeted = fda.search(applications[0], application=True)
        if targeted.state != State.UNAVAILABLE or known_fda.state == State.UNAVAILABLE:
            known_fda = targeted
    status, paired = match(products, known_fda)
    if paired is not None:
        document = search_result.fda_documents.get(paired.label_url) or fda.document(paired)
        return Result(products, daily, document, paired, status)
    return Result(products, daily=daily, fda_match=status)


def for_fda(candidate: FdaCandidate, search_result: Search) -> Result:
    return Result((candidate.product,), fda=search_result.fda_documents[candidate.label_url], fda_candidate=candidate)
