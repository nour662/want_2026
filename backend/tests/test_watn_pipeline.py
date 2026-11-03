from datetime import date

from app.services.watn_pipeline import classify, merge
from app.services.watn_pipeline.evaluate import evaluate_funding, is_publishable
from app.services.watn_pipeline.inputs import load_participants
from app.services.watn_pipeline.matching import score_entity
from app.services.watn_pipeline.models import CompanyInput, FundingRecord
from app.services.watn_pipeline.outputs import _match_baseline
from app.services.watn_pipeline.sources.tedco import Article, TedcoArchive


def rec(source, amount, day, award=None, **kw):
    return FundingRecord(source=source, amount=amount, date_awarded=day, award_number=award, confidence=100, **kw)


def test_sbir_and_usas_copies_of_one_award_merge_on_award_number():
    sbir = rec("sbir", 150000, date(2023, 6, 1), "W911QX-23-C-0012", funding_source="SBIR", phase="I")
    usas = rec("usas_contracts", 149500, date(2023, 6, 20), "W911QX23C0012", raw_type="DEFINITIVE CONTRACT",
               source_url="https://www.usaspending.gov/award/CONT_AWD_W911QX23C0012")
    merged = merge.merge_funding([sbir, usas])
    assert len(merged) == 1
    m = merged[0]
    assert m.amount == 149500  # USAspending amount wins
    assert m.funding_source == "SBIR" and m.phase == "I"  # SBIR knows program and phase
    assert m.source_url.startswith("https://www.usaspending.gov")
    assert m.sources == ["sbir", "usas_contracts"] and m.merged_count == 2


def test_cross_source_merge_on_amount_and_date_without_award_number():
    nsf = rec("nsf", 275000, date(2022, 7, 1), "2212345")
    grant = rec("usas_grants", 275000, date(2022, 7, 15), "FAIN-XYZ-999")
    assert len(merge.merge_funding([nsf, grant])) == 1


def test_same_source_awards_with_different_numbers_stay_separate():
    a = rec("sbir", 50000, date(2021, 1, 1), "AAA-11111")
    b = rec("sbir", 50000, date(2021, 1, 1), "BBB-22222")
    assert len(merge.merge_funding([a, b])) == 2


def test_amount_disagreement_is_flagged():
    a = rec("sbir", 100000, date(2021, 1, 1), "ABC-12345")
    b = rec("usas_contracts", 180000, date(2021, 1, 5), "ABC12345")
    (m,) = merge.merge_funding([a, b])
    assert any("disagree on amount" in r for r in m.review_reasons)


def test_classification_of_usas_records():
    po = classify.classify(rec("usas_contracts", 50000, date(2024, 1, 1), raw_type="PURCHASE ORDER"))
    assert (po.funding_source, po.funding_stage) == ("Purchase Order", "Stage 6: Revenue & Commercial Traction")
    sbir_contract = classify.classify(rec("usas_contracts", 1_000_000, date(2024, 1, 1), raw_type="DEFINITIVE CONTRACT",
                                          program="SBIR PHASE II ADVANCED SENSOR"))
    assert (sbir_contract.funding_source, sbir_contract.phase) == ("SBIR", "II")
    assert sbir_contract.funding_stage == "Stage 2: Seed Funding"
    transfer = classify.classify(rec("usas_grants", 10000, date(2024, 1, 1), raw_type="PROJECT GRANT (B)",
                                     program="TECHNOLOGY TRANSFER PROGRAM"))
    assert transfer.funding_source == "Grant"


def test_evaluation_blocks_low_confidence_and_sets_post_icorps():
    good = evaluate_funding(rec("sbir", 100, date(2024, 5, 1)), cohort_date=date(2023, 1, 1))
    assert is_publishable(good) and good.post_icorps is True
    weak = rec("sbir", 100, date(2020, 5, 1))
    weak.confidence = 70
    evaluate_funding(weak, cohort_date=date(2023, 1, 1))
    assert not is_publishable(weak) and weak.post_icorps is False
    deob = evaluate_funding(rec("usas_contracts", -500, date(2024, 1, 1)), None)
    assert not is_publishable(deob)


def test_generic_names_need_corroboration():
    assert score_entity(input_name="Hale", candidate_names=["HALE LLC"]).tier == "review"
    assert score_entity(input_name="Hale", candidate_names=["HALE LLC"], input_state="MD", candidate_state="Maryland").tier in (
        "confirmed", "probable")
    assert score_entity(input_name="Acme", candidate_names=["X"], input_uei="ABC123", candidate_uei="abc-123").score == 100


