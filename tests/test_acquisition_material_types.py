import pytest
from analysis.acquisition.materials import classify_material


@pytest.mark.parametrize("title,body,expected", [
    ("招股说明书", "样本股份有限公司\n招股说明书\n发行股票类型：人民币普通股", "prospectus"),
    ("招股说明书附录", "样本股份有限公司\n招股说明书附录\n审计报告及财务报告", "prospectus_appendix"),
    ("股票上市公告书暨2001年中期财务报告", "样本公司股票上市公告书暨2001年中期财务报告", "listing_announcement"),
    ("股票上网定价发行公告", "样本公司股票上网定价发行公告\n请参阅《招股说明书》", "issuance_notice"),
    ("招股说明书摘要", "样本公司首次公开发行股票招股说明书摘要", "prospectus_summary"),
    ("关于招股说明书的更正公告", "关于招股说明书的更正公告", "correction_notice"),
    ("可转换公司债券上市公告书", "样本公司可转换公司债券上市公告书", "other_financing"),
    ("人民币普通股股票之认沽权证上市公告书", "关于样本公司人民币普通股股票之认沽权证上市公告书", "other_financing"),
    ("2025年年度报告（修订版）", "样本公司2025年年度报告", "periodic_report"),
    ("2025年年度报告更正公告", "样本公司2025年年度报告更正公告", "correction_notice"),
    ("2025年年度报告延期披露公告", "样本公司2025年年度报告延期披露公告", "report_related_notice"),
    ("董事会决议公告", "董事会决议公告\n" + "董事会审议相关事项。" * 60 + "招股说明书", "other_announcement"),
])
def test_material_identity_uses_title_and_cover_not_incidental_references(title, body, expected):
    result = classify_material(title, text=body)
    assert result.material_type == expected
    assert result.title_type == result.content_type == expected
    assert result.evidence_status == ("unclassified" if expected == "other_announcement" else "title_body_agree")


def test_missing_body_and_conflicting_body_remain_explicit():
    assert classify_material("招股说明书附录").evidence_status == "title_only"
    result = classify_material("招股说明书", text="股票上市公告书\n上市时间")
    assert result.evidence_status == "requires_review"
    assert result.title_type == "prospectus" and result.content_type == "listing_announcement"


def test_revisions_and_summaries_keep_distinct_labels_and_priority():
    assert classify_material("2025年年度报告（修订版）").revised
    assert classify_material("2025年年度报告摘要").material_type == "periodic_summary"
    assert classify_material("2025年年度报告（修订版）").archive_priority == 0
    assert classify_material("2025年年度股东大会决议公告").archive_priority == 1


def test_legacy_web_label_and_financial_data_summary_do_not_override_actual_cover():
    result = classify_material("2001年年度报告摘要", text=(
        "贵州茅台2001年年度报告摘要\n2002-04-16 20:25\n"
        "贵州茅台酒股份有限公司2001年年度报告\n目录\n一 公司基本情况\n二 财务数据和业务数据摘要"))
    assert result.title_type == "periodic_summary" and result.content_type == "periodic_report"
    assert result.evidence_status == "requires_review"


@pytest.mark.parametrize("title,body", [
    ("贵州茅台股东大会会议资料", "贵州茅台股东大会会议资料\n议程\n审议2001年年度报告"),
    ("第一届监事会2007年度第一次会议决议公告", "第一届监事会2007年度第一次会议决议公告\n会议审议通过2006年年度报告"),
    ("董事会决议公告", "董事会决议公告\n关于招股说明书的议案"),
    ("董事会决议公告", "董事会决议公告\n审议关于招股说明书的更正公告"),
    ("董事会决议公告", "董事会决议公告\n审议公司债券上市公告书"),
])
def test_cover_notice_precedes_incidental_report_mentions(title, body):
    result = classify_material(title, text=body)
    assert result.material_type == result.title_type == result.content_type == "other_announcement"
