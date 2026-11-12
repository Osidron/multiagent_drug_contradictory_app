import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from drug_search import coordinator, dailymed, fda
from drug_search.models import Candidates, DailyCandidate, Document, FdaCandidate, Product, State
from drug_search.transport import SourceError


SET_ID = "12345678-1234-1234-1234-123456789abc"
DAILY = DailyCandidate(SET_ID, "EXAMPLE (EXAMINE) TABLET", "Sep 1, 2026", "7")
DAILY_WITHOUT_SECTION = DailyCandidate("22345678-1234-1234-1234-123456789abc", "EXAMPLE CAPSULE", "", "1")
DAILY_SECOND_FORM = DailyCandidate("32345678-1234-1234-1234-123456789abc", "EXAMPLE SOLUTION", "", "1")
PRODUCT = Product("Example", ("Examine",), "TABLET, EXTENDED RELEASE", ("ORAL",), ("10 mg",), ("NDA123456",))
FDA = FdaCandidate("NDA123456", PRODUCT, "https://www.accessdata.fda.gov/example.pdf")
PDF = Document("Drugs@FDA", FDA.label_url, State.FOUND, "A supported restriction.")
SPL = f"""
<document xmlns="urn:hl7-org:v3">
  <setId root="{SET_ID}" /><versionNumber value="7" /><effectiveTime value="20260901" />
  <component><structuredBody>
    <component><section>
      <code code="48780-1" />
      <subject><manufacturedProduct>
        <manufacturedProduct>
          <name>Example</name><formCode displayName="TABLET, EXTENDED RELEASE" />
          <ingredient classCode="ACTIM">
            <quantity><numerator value="10" unit="mg" /><denominator value="1" unit="1" /></quantity>
            <ingredientSubstance><name>Examine</name></ingredientSubstance>
          </ingredient>
          <ingredient classCode="IACT"><ingredientSubstance><name>Excipient</name></ingredientSubstance></ingredient>
          <subjectOf><approval><id root="2.16.840.1.113883.3.150" extension="NDA123456" /></approval></subjectOf>
        </manufacturedProduct>
        <consumedIn><substanceAdministration><routeCode displayName="ORAL" /></substanceAdministration></consumedIn>
      </manufacturedProduct></subject>
    </section></component>
    <component><section>
      <code code="34070-3" />
      <text><paragraph>Values &lt; 30.</paragraph><list><item>Allergy to <content>Examine</content>.</item></list></text>
    </section></component>
    <component><section><code code="43685-7" /><text>Separate warning.</text></section></component>
  </structuredBody></component>
</document>
"""


class ExtractTests(unittest.TestCase):
    def test_extracts_only_contraindications_from_spl_and_binds_version(self):
        products, label = dailymed.parse_label(SPL.encode(), DAILY)
        self.assertEqual(products, (PRODUCT,))
        self.assertEqual(label.state, State.FOUND)
        self.assertEqual(label.text, "Values < 30.\nAllergy to Examine.")
        self.assertEqual(label.date, "2026-09-01")
        self.assertTrue(label.url.endswith("&version=7"))

    def test_missing_spl_section_differs_from_network_failure(self):
        _, missing = dailymed.parse_label(SPL.replace("34070-3", "99999-9").encode(), DAILY)
        with patch.object(dailymed, "fetch", side_effect=SourceError("503")):
            _, failed = dailymed.document(DAILY)
        self.assertEqual((missing.state, failed.state), (State.NO_SECTION, State.UNAVAILABLE))

    def test_extracts_otc_do_not_use_as_its_own_section(self):
        xml = SPL.replace("34070-3", "50570-1").replace("<name>Example</name>",
                                                         "<name>Example<suffix>Low Dose</suffix></name>")
        products, label = dailymed.parse_label(xml.encode(), DAILY)
        self.assertEqual(products[0].name, "Example Low Dose")
        self.assertTrue(label.has_text)
        self.assertEqual(label.section, "Do not use")
        self.assertNotIn("Separate warning", label.text)

    def test_wrong_spl_identifier_is_rejected(self):
        with self.assertRaises(ValueError):
            dailymed.parse_label(SPL.replace(SET_ID, "different").encode(), DAILY)

    def test_pdf_selects_full_section_over_table_of_contents(self):
        pdf = ("CONTENTS\n4 CONTRAINDICATIONS\n5 WARNINGS AND PRECAUTIONS\n"
               "FULL PRESCRIBING INFORMATION\n4 CONTRAINDICATIONS\nValues < 30.\n"
               "5 WARNINGS AND PRECAUTIONS\nSeparate warning.")
        self.assertEqual(fda.pdf_section(pdf), "Values < 30.")

    def test_pdf_with_only_table_of_contents_is_not_evidence(self):
        self.assertEqual(fda.pdf_section("4 CONTRAINDICATIONS\n5 WARNINGS AND PRECAUTIONS"), "")

    def test_chooses_newest_approved_label_and_upgrades_legacy_url(self):
        def submission(status, date, name):
            return {"submission_status": status, "submission_status_date": date,
                    "application_docs": [{"type": "Label", "url": f"http://www.accessdata.fda.gov/{name}.pdf"}]}
        entries = [submission("AP", "20260101", "new"), submission("AP", "20250101", "old"),
                   submission("RE", "20260901", "not-approved")]
        self.assertEqual(fda.approved_label(entries)[:2],
                         ("https://www.accessdata.fda.gov/new.pdf", "2026-01-01"))
        self.assertEqual(fda.label_url("https://www.accessdata.fda.gov.evil.example/example.pdf"), "")


