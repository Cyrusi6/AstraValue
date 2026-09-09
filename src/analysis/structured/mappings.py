from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from .protocols import ProtocolFamily
from .records import DataNature, PeriodKind, ValueKind, decimal_value


@dataclass(frozen=True, slots=True)
class ProtocolSupport:
    dataset_id: str
    plan_group_id: str
    protocol: ProtocolFamily
    operation: str
    upstream: str
    state: str = "executable"


def _support(
    protocol: ProtocolFamily,
    upstream: str,
    rows: Sequence[tuple[str, str, str]],
) -> tuple[ProtocolSupport, ...]:
    return tuple(
        ProtocolSupport(dataset_id, group_id, protocol, operation, upstream)
        for dataset_id, group_id, operation in rows
    )


_PROTOCOL_SUPPORT_ROWS = (
    *_support(
        ProtocolFamily.EM_F,
        "eastmoney",
        (
            ("balance_fields", "F01", "zcfzbDateAjaxNew+zcfzbAjaxNew"),
            ("income_fields", "F02", "lrbDateAjaxNew+lrbAjaxNew:reportType=1"),
            ("cashflow_fields", "F03", "xjllbDateAjaxNew+xjllbAjaxNew:reportType=1"),
            ("income_quarter", "F04", "lrbDateAjaxNew+lrbAjaxNew:reportType=2"),
            ("cashflow_quarter", "F04", "xjllbDateAjaxNew+xjllbAjaxNew:reportType=2"),
        ),
    ),
    *_support(
        ProtocolFamily.EM_M,
        "eastmoney",
        (
            ("em_metrics", "F05", "RPT_F10_FINANCE_MAINFINADATA"),
            ("market_cap", "M01", "RPT_VALUEANALYSIS_DET"),
        ),
    ),
    *_support(
        ProtocolFamily.EM_S,
        "eastmoney",
        (
            ("company_basic", "C01", "RPT_F10_ORG_BASICINFO"),
            ("segments", "C02", "RPT_F10_FN_SEGMENTSV"),
            ("customers_peer", "C03", "RPT_F10_BUSINESS_CUSTSUPP"),
            ("rd", "C04", "RPT_F10_BUSINESS_RDEXPENSE"),
            ("staff_pay", "C05", "RPT_HSF9_BASIC_STAFFCOMPOSITION"),
            ("staff_structure", "C06", "RPT_F10_STAFFCOMPETE_STRUCTURE"),
            ("subsidiaries", "C07", "RPT_F10_PUBLIC_OP_HOLDINGORG"),
            ("controller", "G01", "RPT_F10_EH_RELATION"),
            ("equity", "G02", "RPT_F10_EH_EQUITY"),
            ("holders_history", "G03", "RPT_F10_EH_HOLDERS"),
            ("float_holders_history", "G04", "RPT_F10_EH_FREEHOLDERS"),
            ("seo", "A03", "RPT_F10_DIVIDEND_SEO"),
            ("allotment", "A03", "RPT_F10_DIVIDEND_ALLOTMENT"),
            ("bond_issuance", "A04", "RPT_F10_DIVIDEND_BOND"),
            ("management_roster", "G06", "RPT_F10_ORGINFO_MANAINTRO"),
            ("management_salary", "G07", "RPT_F10_ORGINFO_SALARY"),
            ("margin", "T01", "RPT_MARGIN_STATISTICS_STOCKS"),
            ("block_trade", "T02", "RPT_DATA_BLOCKTRADE"),
            ("billboard", "T03", "RPT_BILLBOARD_DAILYDETAILS"),
            ("institution_holds", "T04", "RPT_F10_MAIN_ORGHOLDDETAILS"),
            ("fund_holds", "T05", "RPT_MAIN_ORGHOLDDETAIL"),
            ("tags", "L01", "RPT_F10_CORETHEME_BOARDTYPE"),
            ("forecasts", "P01", "RPT_HSF10_RES_PREDICTDETAIL"),
            ("capital_projects", "A07", "RPT_F10_CAPITAL_ITEM"),
            ("capital_raise", "A08", "RPT_F10_CAPITAL_RAISE"),
        ),
    ),
    *_support(
        ProtocolFamily.EM_W,
        "eastmoney",
        (
            ("holder_count", "G05", "RPT_HOLDERNUM_DET"),
            ("dividend", "A01", "RPT_SHAREBONUS_DET"),
            ("repurchase", "A02", "RPTA_WEB_GETHGLIST_NEW"),
            ("guarantee", "G09", "RPT_F10_ORGRES_GUARANTEE"),
            ("litigation", "G10", "RPT_LITIGATION_ARBITRATION_BSINFO"),
            ("violation", "G11", "RPT_HSF9_OP_VIOLATION"),
            ("goodwill", "G12", "RPT_GOODWILL_STOCKDETAILS"),
            ("pledge", "A05", "RPTA_APP_ACCUMDETAILS"),
            ("unlock_peer", "A06", "RPT_LIFT_STAGE"),
            ("management_trades", "G08", "RPT_EXECUTIVE_HOLD_DETAILS"),
            ("surveys", "T06", "RPT_ORG_SURVEY"),
            ("macro_cpi", "I01", "RPT_ECONOMY_CPI"),
            ("macro_retail", "I02", "RPT_ECONOMY_TOTAL_RETAIL"),
        ),
    ),
    *_support(
        ProtocolFamily.BAOSTOCK,
        "baostock",
        (
            ("baostock_adjust", "B08", "query_adjust_factor"),
            ("baostock_balance", "B05", "query_balance_data"),
            ("baostock_basic", "B08", "query_stock_basic"),
            ("baostock_calendar", "B08", "query_trade_dates"),
            ("baostock_cash_flow", "B06", "query_cash_flow_data"),
            ("baostock_daily", "B01", "query_history_k_data_plus"),
            ("baostock_dupont", "B07", "query_dupont_data"),
            ("baostock_growth", "B04", "query_growth_data"),
            ("baostock_operation", "B03", "query_operation_data"),
            ("baostock_profit", "B02", "query_profit_data"),
        ),
    ),
)


DATASET_PROTOCOL_SUPPORT: Mapping[str, ProtocolSupport] = {
    item.dataset_id: item for item in _PROTOCOL_SUPPORT_ROWS
}

if len(DATASET_PROTOCOL_SUPPORT) != 55:  # import-time fail closed for the approved v1 set
    raise RuntimeError("structured-data v1 protocol support must contain exactly 55 datasets")


