from concurrent.futures import ThreadPoolExecutor

from . import dailymed, fda
from .models import Candidates, DailyCandidate, FdaCandidate, Product, Result, Search, State


def search(query: str) -> Search:
    with ThreadPoolExecutor(max_workers=2) as executor:
        daily_task = executor.submit(dailymed.search, query)
        fda_task = executor.submit(fda.search, query)
        return Search(daily_task.result(), fda_task.result())


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


def for_daily(candidate: DailyCandidate, known_fda: Candidates[FdaCandidate]) -> Result:
    products, daily = dailymed.document(candidate)
    applications = sorted({number for product in products for number in product.applications
                           if fda.APPLICATION.fullmatch(number)})
    # An application ID from SPL is more precise than the initial name search.
    if len(applications) == 1:
        targeted = fda.search(applications[0], application=True)
        if targeted.state != State.UNAVAILABLE or known_fda.state == State.UNAVAILABLE:
            known_fda = targeted
    status, paired = match(products, known_fda)
    if paired is not None:
        return Result(products, daily, fda.document(paired), paired, status)
    return Result(products, daily=daily, fda_match=status)


def for_fda(candidate: FdaCandidate) -> Result:
    return Result((candidate.product,), fda=fda.document(candidate), fda_candidate=candidate)
