"""Material labels are evidence-derived; a query family is never a document type."""
from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from .content import extract_announcement_text


CLASSIFIER_ID = "announcement-material-type"
CLASSIFIER_VERSION = "1.7.0"
_REPORT = r"(?:年度|半年度|中期|第一季度|第三季度|一季度|三季度|季度)(?:财务)?报告"


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]*>", "", value)))


def _kind(value: str) -> str:
    patterns = [
        ("correction_notice", r"(?:关于.{0,70})?(?:更正|补充|修正).{0,12}(?:公告|通知)"),
        ("other_announcement", r"(?:股东大会.{0,24}会议(?:资料|材料)|(?:董事会|监事会).{0,32}?(?:会议)?决议公告|议案(?:等)?|业绩快报)"),
        ("other_announcement", rf"(?:(?:董事会|独立董事).{{0,40}})?{_REPORT}的?工作(?:制度|规程)"),
        ("other_announcement", rf"(?:数据|信息)(?:来源于|源自|摘自).{{0,90}}{_REPORT}"),
        ("other_announcement", r"(?i:(?:DATA|INFORMATION)(?:HEREIN)?(?:ARE)?(?:DERIVEDFROM|SOURCEDFROM).{0,90}ANNUALREPORT)"),
        ("prospectus_appendix", r"招股说明书(?:及其)?(?:附录|附件|附表)"),
        ("prospectus_summary", r"招股说明书摘要"),
        ("prospectus", r"招股说明书"),
        ("listing_announcement", r"(?:股票)?上市公告书"),
        ("issuance_notice", r"(?:股票.{0,12}发行公告|首次公开发行.{0,25}(?:发行|中签|配售).{0,12}公告)"),
        ("periodic_summary", r"(?i:SUMMARYOF(?:THE)?ANNUALREPORT(?:19|20)\d{2}|ANNUALREPORT(?:19|20)\d{2}SUMMARY)"),
        ("periodic_report", r"(?i:ANNUALREPORT(?:19|20)\d{2}|(?:19|20)\d{2}ANNUALREPORT)"),
    ]
    matches = [(m.start(), rank, kind) for rank, (kind, pattern) in enumerate(patterns)
               if (m := re.search(pattern, value))]
    if report := re.search(_REPORT, value):
        kind = "periodic_summary" if re.match(r"[（(]?摘要", value[report.end():]) else "periodic_report"
        notice = re.search(r"(?:延期|披露|编制|说明会|提示性|审核|审议|董事会).{0,24}(?:公告|通知|意见)", value)
        # A report's standard board assurance is body text, not a notice title.
        # Notice wording must introduce the report or immediately modify its name.
        notice_suffix = re.match(r"(?:[（(]?摘要[）)]?)?(?:的)?(?:网上|网络|线上|业绩|集体){0,3}(?:延期|披露|编制|说明会|提示性|审核|审议|董事会(?:审核|审议)).{0,24}(?:公告|通知|意见)", value[report.end():])
        if (notice and notice.start() < report.start()) or notice_suffix:
            kind = "report_related_notice"
        matches.append((report.start(), len(patterns), kind))
    # Resolve an explicit meeting/resolution heading before interpreting report
    # or IPO terms in its agenda. Specialized modifiers still apply to a report
    # or financing heading, e.g. an annual-report correction or warrant listing.
    if matches and min(matches)[2] == "other_announcement":
        return "other_announcement"
    if re.search(r"(?:更正|补充|修正).{0,12}(?:公告|通知)$", value):
        return "correction_notice"
    if re.search(r"(?:可转债|可转换公司债券|公司债券|权证).{0,60}上市公告书", value):
        return "other_financing"
    if re.search(r"关于.{0,70}(?:招股说明书|上市公告书)", value):
        return "ipo_related_notice"
    return min(matches)[2] if matches else "other_announcement"


@dataclass(frozen=True)
class MaterialClassification:
    material_type: str
    title_type: str
    content_type: str | None
    evidence_status: str
    revised: bool
    language: str
    archive_priority: int
    classifier_version: str = CLASSIFIER_VERSION


def classify_material(title: str, *, text: str | None = None) -> MaterialClassification:
    """Use the actual cover/heading, not incidental references deep in the body."""
    compact_title = _compact(title)
    title_type = _kind(compact_title)
    body_type = None
    if text is not None:
        header = re.sub(r"--- page \d+ ---", "", text[:2000])
        lines = [line.strip() for line in header.splitlines() if line.strip()]
        # Legacy CNINFO HTML repeats a web-page label before its publication
        # timestamp. Keep the text artifact intact but classify the actual cover.
        if (len(lines) >= 3 and _compact(lines[0]).endswith(compact_title)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?", lines[1])):
            header = "\n".join(lines[2:])
        body_type = _kind(_compact(header)[:240])
    material_type = title_type
    status = "title_only"
    if body_type is not None:
        if title_type == body_type:
            status = "unclassified" if title_type == "other_announcement" else "title_body_agree"
        elif title_type == "other_announcement" and body_type != "other_announcement":
            material_type, status = body_type, "content_heading"
        else:
            status = "requires_review"
    language = "en" if re.search(r"英文|English", title, re.I) else "zh"
    revised = bool(re.search(r"修订|修正|更正后|更新后", compact_title))
    priority = 0 if (material_type.startswith(("periodic_", "prospectus")) or material_type in {
        "listing_announcement", "issuance_notice", "ipo_related_notice", "correction_notice",
    }) else 1
    return MaterialClassification(material_type, title_type, body_type, status, revised, language, priority)


def classify_snapshot_material(runtime: Any, snapshot_id: str, *, title: str) -> MaterialClassification:
    extraction = extract_announcement_text(runtime, snapshot_id)
    artifact = next(a for a in runtime.repository.list_derived_artifacts(snapshot_id)
                    if a.derived_artifact_id == extraction.derived_artifact_id)
    text = runtime.blob_store.read_verified_derived(artifact.archive_relative_path,
        expected_sha256=artifact.output_sha256, expected_length=artifact.output_byte_length).decode("utf-8")
    result = classify_material(title, text=text)
    parameters = {"title": title, "text_artifact_id": extraction.derived_artifact_id,
                  "text_sha256": extraction.sha256}
    output = json.dumps({**asdict(result), "input": parameters}, ensure_ascii=False,
                        sort_keys=True, separators=(",", ":")).encode("utf-8")
    for prior in runtime.repository.list_derived_artifacts(snapshot_id):
        if (prior.extractor_id == CLASSIFIER_ID and prior.extractor_version == CLASSIFIER_VERSION
                and prior.parameters == parameters):
            prior_bytes = runtime.blob_store.read_verified_derived(prior.archive_relative_path,
                expected_sha256=prior.output_sha256, expected_length=prior.output_byte_length)
            if prior_bytes != output:
                raise ValueError("material_classifier_version_conflict")
            return result
    runtime.snapshot_service.freeze_derived_artifact(parent_snapshot_id=snapshot_id,
        artifact_type="classification", extractor_id=CLASSIFIER_ID,
        extractor_version=CLASSIFIER_VERSION, parameters=parameters, output=output)
    return result