def validate_protocol_support(
    datasets: Iterable[Mapping[str, Any]],
    *,
    support: Mapping[str, ProtocolSupport] = DATASET_PROTOCOL_SUPPORT,
) -> None:
    """Validate a registry-like mapping without importing the registry implementation."""

    entries = {str(item["dataset_id"]): item for item in datasets}
    missing = sorted(set(entries).difference(support))
    extra = sorted(set(support).difference(entries))
    if missing or extra:
        raise ValueError(f"protocol support mismatch; missing={missing}, extra={extra}")
    for dataset_id, entry in entries.items():
        configured_group = str(entry.get("plan_group_id", ""))
        if configured_group != support[dataset_id].plan_group_id:
            raise ValueError(f"plan group mismatch for {dataset_id}")
        if support[dataset_id].state != "executable":
            raise ValueError(f"dataset is not executable: {dataset_id}")


@dataclass(frozen=True, slots=True)
class FieldRule:
    dataset_id: str
    raw_field: str
    standard_field: str
    nature: DataNature
    unit: str
    period_kind: PeriodKind
    value_kind: ValueKind
    definition_id: str
    multiplier: Decimal = Decimal("1")
    scope: str = "consolidated"

    def __post_init__(self) -> None:
        if not all(
            (
                self.dataset_id,
                self.raw_field,
                self.standard_field,
                self.unit,
                self.definition_id,
                self.scope,
            )
        ):
            raise ValueError("field rules require complete identities and semantics")
        object.__setattr__(self, "multiplier", decimal_value(self.multiplier))


def _field_rules(
    dataset_id: str,
    fields: Mapping[str, tuple[str, str, PeriodKind, ValueKind]],
) -> tuple[FieldRule, ...]:
    return tuple(
        FieldRule(
            dataset_id=dataset_id,
            raw_field=raw,
            standard_field=standard,
            nature=DataNature.OBSERVED,
            unit=unit,
            period_kind=period,
            value_kind=value_kind,
            definition_id=f"structured-fields-v1:{dataset_id}:{raw}",
        )
        for raw, (standard, unit, period, value_kind) in fields.items()
    )


_INSTANT = PeriodKind.INSTANT
_CUMULATIVE = PeriodKind.CUMULATIVE
_SINGLE = PeriodKind.SINGLE_QUARTER
_TTM = PeriodKind.TTM
_QUOTE = PeriodKind.MARKET_QUOTE
_STOCK = ValueKind.STOCK
_FLOW = ValueKind.FLOW
_RATIO = ValueKind.RATIO


_BALANCE_FIELDS = {
    "MONETARYFUNDS": ("cash", "CNY", _INSTANT, _STOCK),
    "ACCOUNTS_RECE": ("accounts_receivable", "CNY", _INSTANT, _STOCK),
    "INVENTORY": ("inventory", "CNY", _INSTANT, _STOCK),
    "CONTRACT_ASSET": ("contract_assets", "CNY", _INSTANT, _STOCK),
    "CONTRACT_LIAB": ("contract_liabilities", "CNY", _INSTANT, _STOCK),
    "GOODWILL": ("goodwill", "CNY", _INSTANT, _STOCK),
    "TOTAL_ASSETS": ("total_assets", "CNY", _INSTANT, _STOCK),
    "TOTAL_LIABILITIES": ("total_liabilities", "CNY", _INSTANT, _STOCK),
    "TOTAL_EQUITY": ("total_equity", "CNY", _INSTANT, _STOCK),
    "TOTAL_PARENT_EQUITY": ("total_parent_equity", "CNY", _INSTANT, _STOCK),
    "TOTAL_CURRENT_ASSETS": ("current_assets", "CNY", _INSTANT, _STOCK),
    "TOTAL_CURRENT_LIAB": ("current_liabilities", "CNY", _INSTANT, _STOCK),
    "SHORT_LOAN": ("short_term_loans", "CNY", _INSTANT, _STOCK),
    "LONG_LOAN": ("long_term_loans", "CNY", _INSTANT, _STOCK),
    "BOND_PAYABLE": ("bonds_payable", "CNY", _INSTANT, _STOCK),
    "LEASE_LIAB": ("lease_liabilities", "CNY", _INSTANT, _STOCK),
    "CIP": ("construction_in_progress", "CNY", _INSTANT, _STOCK),
    "PREPAYMENT": ("prepayments", "CNY", _INSTANT, _STOCK),
    "ACCOUNTS_PAYABLE": ("accounts_payable", "CNY", _INSTANT, _STOCK),
    "SHARE_CAPITAL": ("share_capital", "shares", _INSTANT, _STOCK),
}

_INCOME_FIELDS = {
    "TOTAL_OPERATE_INCOME": ("total_operating_income", "CNY", _CUMULATIVE, _FLOW),
    "OPERATE_INCOME": ("operating_income", "CNY", _CUMULATIVE, _FLOW),
    "OPERATE_COST": ("operating_cost", "CNY", _CUMULATIVE, _FLOW),
    "OPERATE_PROFIT": ("operating_profit", "CNY", _CUMULATIVE, _FLOW),
    "NETPROFIT": ("net_profit", "CNY", _CUMULATIVE, _FLOW),
    "PARENT_NETPROFIT": ("parent_net_profit", "CNY", _CUMULATIVE, _FLOW),
    "DEDUCT_PARENT_NETPROFIT": ("deducted_parent_net_profit", "CNY", _CUMULATIVE, _FLOW),
    "SALE_EXPENSE": ("selling_expense", "CNY", _CUMULATIVE, _FLOW),
    "MANAGE_EXPENSE": ("administrative_expense", "CNY", _CUMULATIVE, _FLOW),
    "RESEARCH_EXPENSE": ("research_expense", "CNY", _CUMULATIVE, _FLOW),
    "FINANCE_EXPENSE": ("finance_expense", "CNY", _CUMULATIVE, _FLOW),
    "INCOME_TAX": ("income_tax", "CNY", _CUMULATIVE, _FLOW),
    "TOTAL_PROFIT": ("total_profit", "CNY", _CUMULATIVE, _FLOW),
    "ASSET_IMPAIRMENT_LOSS": ("asset_impairment_loss", "CNY", _CUMULATIVE, _FLOW),
    "CREDIT_IMPAIRMENT_LOSS": ("credit_impairment_loss", "CNY", _CUMULATIVE, _FLOW),
    "BASIC_EPS": ("basic_eps", "CNY_per_share", _CUMULATIVE, _RATIO),
    "DILUTED_EPS": ("diluted_eps", "CNY_per_share", _CUMULATIVE, _RATIO),
}

