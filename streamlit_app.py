from html import escape

import streamlit as st

from drug_search import coordinator
from drug_search.models import Document, FdaCandidate, Product, State


st.set_page_config(page_title="Противопоказания к лекарствам", page_icon="📄")

MESSAGES = {
    State.NO_MATCH: "Совпадений не найдено.",
    State.NO_DOCUMENT: "Ссылка на утверждённую инструкцию отсутствует в данных Drugs@FDA.",
    State.NO_SECTION: "В выбранной инструкции раздел противопоказаний не найден.",
    State.UNREADABLE: "Не удалось прочитать документ.",
    State.UNAVAILABLE: "Источник или документ временно недоступен.",
    State.AMBIGUOUS: "Найдено несколько возможных записей. Однозначное сопоставление не выполнено.",
}


def render_document(document: Document) -> None:
    st.markdown(f"**{document.source}**")
    metadata = [document.date, f"версия {document.version}" if document.version else ""]
    if details := " · ".join(value for value in metadata if value):
        st.caption(details)
    if document.has_text:
        if document.section != "Contraindications":
            st.caption(document.section)
        st.html(
            '<div style="padding: 1rem 1.25rem; border-radius: 1.25rem; '
            'background: rgba(101, 143, 202, 0.12); border: 1px solid rgba(101, 143, 202, 0.30); '
            'white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.65;">'
            f'{escape(document.text.strip())}</div>'
        )
    else:
        st.info(MESSAGES[document.state])
    if document.url:
        st.link_button(f"Открыть инструкцию в {document.source}", document.url)


def fda_option(candidate: FdaCandidate) -> str:
    product = candidate.product
    return " · ".join(filter(None, [
        product.name, " + ".join(product.ingredients), product.form,
        ", ".join(product.strengths), candidate.application, candidate.marketing_status,
    ]))


def show_products(products: tuple[Product, ...]) -> None:
    presentations = {}
    for product in products:
        key = (product.name, product.ingredients, product.form, product.routes)
        presentations.setdefault(key, {}).update(dict.fromkeys(product.strengths))
    for (name, ingredients, form, routes), strengths in presentations.items():
        if name:
            st.text(name)
        if ingredients:
            st.text("Действующее вещество: " + " + ".join(ingredients))
        details = " · ".join(filter(None, (form, ", ".join(routes), ", ".join(strengths))))
        if details:
            st.caption(details)


st.title("Противопоказания к лекарствам")
st.caption("Учебный поиск по инструкциям для рынка США")
with st.form("search_form"):
    left, right = st.columns([5, 1], vertical_alignment="bottom")
    query = left.text_input("Название препарата", placeholder="Aspirin", max_chars=100)
    submit = right.form_submit_button("Найти", type="primary", use_container_width=True)

st.markdown(
    "Введите действующее вещество или торговое название на английском и нажмите «Найти». "
    "Если есть несколько вариантов, выберите нужный препарат или форму.\n\n"
    "В результате появится раздел противопоказаний на английском. "
    "По ссылке можно открыть инструкцию и проверить формулировку.\n\n"
    "Интерфейс работает на Streamlit. Два агента ищут документы в "
    "[DailyMed](https://dailymed.nlm.nih.gov/dailymed/) и "
    "[Drugs@FDA](https://www.accessdata.fda.gov/scripts/cder/daf/), "
    "а координатор сопоставляет найденные записи."
)

if submit:
    st.session_state.pop("search", None)
    st.session_state.pop("result", None)
    query = " ".join(query.split())
    if not query or not any(char.isascii() and char.isalnum() for char in query):
        st.warning("Введите название на английском.")
    else:
        with st.spinner("Ищем инструкции…"):
            st.session_state.search = coordinator.search(query)

if "search" not in st.session_state:
    st.stop()

search = st.session_state.search
if search.daily.state != State.FOUND:
    st.info(f"DailyMed. {MESSAGES[search.daily.state]}")
if search.fda.state != State.FOUND and (search.fda.state != State.NO_MATCH or not search.daily.items):
    st.info(f"Drugs@FDA. {MESSAGES[search.fda.state]}")

if search.daily.truncated or search.fda.truncated:
    st.caption("Показана часть совпадений. Уточните название, если нужного препарата нет в списке.")

tabs = []
if search.daily.items:
    tabs.append(("DailyMed", search.daily.items))
if search.fda.items:
    tabs.append(("Drugs@FDA", search.fda.items))
if not tabs:
    st.stop()

if len(tabs) == 2:
    source = st.radio("Выбрать инструкцию из", [source for source, _ in tabs], horizontal=True)
else:
    source = tabs[0][0]
items = dict(tabs)[source]
if len(items) == 1:
    selected = items[0]
else:
    formatter = (lambda item: f"{item.title} · {item.date}") if source == "DailyMed" else fda_option
    selected = st.selectbox("Выберите препарат или форму", items, index=None, format_func=formatter)
if selected is None:
    st.stop()

if st.session_state.get("result_key") != selected or "result" not in st.session_state:
    with st.spinner("Читаем инструкцию…"):
        st.session_state.result = (coordinator.for_daily(selected, search.fda) if source == "DailyMed"
                                   else coordinator.for_fda(selected))
        st.session_state.result_key = selected

result = st.session_state.result
st.subheader("Противопоказания")
show_products(result.products)
if not result.products and source == "DailyMed":
    st.text(selected.title)
if result.fda_candidate and result.fda_candidate.marketing_status:
    st.caption(f"Статус продукта по Drugs@FDA: {result.fda_candidate.marketing_status}")

if result.fda:
    if result.daily and result.daily.state == result.fda.state == State.FOUND:
        if result.daily.text.split() == result.fda.text.split():
            st.success("Текст раздела совпадает в двух найденных документах.")
        else:
            st.info("Формулировки в найденных документах различаются. Проверьте дату и версию каждой инструкции.")
    render_document(result.fda)
    st.link_button("Открыть регистрационную карточку FDA", result.fda_candidate.card_url)
if result.daily:
    render_document(result.daily)
    if result.fda is None:
        st.info(f"Сверка с Drugs@FDA. {MESSAGES[result.fda_match]}")