def test_tedco_extracts_amount_and_fund_nearest_the_company():
    archive = TedcoArchive(cache_dir=".")
    archive.articles = {
        "u1": Article(
            url="u1", title="TEDCO Announces Builder Fund Investments", published="2024-03-05",
            text=("TEDCO announced $2,000,000 in total investments. Ayatris, Inc., located in Frederick County, "
                  "received $1,000,000 Integrated Pharma Services, located in Montgomery County, received $200,000."),
        ),
        "u2": Article(url="u2", title="Maryland Technology Development Board of Directors Meeting Notice",
                      published="2024-03-01", text="Integrated Pharma Services received $5"),
    }
    company = CompanyInput(key="integrated pharma services", name="Integrated Pharma Services")
    (found,) = archive.find_funding(company)
    assert found.amount == 200000
    assert found.program == "Builder Fund" and found.funding_source == "TEDCO"
    assert found.date_awarded == date(2024, 3, 5) and found.confidence >= 80


def test_baseline_matching_by_url_then_amount_date():
    rows = [
        {"id": "FID-1", "amount": 50000.0, "date": date(2025, 2, 26), "source": "Purchase Order", "award": None,
         "url": "https://www.usaspending.gov/award/CONT_AWD_80NSSC25PA591_8000_-NONE-_-NONE-",
         "url_key": "HTTPSWWWUSASPENDINGGOVAWARDCONTAWD80NSSC25PA5918000NONENONE", "used": False},
        {"id": "FID-2", "amount": 202623.0, "date": date(2025, 1, 20), "source": "SBIR", "award": None,
         "url": "https://www.sbir.gov/awards/215679", "url_key": "", "used": False},
    ]
    usas = rec("usas_contracts", 50000, date(2025, 2, 26), "80NSSC25PA591",
               source_url="https://www.usaspending.gov/award/CONT_AWD_80NSSC25PA591_8000_-NONE-_-NONE-")
    sbir = rec("sbir", 202623, date(2025, 1, 27), "W911-X", funding_source="SBIR")
    new = rec("nsf", 999, date(2025, 1, 1), "2299999")
    assert _match_baseline(rows, usas) == "FID-1"
    assert _match_baseline(rows, sbir) == "FID-2"
    assert _match_baseline(rows, new) is None


def test_participant_rows_collapse_per_company(tmp_path):
    path = tmp_path / "in.csv"
    path.write_text(
        "Company,UEI,DUNS,Website,Regional Start Date,Name,CorpID,EID\n"
        "Acme Robotics,,078386164.0,acme.io,4/1/2021,Jane Doe,IC-1,E-1\n"
        "ACME Robotics Inc.,KMNNMQTYNJM5,,,1/6/2020,John Roe,IC-2,E-2\n"
    )
    (company,) = load_participants([path])
    assert company.uei == "KMNNMQTYNJM5" and company.duns == "078386164"
    assert company.cohort_date == date(2020, 1, 6)
    assert company.contacts == ["Jane Doe", "John Roe"] and company.corp_ids == ["IC-1", "IC-2"]


def test_lookalike_company_names_are_not_auto_accepted():
    for a, b in [("Smart Wand", "SMART Pad LLC"), ("Environmental Sensing Solutions", "Environmental Operating Solutions, Inc"),
                 ("StormWork Tool", "TOOL, INC."), ("Flow Power", "POWER INFO LLC"),
                 ("Health Solutions Research", "3D Health Solutions, Inc."), ("Smart Wand", "SMART Pad LLC")]:
        assert score_entity(input_name=a, candidate_names=[b], input_state="MD", candidate_state="MD").tier not in (
            "confirmed", "probable"), (a, b)
    assert score_entity(input_name="N5 Sensors", candidate_names=["N5 SENSORS, INC."]).tier == "confirmed"
    assert score_entity(input_name="Kestrel", candidate_names=["Kestrel Intelligence Inc."]).tier == "review"
    assert score_entity(input_name="Kestrel", candidate_names=["Kestrel Intelligence Inc."], input_website="kestrelintelligence.com",
                        candidate_website="https://www.kestrelintelligence.com").tier == "confirmed"


def test_generic_legal_names_do_not_match_distinct_companies():
    for a, b in [("Avcentx Therapeutics", "LP Therapeutics Inc"), ("Prothymos Technologies", "V CORP. TECHNOLOGIES, INC."),
                 ("S Neuro", "T-Neuro, Inc.")]:
        assert score_entity(input_name=a, candidate_names=[b]).tier not in ("confirmed", "probable"), (a, b)
    assert score_entity(input_name="Scribe", candidate_names=["Scribe Therapeutics Inc."]).tier == "review"
    assert score_entity(input_name="Vellum Biosciences LLC", candidate_names=["VELLUM BIOSCIENCES LLC"]).tier == "confirmed"