_CASHFLOW_FIELDS = {
    "NETCASH_OPERATE": ("operating_cash_flow", "CNY", _CUMULATIVE, _FLOW),
    "NETCASH_INVEST": ("investing_cash_flow", "CNY", _CUMULATIVE, _FLOW),
    "NETCASH_FINANCE": ("financing_cash_flow", "CNY", _CUMULATIVE, _FLOW),
    "END_CASH_EQUIVALENTS": ("ending_cash_equivalents", "CNY", _INSTANT, _STOCK),
    "BEGIN_CASH_EQUIVALENTS": ("beginning_cash_equivalents", "CNY", _INSTANT, _STOCK),
    "CONSTRUCT_LONG_ASSET": ("long_asset_cash_purchase", "CNY", _CUMULATIVE, _FLOW),
    "FA_IR_DEPR": ("fixed_and_investment_property_depreciation", "CNY", _CUMULATIVE, _FLOW),
    "IA_AMORTIZE": ("intangible_amortization", "CNY", _CUMULATIVE, _FLOW),
}


_ALL_FIELD_RULES = (
    *_field_rules("balance_fields", _BALANCE_FIELDS),
    *_field_rules("income_fields", _INCOME_FIELDS),
    *_field_rules("cashflow_fields", _CASHFLOW_FIELDS),
    *_field_rules(
        "income_quarter",
        {key: (value[0], value[1], _SINGLE, value[3]) for key, value in _INCOME_FIELDS.items()},
    ),
    *_field_rules(
        "cashflow_quarter",
        {key: (value[0], value[1], _SINGLE if value[3] is _FLOW else _INSTANT, value[3]) for key, value in _CASHFLOW_FIELDS.items()},
    ),
    *_field_rules("em_metrics", {"ROIC": ("roic", "ratio", _CUMULATIVE, _RATIO)}),
    *_field_rules(
        "baostock_profit",
        {
            "roeAvg": ("roe_average", "ratio", _CUMULATIVE, _RATIO),
            "npMargin": ("net_profit_margin", "ratio", _CUMULATIVE, _RATIO),
            "gpMargin": ("gross_profit_margin", "ratio", _CUMULATIVE, _RATIO),
            "epsTTM": ("eps_ttm", "CNY_per_share", _TTM, _RATIO),
        },
    ),
    *_field_rules(
        "baostock_operation",
        {
            "NRTurnRatio": ("receivables_turnover", "ratio", _CUMULATIVE, _RATIO),
            "NRTurnDays": ("receivables_turnover_days", "days", _CUMULATIVE, _RATIO),
            "INVTurnRatio": ("inventory_turnover", "ratio", _CUMULATIVE, _RATIO),
            "INVTurnDays": ("inventory_turnover_days", "days", _CUMULATIVE, _RATIO),
            "CATurnRatio": ("current_asset_turnover", "ratio", _CUMULATIVE, _RATIO),
            "AssetTurnRatio": ("asset_turnover", "ratio", _CUMULATIVE, _RATIO),
        },
    ),
    *_field_rules(
        "baostock_growth",
        {
            "YOYEquity": ("equity_growth_yoy", "ratio", _CUMULATIVE, _RATIO),
            "YOYAsset": ("asset_growth_yoy", "ratio", _CUMULATIVE, _RATIO),
            "YOYNI": ("net_profit_growth_yoy", "ratio", _CUMULATIVE, _RATIO),
            "YOYEPSBasic": ("basic_eps_growth_yoy", "ratio", _CUMULATIVE, _RATIO),
            "YOYPNI": ("parent_net_profit_growth_yoy", "ratio", _CUMULATIVE, _RATIO),
        },
    ),
    *_field_rules(
        "baostock_balance",
        {
            "currentRatio": ("current_ratio", "ratio", _INSTANT, _RATIO),
            "quickRatio": ("quick_ratio", "ratio", _INSTANT, _RATIO),
            "cashRatio": ("cash_ratio", "ratio", _INSTANT, _RATIO),
            "YOYLiability": ("liability_growth_yoy", "ratio", _INSTANT, _RATIO),
            "liabilityToAsset": ("liability_to_asset", "ratio", _INSTANT, _RATIO),
            "assetToEquity": ("asset_to_equity", "ratio", _INSTANT, _RATIO),
        },
    ),
    *_field_rules(
        "baostock_cash_flow",
        {
            "CAToAsset": ("current_assets_to_assets", "ratio", _INSTANT, _RATIO),
            "NCAToAsset": ("noncurrent_assets_to_assets", "ratio", _INSTANT, _RATIO),
            "tangibleAssetToAsset": ("tangible_assets_to_assets", "ratio", _INSTANT, _RATIO),
            "ebitToInterest": ("ebit_interest_coverage", "ratio", _CUMULATIVE, _RATIO),
            "CFOToOR": ("operating_cashflow_to_revenue", "ratio", _CUMULATIVE, _RATIO),
            "CFOToNP": ("operating_cashflow_to_net_profit", "ratio", _CUMULATIVE, _RATIO),
            "CFOToGr": ("operating_cashflow_to_gross_revenue", "ratio", _CUMULATIVE, _RATIO),
        },
    ),
    *_field_rules(
        "baostock_dupont",
        {
            "dupontROE": ("dupont_roe", "ratio", _CUMULATIVE, _RATIO),
            "dupontAssetStoEquity": ("dupont_asset_to_equity", "ratio", _CUMULATIVE, _RATIO),
            "dupontAssetTurn": ("dupont_asset_turnover", "ratio", _CUMULATIVE, _RATIO),
            "dupontPnitoni": ("dupont_parent_profit_to_net_profit", "ratio", _CUMULATIVE, _RATIO),
            "dupontNitogr": ("dupont_net_profit_to_revenue", "ratio", _CUMULATIVE, _RATIO),
            "dupontTaxBurden": ("dupont_tax_burden", "ratio", _CUMULATIVE, _RATIO),
            "dupontIntburden": ("dupont_interest_burden", "ratio", _CUMULATIVE, _RATIO),
            "dupontEbittogr": ("dupont_ebit_to_revenue", "ratio", _CUMULATIVE, _RATIO),
        },
    ),
    *_field_rules(
        "baostock_daily",
        {
            "open": ("market_open", "CNY", _QUOTE, _STOCK),
            "high": ("market_high", "CNY", _QUOTE, _STOCK),
            "low": ("market_low", "CNY", _QUOTE, _STOCK),
            "close": ("market_close", "CNY", _QUOTE, _STOCK),
            "preclose": ("market_previous_close", "CNY", _QUOTE, _STOCK),
            "volume": ("market_volume", "shares", _QUOTE, _FLOW),
            "amount": ("market_amount", "CNY", _QUOTE, _FLOW),
            "turn": ("turnover_rate", "percent", _QUOTE, _RATIO),
            "pctChg": ("price_change_rate", "percent", _QUOTE, _RATIO),
            "peTTM": ("pe_ttm", "multiple", _QUOTE, _RATIO),
            "pbMRQ": ("pb_mrq", "multiple", _QUOTE, _RATIO),
            "psTTM": ("ps_ttm", "multiple", _QUOTE, _RATIO),
            "pcfNcfTTM": ("pcf_net_cashflow_ttm", "multiple", _QUOTE, _RATIO),
        },
    ),
    *_field_rules(
        "market_cap",
        {
            "TOTAL_MARKET_CAP": ("total_market_cap", "CNY", _QUOTE, _STOCK),
            "NOTLIMITED_MARKETCAP_A": ("float_market_cap", "CNY", _QUOTE, _STOCK),
            "CLOSE_PRICE": ("market_close", "CNY", _QUOTE, _STOCK),
            "TOTAL_SHARES": ("total_shares", "shares", _QUOTE, _STOCK),
            "FREE_SHARES_A": ("free_float_shares", "shares", _QUOTE, _STOCK),
            "PE_TTM": ("pe_ttm", "multiple", _QUOTE, _RATIO),
            "PB_MRQ": ("pb_mrq", "multiple", _QUOTE, _RATIO),
            "PS_TTM": ("ps_ttm", "multiple", _QUOTE, _RATIO),
            "PCF_OCF_TTM": ("pcf_net_cashflow_ttm", "multiple", _QUOTE, _RATIO),
        },
    ),
    *_field_rules(
        "equity",
        {
            "TOTAL_SHARES": ("total_shares", "shares", _INSTANT, _STOCK),
            "LIMITED_SHARES": ("limited_shares", "shares", _INSTANT, _STOCK),
            "UNLIMITED_SHARES": ("unlimited_shares", "shares", _INSTANT, _STOCK),
            "LISTED_A_SHARES": ("listed_a_shares", "shares", _INSTANT, _STOCK),
        },
    ),
    *_field_rules(
        "goodwill",
        {
            "GOODWILL": ("goodwill_balance", "CNY", _INSTANT, _STOCK),
            "GOODWILL_CHANGE": ("goodwill_impairment", "CNY", _CUMULATIVE, _FLOW),
        },
    ),
    *_field_rules(
        "segments",
        {
            "MAIN_BUSINESS_INCOME": ("segment_revenue", "CNY", _CUMULATIVE, _FLOW),
            "MAIN_BUSINESS_COST": ("segment_cost", "CNY", _CUMULATIVE, _FLOW),
            "MAIN_BUSINESS_RPOFIT": ("segment_profit", "CNY", _CUMULATIVE, _FLOW),
            "MBI_RATIO": ("segment_revenue_ratio", "ratio", _CUMULATIVE, _RATIO),
            "GROSS_RPOFIT_RATIO": ("segment_gross_margin", "ratio", _CUMULATIVE, _RATIO),
        },
    ),
    FieldRule(
        dataset_id="baostock_daily",
        raw_field="turn",
        standard_field="turnover_rate",
        nature=DataNature.OBSERVED,
        unit="ratio",
        period_kind=_QUOTE,
        value_kind=_RATIO,
        definition_id="structured-fields-v1:baostock_daily:turn",
        multiplier=Decimal("0.01"),
        scope="security",
    ),
    FieldRule(
        dataset_id="baostock_daily",
        raw_field="pctChg",
        standard_field="price_change_rate",
        nature=DataNature.OBSERVED,
        unit="ratio",
        period_kind=_QUOTE,
        value_kind=_RATIO,
        definition_id="structured-fields-v1:baostock_daily:pctChg",
        multiplier=Decimal("0.01"),
        scope="security",
    ),
    FieldRule(
        dataset_id="dividend",
        raw_field="PRETAX_BONUS_RMB",
        standard_field="cash_dividend_per_share_plan",
        nature=DataNature.ANNOUNCED_PLAN,
        unit="CNY_per_share",
        period_kind=_CUMULATIVE,
        value_kind=_FLOW,
        definition_id="structured-fields-v1:dividend:PRETAX_BONUS_RMB",
        multiplier=Decimal("0.1"),
        scope="security",
    ),
    FieldRule(
        dataset_id="float_holders_history",
        raw_field="FREE_HOLDNUM_RATIO",
        standard_field="free_float_holder_ratio",
        nature=DataNature.OBSERVED,
        unit="ratio",
        period_kind=_INSTANT,
        value_kind=_RATIO,
        definition_id="structured-fields-v1:float_holders_history:FREE_HOLDNUM_RATIO",
        multiplier=Decimal("0.01"),
        scope="security",
    ),
)


