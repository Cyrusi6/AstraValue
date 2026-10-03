"""Light first package: useful category summaries and read entrypoints only."""
from .authoring import VERSION


def aligned_peers(peers):
    accepted, excluded = [], []
    keys = ("period", "period_type", "unit", "currency", "scope")
    signatures = {}
    for peer in peers:
        for metric in peer.get("metrics", []):
            if all(metric.get(k) for k in keys):
                signatures.setdefault(metric.get("metric_id"), set()).add(tuple(metric[k] for k in keys))
    for peer in peers:
        metrics=[]
        for m in peer.get("metrics",[]):
            if all(m.get(k) for k in keys) and len(signatures.get(m.get("metric_id"),()))==1:
                metrics.append(m)
            else:excluded.append({"ticker":peer.get("ticker"),"metric_id":m.get("metric_id"),"reason":"missing_or_unaligned_comparison_definition"})
        if metrics:accepted.append({**peer,"metrics":metrics})
    return accepted,excluded


class Briefing:
    def __init__(self,workspace):self.w=workspace

    def get_research_brief(self,research_id:str):
        """Get only useful category summaries, date spans, availability and expansion links."""
        from .catalog import Catalog
        result=Catalog(self.w).catalog_overview(research_id)
        s,_,payload=self.w.pack(research_id)
        peers, excluded = aligned_peers(payload.get("peers", []))
        return {**result,'company':s['company'],'as_of':s['as_of'],'writing_contract':VERSION,
            'peer_comparison': {
                'status': 'ready' if peers else 'pending',
                'included_tickers': [item.get('ticker') for item in peers],
                'items': peers,
                'excluded_metrics': excluded,
                'limitations': '冻结同行事实用于同口径观察；品牌带、渠道、产品结构和区域差异未标准化，集合仍需研究者确认。',
            },
            'content':'按类别展开目录，再选择指标、完整报表、附注或公告读取。',
            'prompt_tool':'get_research_prompt'}