def test_tedco_ignores_generic_names_and_program_totals():
    archive = TedcoArchive(cache_dir=".")
    archive.articles = {
        "u1": Article(url="u1", title="Phase II of the Builder Fund opens", published="2024-01-01",
                      text="TEDCO opens phase two of its fund with $5 million."),
        "u2": Article(url="u2", title="Equitech Growth Fund awards", published="2024-01-01",
                      text="Thrive Company joins a $135 million program awarded by TEDCO."),
    }
    assert archive.find_funding(CompanyInput(key="phase", name="Phase")) == []
    (thrive,) = archive.find_funding(CompanyInput(key="thrive company", name="Thrive Company"))
    assert not is_publishable(evaluate_funding(thrive, None))


def _uspto_item(app_no, title, applicant, state="MD", inventors=(), assignee=None, patent_no=None, filed="2022-03-01"):
    item = {
        "applicationNumberText": app_no,
        "applicationMetaData": {
            "inventionTitle": title, "filingDate": filed, "patentNumber": patent_no,
            "applicationStatusDescriptionText": "Patented Case" if patent_no else "Docketed New Case - Ready for Examination",
            "firstApplicantName": applicant,
            "applicantBag": [{"applicantNameText": applicant, "correspondenceAddressBag": [{"geographicRegionCode": state}]}],
            "inventorBag": [{"inventorNameText": n} for n in inventors],
            "cpcClassificationBag": ["H01L  31/0304"],
        },
    }
    if assignee:
        item["assignmentBag"] = [{"assigneeBag": [{"assigneeNameText": assignee, "assigneeAddress": {"geographicRegionCode": state}}]}]
    return item


def test_uspto_scores_company_owned_patents(monkeypatch):
    from app.services.watn_pipeline.sources import uspto

    items = [
        _uspto_item("17123456", "UV photodetector", "N5 SENSORS, INC.", patent_no="11555555"),
        _uspto_item("17999999", "Gas sensor", "N5 Sensors Inc", filed="2019-05-01"),
        _uspto_item("16000001", "Unrelated", "N5 SENSORS HOLDINGS GMBH", state="BY"),
        _uspto_item("16000002", "Univ patent", "University of Maryland", inventors=["Jane Doe"], assignee="N5 Sensors, Inc."),
    ]
    queries = []

    def fake_post(url, payload, headers=None, **kw):
        queries.append(payload["q"])
        assert headers == {"X-API-KEY": "k"}
        return {"count": len(items), "patentFileWrapperDataBag": items}

    monkeypatch.setattr(uspto.http, "post_json", fake_post)
    company = CompanyInput(key="n5 sensors", name="N5 Sensors", state="MD", contacts=["Jane Doe"],
                           cohort_date=date(2020, 1, 1))
    patents = {p.application_number: p for p in uspto.UsptoPatents("k").find(company, ["N5 SENSORS, INC."])}

    assert len(queries) == 1 and "applicantNameText" in queries[0] and "assigneeNameText" in queries[0]
    assert "inventor" not in queries[0].lower()  # company-only search
    assert patents["17123456"].tier == "confirmed" and patents["17123456"].patent_number == "11555555"
    assert patents["16000002"].owner_role == "assignee" and patents["16000002"].tier == "confirmed"
    assert "16000001" not in patents or patents["16000001"].tier not in ("confirmed", "probable")


def test_uspto_generic_name_goes_to_review_and_404_is_empty(monkeypatch):
    import requests

    from app.services.watn_pipeline.sources import uspto

    def fake_post(url, payload, headers=None, **kw):
        if "nothing" in payload["q"]:
            resp = requests.Response()
            resp.status_code = 404
            raise requests.HTTPError(response=resp)
        return {"count": 1, "patentFileWrapperDataBag": [_uspto_item("15000000", "Valve", "HALE PRODUCTS INC", state="PA")]}

    monkeypatch.setattr(uspto.http, "post_json", fake_post)
    source = uspto.UsptoPatents("k")
    assert source.find(CompanyInput(key="nothing here", name="Nothing Here")) == []
    hale = source.find(CompanyInput(key="hale", name="Hale", state="MD"))
    assert all(p.tier not in ("confirmed", "probable") for p in hale)
    assert uspto.UsptoPatents(api_key=None).available is (bool(__import__("os").getenv("USPTO_API_KEY")))