FIELD_RULES: Mapping[tuple[str, str], FieldRule] = {
    (item.dataset_id, item.raw_field): item for item in _ALL_FIELD_RULES
}


_DEFAULT_NATURES: Mapping[str, DataNature] = {
    dataset_id: DataNature.OBSERVED for dataset_id in DATASET_PROTOCOL_SUPPORT
} | {
    "tags": DataNature.PLATFORM_LABEL,
    "forecasts": DataNature.FORECAST,
    "surveys": DataNature.SOURCE_TEXT,
}


FIELD_NATURE_OVERRIDES: Mapping[tuple[str, str], DataNature] = {
    ("company_basic", "ORG_PROFILE"): DataNature.SOURCE_TEXT,
    ("company_basic", "ORG_PROFIE"): DataNature.SOURCE_TEXT,
    ("company_basic", "MAIN_BUSINESS"): DataNature.SOURCE_TEXT,
    ("company_basic", "BUSINESS_SCOPE"): DataNature.SOURCE_TEXT,
    ("company_basic", "BLGAINIAN"): DataNature.PLATFORM_LABEL,
    ("company_basic", "BLGAINIAN_CODE"): DataNature.PLATFORM_LABEL,
    ("pledge", "WARNING_LINE"): DataNature.PROVIDER_ESTIMATE,
    ("pledge", "OPENLINE"): DataNature.PROVIDER_ESTIMATE,
    ("pledge", "WARNING_STATE"): DataNature.PROVIDER_ESTIMATE,
    ("pledge", "IS_DOUBT"): DataNature.PROVIDER_ESTIMATE,
    ("capital_projects", "YIELD"): DataNature.FORECAST,
    ("capital_projects", "INVEST_RECOVERY_PERIOD"): DataNature.FORECAST,
    ("capital_projects", "PLAN_INVEST_AMT"): DataNature.ANNOUNCED_PLAN,
    ("capital_projects", "ACTUAL_INPUT_RF"): DataNature.OBSERVED,
    ("dividend", "PRETAX_BONUS_RMB"): DataNature.ANNOUNCED_PLAN,
    ("dividend", "BONUS_RATIO"): DataNature.ANNOUNCED_PLAN,
    ("dividend", "IT_RATIO"): DataNature.ANNOUNCED_PLAN,
    ("repurchase", "REPURAMOUNTLIMIT"): DataNature.ANNOUNCED_PLAN,
    ("repurchase", "REPURNUMCAP"): DataNature.ANNOUNCED_PLAN,
    ("repurchase", "REPURAMOUNT"): DataNature.OBSERVED,
    ("repurchase", "REPURNUM"): DataNature.OBSERVED,
    ("management_roster", "RESUME"): DataNature.SOURCE_TEXT,
    ("surveys", "CONTENT"): DataNature.SOURCE_TEXT,
    ("surveys", "REMARK"): DataNature.SOURCE_TEXT,
    ("staff_pay", "AVG_SALARY"): DataNature.PROVIDER_ESTIMATE,
    ("staff_pay", "AVG_SALARY_NEW"): DataNature.PROVIDER_ESTIMATE,
    ("float_holders_history", "CAMRELATION_GROUP_LABEL"): DataNature.PLATFORM_LABEL,
    ("float_holders_history", "HOLDER_RELATION_LABLE"): DataNature.PLATFORM_LABEL,
}


