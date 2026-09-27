from decimal import Decimal
import pytest
from test_research_calculations import FrozenInputs
from analysis.research.calculation_schema import public_calculator
from analysis.research.workspace import ResearchError


class Inputs(FrozenInputs):
    def __init__(self):
        super().__init__()
        self.texts = {'A':'将测试酒（2026）销售合同价由100元/瓶调整为110元/瓶。',
            'B':'将测试酒（2026）销售合同价由110元/瓶调整为121元/瓶。',
            'D':'2025年度累计派发现金红利0.000008亿元（含年度分红预案金额）'}
    def read_evidence(self,rid,ref,**kwargs):
        return dict(content=self.texts[ref],original_hash_verified=True,source_role='issuer_disclosure',
            evidence_id=ref,period='2026-01-01' if ref=='A' else '2026-02-01',original_sha256='hash')


def forecast(g=0):
    return {'dividend_growth':{'value':g,'reason':'维持历史总派息假设'},
        'horizon':{'value':'next_12_months','reason':'一年持有期'}}


def test_chained_price_change_is_compounded_not_added():
    r=public_calculator(Inputs())('r','price_change',{'start_evidence':'A','end_evidence':'B'})
    assert Decimal(r['result']['value']) == Decimal('.21')
    assert r['source_evidence']['start_evidence']['quote']
    assert r['value_reference'].endswith('.value}}')


def test_payout_and_forecast_yield_have_separate_bases():
    calc=public_calculator(Inputs())
    r=calc('r','payout_ratio',{'dividend_evidence':'D','earnings':'E'})
    assert Decimal(r['result']['value']) == Decimal('.8')
    b={'dividend_evidence':'D','shares':'S','price':'P'}
    historical=calc('r','dividend_yield',b)['result']
    predicted=calc('r','forecast_dividend_yield',b,forecast(.25))['result']
    assert Decimal(predicted['forecast_dividend_total']) == 1000
    assert Decimal(predicted['value']) == Decimal(1000)/15000
    assert historical['basis'] != predicted['basis']
    assert predicted['price_date']=='2026-09-11'
    assert Decimal(calc('r','forecast_dividend_yield',b,forecast(-1))['result']['value']) == 0


@pytest.mark.parametrize('change', ['product','chain','ambiguous','zero'])
def test_bad_price_inputs_rejected(change):
    w=Inputs()
    w.texts['B']={'product':'将其他酒销售合同价由110元/瓶调整为121元/瓶。',
        'chain':'将测试酒（2026）销售合同价由115元/瓶调整为121元/瓶。',
        'ambiguous':w.texts['B']*2,'zero':'将测试酒（2026）销售合同价由110元/瓶调整为0元/瓶。'}[change]
    with pytest.raises(ResearchError):
        public_calculator(w)('r','price_change',{'start_evidence':'A','end_evidence':'B'})


def test_year_mismatch_and_missing_prediction_assumption_rejected():
    w=Inputs(); w.rows[0]['period']='2024-12-31'
    with pytest.raises(ResearchError,match='year_or_scope'):
        public_calculator(w)('r','payout_ratio',{'dividend_evidence':'D','earnings':'E'})
    with pytest.raises(ResearchError,match='explicit_dividend'):
        public_calculator(w)('r','forecast_dividend_yield',{'dividend_evidence':'D','shares':'S','price':'P'})


@pytest.mark.parametrize('bad', ['0','NaN','Infinity'])
def test_bad_market_input(bad):
    w=Inputs();w.rows[2]['fact']['value']=bad
    with pytest.raises(ResearchError):
        public_calculator(w)('r','dividend_yield',{'dividend_evidence':'D','shares':'S','price':'P'})


def test_current_quote_period_is_accepted_but_cumulative_is_not():
    w=Inputs()
    for x in w.rows[1:3]:
        x['period_type']='current';x['fact']['period_type']='market_quote'
    public_calculator(w)('r','dividend_yield',{'dividend_evidence':'D','shares':'S','price':'P'})
    w.rows[2]['period_type']='cumulative'
    with pytest.raises(ResearchError,match='instant_market'):
        public_calculator(w)('r','dividend_yield',{'dividend_evidence':'D','shares':'S','price':'P'})


def test_unverified_source_rejected():
    w=Inputs(); original=w.read_evidence
    w.read_evidence=lambda *a,**kw: {**original(*a,**kw),'original_hash_verified':False}
    with pytest.raises(ResearchError,match='verified_issuer'):
        public_calculator(w)('r','price_change',{'start_evidence':'A','end_evidence':'B'})