class MatchTests(unittest.TestCase):
    def test_matches_composition_form_route_and_application(self):
        status, selected = coordinator.match((PRODUCT,), Candidates(State.FOUND, (FDA,)))
        self.assertEqual((status, selected), (State.FOUND, FDA))

    def test_same_application_and_name_do_not_override_wrong_form(self):
        different = Product(PRODUCT.name, PRODUCT.ingredients, "TABLET", PRODUCT.routes)
        wrong = FdaCandidate(FDA.application, different)
        self.assertEqual(coordinator.match((PRODUCT,), Candidates(State.FOUND, (wrong,))),
                         (State.NO_MATCH, None))

    def test_similar_products_across_applications_remain_ambiguous(self):
        without_id = Product(PRODUCT.name, PRODUCT.ingredients, PRODUCT.form, PRODUCT.routes)
        other = FdaCandidate("NDA987654", without_id)
        self.assertEqual(coordinator.match((without_id,), Candidates(State.FOUND, (FDA, other))),
                         (State.AMBIGUOUS, None))

    @patch.object(dailymed, "search", return_value=Candidates(State.NO_MATCH))
    @patch.object(fda, "document")
    @patch.object(fda, "search")
    def test_screens_fda_candidates_and_reuses_the_document(self, search, document, _):
        shared = FdaCandidate(FDA.application, Product("Second", ("Examine",)), FDA.label_url)
        empty = FdaCandidate("NDA999999", PRODUCT, "https://www.accessdata.fda.gov/empty.pdf")
        search.return_value = Candidates(State.FOUND, (FDA, shared, empty))
        document.side_effect = lambda item: (PDF if item.label_url == FDA.label_url else
                                             Document("Drugs@FDA", item.label_url, State.NO_SECTION))
        results = coordinator.search("Example")
        self.assertEqual(results.fda.items, (FDA, shared))
        self.assertEqual(coordinator.for_fda(shared, results).fda, PDF)
        self.assertEqual(document.call_count, 2)


class AppTests(unittest.TestCase):
    @patch.object(fda, "document", return_value=PDF)
    @patch.object(fda, "search", return_value=Candidates(State.FOUND, (FDA,)))
    @patch.object(dailymed, "fetch", side_effect=SourceError("timeout"))
    def test_shows_fda_document_when_dailymed_fails(self, *_):
        app = AppTest.from_file("../streamlit_app.py").run()
        app.text_input[0].set_value("Example")
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("DailyMed" in item.value and "недоступен" in item.value for item in app.info))
        self.assertTrue(any(PDF.text in item.value for item in app.get("html")))

    @patch.object(fda, "search", return_value=Candidates(State.NO_MATCH))
    @patch.object(dailymed, "search", return_value=Candidates(State.FOUND, (
        DAILY, DAILY_WITHOUT_SECTION, DAILY_SECOND_FORM
    )))
    @patch.object(dailymed, "document")
    def test_selection_hides_forms_without_text_and_reuses_loaded_document(self, document, *_):
        document.side_effect = lambda item: ((PRODUCT,), Document(
            "DailyMed", "https://dailymed.nlm.nih.gov/example",
            State.NO_SECTION if item == DAILY_WITHOUT_SECTION else State.FOUND,
            "" if item == DAILY_WITHOUT_SECTION else "Only this form. Values < 30."
        ))
        app = AppTest.from_file("../streamlit_app.py").run()
        self.assertEqual(app.text_input[0].proto.placeholder, "Aspirin")
        app.text_input[0].set_value("Example")
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.selectbox[0].options), 2)
        self.assertTrue(all("CAPSULE" not in option for option in app.selectbox[0].options))
        self.assertFalse(app.get("html"))
        app.selectbox[0].select_index(0).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("Only this form. Values &lt; 30." in item.value for item in app.get("html")))
        self.assertEqual(document.call_count, 3)


if __name__ == "__main__":
    unittest.main()