@dataclass(frozen=True, slots=True)
class MappedField:
    dataset_id: str
    raw_field: str
    raw_value: Any
    nature: DataNature
    standard_field: str | None
    unit: str | None
    period_kind: PeriodKind | None
    value_kind: ValueKind | None
    definition_id: str | None
    multiplier: Decimal | None
    formula_eligible: bool

    @property
    def normalized_numeric_value(self) -> Decimal | None:
        if self.raw_value is None or self.multiplier is None:
            return None
        return decimal_value(self.raw_value) * self.multiplier


def classify_field(
    dataset_id: str,
    raw_field: str,
    *,
    known_fields: Iterable[str] | None = None,
) -> DataNature:
    if known_fields is not None and raw_field not in set(known_fields):
        return DataNature.UNCLASSIFIED
    return FIELD_NATURE_OVERRIDES.get(
        (dataset_id, raw_field),
        _DEFAULT_NATURES.get(dataset_id, DataNature.UNCLASSIFIED),
    )


def map_row(
    dataset_id: str,
    row: Mapping[str, Any],
    *,
    known_fields: Iterable[str] | None = None,
    field_rules: Mapping[tuple[str, str], FieldRule] = FIELD_RULES,
) -> tuple[MappedField, ...]:
    """Preserve every raw field while admitting only confirmed standard mappings."""

    known = None if known_fields is None else frozenset(known_fields)
    mapped: list[MappedField] = []
    for raw_field, value in row.items():
        nature = classify_field(dataset_id, raw_field, known_fields=known)
        rule = field_rules.get((dataset_id, raw_field))
        if nature is DataNature.UNCLASSIFIED:
            rule = None
        mapped.append(
            MappedField(
                dataset_id=dataset_id,
                raw_field=raw_field,
                raw_value=value,
                nature=nature,
                standard_field=rule.standard_field if rule else None,
                unit=rule.unit if rule else None,
                period_kind=rule.period_kind if rule else None,
                value_kind=rule.value_kind if rule else None,
                definition_id=rule.definition_id if rule else None,
                multiplier=rule.multiplier if rule else None,
                formula_eligible=(
                    rule is not None
                    and value is not None
                    and nature in {DataNature.OBSERVED, DataNature.DETERMINISTIC}
                ),
            )
        )
    return tuple(mapped)


def aggregation_level(dataset_id: str) -> str:
    return {
        "institution_holds": "institution_summary",
        "fund_holds": "fund_detail",
        "segments": "hierarchical_segment",
        "forecasts": "forecast_opinion",
        "tags": "platform_label",
    }.get(dataset_id, "record")


def _nonempty(row: Mapping[str, Any], names: Sequence[str]) -> tuple[str | None, str | None]:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return name, str(value).strip()
    return None, None


_ANONYMOUS_COUNTERPARTY = re.compile(
    r"^(?:第?[一二三四五六七八九十\d]+(?:名|大)?(?:客户|供应商)|(?:客户|供应商)[一二三四五六七八九十\d]+)$"
)


@dataclass(frozen=True, slots=True)
class EntityReference:
    name: str | None
    source_code: str | None
    identity_key: str | None
    resolution: str


def entity_reference(
    row: Mapping[str, Any],
    *,
    code_fields: Sequence[str],
    name_fields: Sequence[str],
    anonymous_names: bool = False,
) -> EntityReference:
    """Keep coded identities distinct and never merge name-only subjects."""

    _, code = _nonempty(row, code_fields)
    _, name = _nonempty(row, name_fields)
    anonymous = bool(
        anonymous_names
        and (
            name is None
            or name in {"匿名", "未披露", "未公开", "-", "--"}
            or bool(_ANONYMOUS_COUNTERPARTY.fullmatch(name))
        )
    )
    if code:
        return EntityReference(name, code, f"source-code:{code}", "source_code")
    if anonymous:
        return EntityReference(name, None, None, "anonymous")
    if name:
        return EntityReference(name, None, None, "name_only")
    return EntityReference(None, None, None, "missing")


_DIMENSION_FIELDS: Mapping[str, tuple[str, ...]] = {
    "company_basic": ("SECUCODE", "ORG_CODE"),
    "segments": (
        "SECUCODE",
        "REPORT_DATE",
        "MAINOP_TYPE",
        "ITEM_CODE",
        "ITEM_PARENT_CODE",
        "ITEM_LEVEL",
    ),
    "customers_peer": ("SECUCODE", "REPORT_DATE", "TYPE", "TYPE_CODE", "RANK"),
    "rd": ("SECUCODE", "REPORT_DATE", "REPORT_TYPE_CODE"),
    "staff_pay": ("SECUCODE", "REPORT_DATE", "PAYYEAR"),
    "staff_structure": (
        "SECUCODE",
        "REPORT_DATE",
        "DISTRIBUTION_TYPE",
        "DISTRIBUTION_NAME",
        "RANK_NUM",
    ),
    "subsidiaries": ("SECUCODE", "REPORT_DATE", "ORG_CODE", "HOLD_ORG_NAME"),
}


def record_dimensions(dataset_id: str, row: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Return stable business dimensions without treating dimensions as additive rows."""

    return tuple(
        (name, str(row[name]).strip())
        for name in _DIMENSION_FIELDS.get(dataset_id, ())
        if row.get(name) is not None and str(row[name]).strip()
    )


@dataclass(frozen=True, slots=True)
class CounterpartyRecord:
    counterparty: EntityReference
    counterparty_type: str | None
    rank: str | None
    amount: Any
    total_amount: Any
    source_ratio: Any
    dimensions: tuple[tuple[str, str], ...]


def map_counterparty_record(row: Mapping[str, Any]) -> CounterpartyRecord:
    """Map C03 without inventing identities or silently summing dimensions."""

    _, counterparty_type = _nonempty(row, ("TYPE", "TYPE_CODE"))
    _, rank = _nonempty(row, ("RANK",))
    return CounterpartyRecord(
        counterparty=entity_reference(
            row,
            code_fields=("ITEM_CODE", "COUNTERPARTY_CODE"),
            name_fields=("ITEM_NAME",),
            anonymous_names=True,
        ),
        counterparty_type=counterparty_type,
        rank=rank,
        amount=row.get("AMOUNT"),
        total_amount=row.get("SUM_AMOUNT"),
        source_ratio=row.get("TOI_RATIO"),
        dimensions=record_dimensions("customers_peer", row),
    )


_DATE_FIELD_ROLES: Mapping[str, Mapping[str, str]] = {
    "management_roster": {
        "INCUMBENT_DATE": "incumbent_start",
        "INCUMBENT_TIME": "incumbent_source_period",
        "REPORT_DATE": "report_date",
    },
    "management_trades": {"CHANGE_DATE": "event_date"},
    "guarantee": {
        "GUARANTEE_START_DATE": "effective_start",
        "GUARANTEE_END_DATE": "effective_end",
        "NOTICE_DATE": "notice_date",
        "TRADE_DATE": "event_date",
    },
    "litigation": {
        "FI_PROSECUTE_DATE": "filing_date",
        "FI_JUDGMENT_DATE": "judgment_date",
        "NOTICE_DATE": "notice_date",
    },
    "violation": {"NOTICE_DATE": "notice_date"},
    "goodwill": {"REPORT_DATE": "report_date", "NOTICE_DATE": "notice_date"},
}


def record_dates(dataset_id: str, row: Mapping[str, Any]) -> Mapping[str, Any]:
    """Preserve distinct report, effective, event and notice dates."""

    roles = _DATE_FIELD_ROLES.get(dataset_id, {})
    return {
        role: row[field_name]
        for field_name, role in roles.items()
        if row.get(field_name) not in {None, ""}
    }


def holding_ratio_basis(dataset_id: str, raw_field: str) -> str:
    """Name the denominator boundary; unknown bases remain explicitly unconfirmed."""

    return {
        ("holders_history", "HOLD_NUM_RATIO"): "total_shares_unconfirmed",
        ("float_holders_history", "FREE_HOLDNUM_RATIO"): "free_float_shares",
        ("float_holders_history", "HOLD_RATIO"): "total_shares_unconfirmed",
        ("pledge", "PF_HOLD_RATIO"): "holder_owned_shares_unconfirmed",
        ("pledge", "PF_TSR"): "company_total_shares_unconfirmed",
    }.get((dataset_id, raw_field), "not_a_registered_ratio")


@dataclass(frozen=True, slots=True)
class LifecycleState:
    dataset_id: str
    source_value: str | None
    phase: str


_LIFECYCLE_FIELD: Mapping[str, str] = {
    "dividend": "ASSIGN_PROGRESS",
    "repurchase": "REPURPROGRESS",
    "seo": "LISTING_STATE",
    "bond_issuance": "LISTING_STATE",
    "pledge": "UNFREEZE_STATE",
}


def lifecycle_state(dataset_id: str, row: Mapping[str, Any]) -> LifecycleState:
    """Classify lifecycle for display while retaining the exact supplier value."""

    field_name = _LIFECYCLE_FIELD.get(dataset_id)
    source_value = None if field_name is None else str(row.get(field_name) or "").strip() or None
    normalized = (source_value or "").lower()
    if any(token in normalized for token in ("取消", "终止", "停止", "cancel", "terminate")):
        phase = "terminated"
    elif any(token in normalized for token in ("完成", "实施完毕", "已解押", "complete", "finished")):
        phase = "completed"
    elif any(token in normalized for token in ("实施中", "进行中", "回购中", "in progress")):
        phase = "in_progress"
    elif any(token in normalized for token in ("通过", "批准", "approved")):
        phase = "approved"
    elif any(token in normalized for token in ("预案", "计划", "proposal", "planned")):
        phase = "planned"
    else:
        phase = "unknown"
    return LifecycleState(dataset_id, source_value, phase)


def holding_record_key(dataset_id: str, row: Mapping[str, Any]) -> tuple[str, ...]:
    """Keep institution summaries and fund details in separate identity spaces."""

    level = aggregation_level(dataset_id)
    if dataset_id == "institution_holds":
        fields = ("SECUCODE", "REPORT_DATE", "ORG_TYPE")
    elif dataset_id == "fund_holds":
        fields = ("SECUCODE", "REPORT_DATE", "FUND_CODE", "HOLDER_CODE")
    else:
        raise ValueError("holding identity is only defined for registered holding datasets")
    values = [level]
    for name in fields:
        value = row.get(name)
        if value is not None and str(value).strip():
            values.append(f"{name}={str(value).strip()}")
    if len(values) == 1:
        raise ValueError("holding record is missing its business identity")
    return tuple(values)


@dataclass(frozen=True, slots=True)
class MappingGap:
    gap_id: str
    affected_groups: tuple[str, ...]
    reason: str
    next_path: str


KNOWN_MAPPING_GAPS: Mapping[str, MappingGap] = {
    "complete_management_tenure": MappingGap(
        "complete_management_tenure",
        ("G06", "RD07"),
        "任职摘要不能证明全部历史离任日期",
        "定向读取治理章节并保留完整任离职链",
    ),
    "comprehensive_related_transactions": MappingGap(
        "comprehensive_related_transactions",
        ("G09", "RD07"),
        "IS_RELATED_TRADE 仅表示关联担保，不是全面关联交易",
        "定向读取关联交易章节",
    ),
    "parent_company_statements": MappingGap(
        "parent_company_statements",
        ("F01", "F02", "F03", "RD05"),
        "默认三表范围不能同时冒充母公司单体范围",
        "取得带明确表头和范围的母公司报表",
    ),
    "bond_lifecycle": MappingGap(
        "bond_lifecycle",
        ("A04", "RD06", "RD08"),
        "发行记录不包含当前余额、实际转股和赎回全生命周期",
        "定向读取债券状态材料",
    ),
    "operating_quantity_and_channel": MappingGap(
        "operating_quantity_and_channel",
        ("C02", "C03", "RD02", "RD03"),
        "收入、社零或平台标签不能替代产销量、渠道库存和终端价格",
        "使用明确单位和期间的经营章节证据或新注册免费来源",
    ),
}


def select_leaf_segments(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """Return non-overlapping leaves for aggregation; original rows remain untouched."""

    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        group = (
            str(row.get("SECUCODE", "")),
            str(row.get("REPORT_DATE", "")),
            str(row.get("MAINOP_TYPE", "")),
        )
        grouped.setdefault(group, []).append(row)
    leaves: list[Mapping[str, Any]] = []
    for group_rows in grouped.values():
        parent_codes = {
            str(row.get("ITEM_PARENT_CODE"))
            for row in group_rows
            if row.get("ITEM_PARENT_CODE") not in {None, ""}
        }
        seen: set[tuple[str, str, str]] = set()
        for row in group_rows:
            item_code = str(row.get("ITEM_CODE", ""))
            identity = (
                item_code,
                str(row.get("ACTUAL_ITEM_NAME", row.get("ITEM_NAME", ""))),
                str(row.get("ITEM_LEVEL", "")),
            )
            if identity in seen:
                continue
            seen.add(identity)
            if item_code and item_code in parent_codes:
                continue
            leaves.append(row)
    return tuple(leaves)


@dataclass(frozen=True, slots=True)
class FallbackSpec:
    adapter_id: str
    wrapper: str
    upstream: str
    allowed_params: frozenset[str]
    field_map: Mapping[str, str]
    compatible_fields: frozenset[str]
    validation_state: str


FALLBACK_SPECS: Mapping[str, FallbackSpec] = {
    "eastmoney_market_history": FallbackSpec(
        adapter_id="eastmoney_market_history",
        wrapper="stock_zh_a_hist",
        upstream="push2his.eastmoney.com",
        allowed_params=frozenset({"symbol", "period", "start_date", "end_date", "adjust", "timeout"}),
        field_map={
            "日期": "quote_date",
            "股票代码": "security_code",
            "开盘": "market_open",
            "收盘": "market_close",
            "最高": "market_high",
            "最低": "market_low",
            "成交额": "market_amount",
            "涨跌幅": "price_change_rate",
            "换手率": "turnover_rate",
        },
        compatible_fields=frozenset(
            {"quote_date", "security_code", "market_open", "market_close", "market_high", "market_low", "market_amount", "price_change_rate", "turnover_rate"}
        ),
        validation_state="trigger_sample_required",
    ),
    "sina_financial_statements": FallbackSpec(
        adapter_id="sina_financial_statements",
        wrapper="stock_financial_report_sina",
        upstream="vip.stock.finance.sina.com.cn",
        allowed_params=frozenset({"stock", "symbol"}),
        field_map={
            "报告日": "report_date",
            "货币资金": "cash",
            "应收账款": "accounts_receivable",
            "存货": "inventory",
            "商誉": "goodwill",
            "资产总计": "total_assets",
            "负债合计": "total_liabilities",
            "营业收入": "operating_income",
            "营业成本": "operating_cost",
            "净利润": "net_profit",
            "归属于母公司所有者的净利润": "parent_net_profit",
            "经营活动产生的现金流量净额": "operating_cash_flow",
            "投资活动产生的现金流量净额": "investing_cash_flow",
            "筹资活动产生的现金流量净额": "financing_cash_flow",
        },
        compatible_fields=frozenset(
            {"report_date", "cash", "accounts_receivable", "inventory", "goodwill", "total_assets", "total_liabilities", "operating_income", "operating_cost", "net_profit", "parent_net_profit", "operating_cash_flow", "investing_cash_flow", "financing_cash_flow"}
        ),
        validation_state="trigger_sample_required",
    ),
    "eastmoney_same_meaning_metrics": FallbackSpec(
        adapter_id="eastmoney_same_meaning_metrics",
        wrapper="EM-M:RPT_F10_FINANCE_MAINFINADATA",
        upstream="datacenter.eastmoney.com",
        allowed_params=frozenset({"type", "sty", "filter", "p", "ps", "sr", "st", "source", "client"}),
        field_map={"ROIC": "roic", "ROEJQ": "weighted_roe_candidate", "XSMLL": "gross_margin_candidate"},
        compatible_fields=frozenset({"roic"}),
        validation_state="field_definition_required",
    ),
}


@dataclass(frozen=True, slots=True)
class FallbackBatch:
    adapter_id: str
    rows: tuple[Mapping[str, Any], ...]
    values: Mapping[str, tuple[Any, ...]]
    gaps: Mapping[str, str]


def _records_from_result(result: Any) -> tuple[Mapping[str, Any], ...]:
    if result is None:
        return ()
    if hasattr(result, "to_dict"):
        records = result.to_dict(orient="records")
    else:
        records = result
    if isinstance(records, Mapping):
        records = [records]
    if not isinstance(records, Iterable) or isinstance(records, (str, bytes)):
        raise ValueError("fallback wrapper returned an unsupported result")
    normalized = tuple(dict(row) for row in records)
    if any(not isinstance(row, Mapping) for row in normalized):
        raise ValueError("fallback rows must be mappings")
    return normalized


def execute_fallback(
    adapter_id: str,
    caller: Callable[..., Any],
    *,
    params: Mapping[str, Any],
    requested_fields: Iterable[str],
    validated_fields: Iterable[str] = (),
) -> FallbackBatch:
    """Execute an injected wrapper only for requested, definition-compatible gaps."""

    spec = FALLBACK_SPECS[adapter_id]
    unknown_params = sorted(set(params).difference(spec.allowed_params))
    if unknown_params:
        raise ValueError(f"unsupported fallback parameters: {', '.join(unknown_params)}")
    requested = frozenset(requested_fields)
    validated = frozenset(validated_fields)
    gaps: dict[str, str] = {}
    usable = set()
    for field_id in requested:
        if field_id not in spec.compatible_fields:
            gaps[field_id] = "definition_or_period_mismatch"
        elif spec.validation_state != "validated" and field_id not in validated:
            gaps[field_id] = "trigger_sample_not_validated"
        else:
            usable.add(field_id)
    if not usable:
        return FallbackBatch(adapter_id, (), {}, gaps)
    rows = _records_from_result(caller(**params))
    values: dict[str, list[Any]] = {field_id: [] for field_id in usable}
    for row in rows:
        for raw_name, standard_name in spec.field_map.items():
            if standard_name in usable and raw_name in row and row[raw_name] is not None:
                values[standard_name].append(row[raw_name])
    for field_id in usable:
        if not values[field_id]:
            gaps[field_id] = "fallback_field_missing"
    return FallbackBatch(
        adapter_id=adapter_id,
        rows=rows,
        values={key: tuple(value) for key, value in values.items() if value},
        gaps=gaps,
    )


@dataclass(frozen=True, slots=True)
class CandidateValue:
    fact_id: str
    field_id: str
    value: Any
    source_dataset: str
    definition_id: str
    period_kind: PeriodKind
    scope: str
    available_at: datetime
    valid_until: datetime

    def __post_init__(self) -> None:
        if not all((self.fact_id, self.field_id, self.source_dataset, self.definition_id, self.scope)):
            raise ValueError("candidate values require complete identities")
        for name in ("available_at", "valid_until"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if self.available_at > self.valid_until:
            raise ValueError("valid_until cannot precede available_at")


@dataclass(frozen=True, slots=True)
class FieldRoute:
    field_id: str
    primary_dataset: str
    fallback_datasets: tuple[str, ...]
    definition_id: str
    period_kind: PeriodKind
    scope: str


@dataclass(frozen=True, slots=True)
class RequestCall:
    dataset_id: str
    field_ids: tuple[str, ...]
    purpose: str


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    values: Mapping[str, CandidateValue]
    gaps: Mapping[str, str]
    calls: tuple[RequestCall, ...]
    fallback_reasons: Mapping[str, str]


def _candidate_valid(candidate: CandidateValue, route: FieldRoute, as_of: datetime) -> bool:
    return (
        candidate.field_id == route.field_id
        and candidate.definition_id == route.definition_id
        and candidate.period_kind is route.period_kind
        and candidate.scope == route.scope
        and candidate.available_at <= as_of <= candidate.valid_until
        and candidate.value is not None
    )


def resolve_fields(
    routes: Sequence[FieldRoute],
    *,
    as_of: datetime,
    cache: Mapping[str, CandidateValue],
    fetch: Callable[[str, frozenset[str]], Mapping[str, CandidateValue | None]],
) -> ResolutionResult:
    """Use valid cache, coalesce primary requests, then fill only actual gaps."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    cutoff = as_of.astimezone(timezone.utc)
    by_field = {route.field_id: route for route in routes}
    if len(by_field) != len(routes):
        raise ValueError("field routes must be unique")
    values: dict[str, CandidateValue] = {}
    gaps: dict[str, str] = {}
    fallback_reasons: dict[str, str] = {}
    calls: list[RequestCall] = []

    unresolved = set(by_field)
    for field_id in tuple(unresolved):
        cached = cache.get(field_id)
        if cached is not None and _candidate_valid(cached, by_field[field_id], cutoff):
            values[field_id] = cached
            unresolved.remove(field_id)
        elif cached is not None:
            gaps[field_id] = "stale_or_incompatible_cache"

    primary_groups: dict[str, set[str]] = {}
    for field_id in unresolved:
        primary_groups.setdefault(by_field[field_id].primary_dataset, set()).add(field_id)
    for dataset_id, field_ids in sorted(primary_groups.items()):
        calls.append(RequestCall(dataset_id, tuple(sorted(field_ids)), "primary"))
        try:
            returned = fetch(dataset_id, frozenset(field_ids))
        except Exception:
            returned = {}
            for field_id in field_ids:
                fallback_reasons[field_id] = "primary_failed"
        for field_id in field_ids:
            candidate = returned.get(field_id)
            if candidate is not None and _candidate_valid(candidate, by_field[field_id], cutoff):
                values[field_id] = candidate
                unresolved.discard(field_id)
                gaps.pop(field_id, None)
            else:
                fallback_reasons.setdefault(
                    field_id,
                    "primary_missing" if candidate is None else "primary_validation_failed",
                )

    max_fallbacks = max((len(by_field[item].fallback_datasets) for item in unresolved), default=0)
    for rank in range(max_fallbacks):
        groups: dict[str, set[str]] = {}
        for field_id in unresolved:
            candidates = by_field[field_id].fallback_datasets
            if rank < len(candidates):
                groups.setdefault(candidates[rank], set()).add(field_id)
        for dataset_id, field_ids in sorted(groups.items()):
            calls.append(RequestCall(dataset_id, tuple(sorted(field_ids)), "fallback"))
            try:
                returned = fetch(dataset_id, frozenset(field_ids))
            except Exception:
                returned = {}
            for field_id in tuple(field_ids):
                if field_id not in unresolved:
                    continue
                candidate = returned.get(field_id)
                if candidate is not None and _candidate_valid(candidate, by_field[field_id], cutoff):
                    values[field_id] = candidate
                    unresolved.remove(field_id)
                    gaps.pop(field_id, None)

    for field_id in unresolved:
        gaps[field_id] = "no_valid_value"
    return ResolutionResult(
        values=values,
        gaps=gaps,
        calls=tuple(calls),
        fallback_reasons=fallback_reasons,
    )
