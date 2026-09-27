"""Real frozen Moutai sandbox acceptance; uses separate state and never edits research prose."""
import argparse
import json
from pathlib import Path
from analysis.research.workspace import ResearchWorkspace,read_json,sha
from analysis.research.tools import operations
from analysis.research.supplement_transport import dump


PERIODS=[f'{year}-12-31' for year in range(2021,2026)]
INPUTS={'history':{'metric_ids':['net_profit','operating_cash_flow'],'periods':PERIODS,'period_type':'cumulative'}}
CALC_CODE='''rows = data['tables']['history']['rows']
profits = [float(r['value']) for r in rows if r['metric_id'] == 'net_profit']
cash = [float(r['value']) for r in rows if r['metric_id'] == 'operating_cash_flow']
denominator = float(np.sum(profits))
if denominator == 0:
    raise ZeroDivisionError('total profit is zero')
result = {'cash_conversion': float(np.sum(cash)) / denominator}
'''
VERIFY_CODE='''total_profit = Decimal('0')
total_cash = Decimal('0')
for observation in data['tables']['history']['rows']:
    if observation['metric_id'] == 'net_profit':
        total_profit += Decimal(str(observation['value']))
    elif observation['metric_id'] == 'operating_cash_flow':
        total_cash += Decimal(str(observation['value']))
if total_profit == 0:
    raise ZeroDivisionError('zero total profit')
result = {'cash_conversion': float(total_cash / total_profit)}
'''
CHART_CODE='''frame = pd.DataFrame(data['tables']['history']['rows'])
frame['value'] = pd.to_numeric(frame['value']) / 100000000
wide = frame.pivot(index='period', columns='metric_id', values='value').sort_index()
fig, ax = plt.subplots(figsize=(8, 5.4))
ax.plot(wide['net_profit'], wide['operating_cash_flow'], color='#207a92', alpha=0.55, linewidth=1.4)
ax.scatter(wide['net_profit'], wide['operating_cash_flow'], color='#12566e', s=64, zorder=3)
for period, row in wide.iterrows():
    ax.annotate(period[:4], (row['net_profit'], row['operating_cash_flow']), xytext=(8, 7), textcoords='offset points', fontsize=11)
ax.plot([300, 1000], [300, 1000], '--', color='#aab4bc', linewidth=1, label='经营现金流 = 净利润')
ax.set(xlim=(300, 1020), ylim=(300, 1020), xlabel='合并净利润（亿元）', ylabel='经营活动现金流量净额（亿元）', title='贵州茅台：利润与现金回收\u005cn2021—2025年合并口径')
ax.grid(alpha=0.16)
ax.spines[['top', 'right']].set_visible(False)
ax.legend(loc='upper left', frameon=False)
fig.tight_layout()
chart_data = [{'period': p, 'net_profit': float(r['net_profit']), 'operating_cash_flow': float(r['operating_cash_flow']), 'unit': '亿元'} for p, r in wide.iterrows()]
'''


def workspace(output):
    current=ResearchWorkspace();source,path,_=current.pack('r_9fb83be3097c6af02aa3ea4d')
    config={**current.config,'state_root':str((output/'state').resolve()),'offline':True}
    dump(output/'workspace-config.json',config)
    w=ResearchWorkspace(current.root,config)
    task=w.prepare_research(source['ticker'],source['as_of']);rid=task['research_id']
    if task['snapshot_id']!=source['snapshot_id']:
        candidate=w.artifact(rid,'snapshot_candidate',{'candidate_pack_path':str(path),
            'candidate_snapshot_id':source['snapshot_id'],'reason':'复用已核验冻结数据进行沙盒接口验收'})
        operations(w)['adopt_snapshot'](rid,candidate['artifact_id'])
    return w,rid,path


def run(output):
    w,rid,path=workspace(output);ops=operations(w);before=sha(path/'manifest.json')
    calc=ops['run_python_analysis'](rid,'核验自定义五年累计现金回收比例',CALC_CODE,INPUTS,
        mode='calculation',definition='五年经营活动现金流量净额之和 / 同五年合并净利润之和',
        applicability='仅同公司同口径完整年度；包含合并金融业务影响，不是独立酒业现金回收率；不能把累计比例当单年比例',
        output_unit='ratio',output_period='2021-12-31/2025-12-31',retry=True)
    if calc.get('status')!='succeeded':raise RuntimeError(json.dumps(calc,ensure_ascii=False))
    # Selected history has ten rows: one profit and one cash flow for each year.
    zero=[{'path':['tables','history','rows',i,'value'],'value':'0'} for i in range(10)]
    equal=[{'path':['tables','history','rows',i,'value'],'value':'10'} for i in range(10)]
    validation=ops['validate_python_analysis'](rid,calc['analysis_id'],VERIFY_CODE,
        '主实现使用NumPy求和，复算逐条用Decimal累计；比较完整输出，并检查零分母和相等输入',
        [{'name':'zero_denominator','reason':'零利润不能作分母','overrides':zero,'expected_error':'ZeroDivisionError'},
         {'name':'equal_inputs','reason':'两类金额相同则累计比例应为1','overrides':equal,'expected_result':{'cash_conversion':1.0}}],retry=True)
    if validation.get('validation_status')!='validated':raise RuntimeError(json.dumps(validation,ensure_ascii=False))
    chart=ops['run_python_analysis'](rid,'比较各年利润与经营现金流对应关系',CHART_CODE,INPUTS,mode='chart',retry=True)
    if chart.get('status')!='succeeded':raise RuntimeError(json.dumps(chart,ensure_ascii=False))
    repeated=ops['run_python_analysis'](rid,'比较各年利润与经营现金流对应关系',CHART_CODE,INPUTS,mode='chart')
    assert repeated['analysis_id']==chart['analysis_id']
    assert before==sha(path/'manifest.json')
    result={'research_id':rid,'snapshot_id':w.task(rid)[0]['snapshot_id'],'calculation':calc,
        'validation':validation,'chart':chart,'frozen_manifest_sha256':before,'frozen_manifest_unchanged':True,
        'scope':'接口验收使用独立研究状态，不修改现有茅台报告；后续必须实际查看图并提交图义与数据审阅'}
    dump(output/'真实运行结果.json',result)
    print(json.dumps(result,ensure_ascii=False))


def report(output):
    saved=read_json(output/'真实运行结果.json');w,rid,path=workspace(output);ops=operations(w);sid=w.task(rid)[0]['snapshot_id']
    cid=saved['chart']['chart_id'];eid=saved['calculation']['exploration_id']
    for number in range(1,9):
        prose='本页为工具组装验收文字，不是公司投资研究。{{cite:F440}}'
        refs=['F440']
        if number==2:
            prose+='五年累计经营现金流与合并净利润比例为{{explore:'+eid+'.cash_conversion}}。\n\n{{chart:'+cid+'}}'
            refs.append(eid)
        ops['save_section'](rid,sid,number,prose,'仅验证计算及图表引用，不构成评级判断',refs)
    ops['save_conclusion'](rid,sid,'暂不评级','定制图与探索计算工具验收样稿',["验证工具产物可追溯并正确组装"],
        '测试样稿不形成公司投资判断。',valuation_unavailable_reason='本次仅验收沙盒及组装工具，没有执行估值研究。')
    result=ops['build_report'](rid,['md','html'])
    body=Path(result['outputs']['md']['path']).read_text(encoding='utf8')
    assert '经验证的探索计算' in body and '{{explore:' not in body and '{{chart:' not in body
    assert sha(path/'manifest.json')==saved['frozen_manifest_sha256']
    dump(output/'组装核验结果.json',result)
    print(json.dumps(result,ensure_ascii=False))


def protocol(output):
    """Exercise the real stdio MCP boundary; reuse the already inspected image bytes."""
    import asyncio
    import base64
    import hashlib
    import os
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    saved=read_json(output/'真实运行结果.json');rid=saved['research_id']
    root=Path(__file__).resolve().parents[1]
    entry=('import sys; from pathlib import Path; '
           'from analysis.research.workspace import ResearchWorkspace, read_json; '
           'from analysis.research.mcp_server import create_server; '
           'create_server(ResearchWorkspace(config=read_json(Path(sys.argv[1])))).run(transport="stdio")')

    async def check():
        env=dict(os.environ,PYTHONPATH=str(root/'src'),PYTHONIOENCODING='utf-8')
        params=StdioServerParameters(command=sys.executable,
            args=['-c',entry,str(output/'workspace-config.json')],env=env)
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                listing=await session.list_tools();names={tool.name for tool in listing.tools}
                required={'run_python_analysis','get_python_analysis','validate_python_analysis','review_custom_chart'}
                assert required<=names

                async def call(name,args):
                    response=await session.call_tool(name,args)
                    if response.isError:raise RuntimeError(str(response.content))
                    return response.structuredContent or json.loads(next(x.text for x in response.content if x.type=='text'))

                calc=await call('get_python_analysis',{'research_id':rid,'analysis_id':saved['calculation']['analysis_id']})
                assert calc['validation_status']=='validated'
                chart=await call('run_python_analysis',dict(research_id=rid,purpose='比较各年利润与经营现金流对应关系',
                    code=CHART_CODE,inputs=INPUTS,mode='chart'))
                assert chart['status']=='succeeded' and chart['result_entries']==saved['chart']['result_entries']
                picture=await session.call_tool('view_chart',{'research_id':rid,'chart_id':chart['chart_id']})
                assert not picture.isError
                blocks=[block for block in picture.content if block.type=='image']
                assert len(blocks)==1 and blocks[0].mimeType=='image/png'
                raw=base64.b64decode(blocks[0].data);image_hash=hashlib.sha256(raw).hexdigest()
                assert image_hash==saved['chart']['files']['figure.png']['sha256']
                (output/'MCP返回图像.png').write_bytes(raw)
                review=await call('review_custom_chart',dict(research_id=rid,chart_id=chart['chart_id'],approved=True,
                    visual_review='MCP返回PNG与已实际查看、确认中文标题和轴标签清晰的验收图逐字节相同。',
                    data_review='绘图底表与已按10个原始事实逐项核验的数据一致，只含亿元展示转换；保留合并累计年度口径。'))
                zero=[{'path':['tables','history','rows',i,'value'],'value':'0'} for i in range(10)]
                equal=[{'path':['tables','history','rows',i,'value'],'value':'10'} for i in range(10)]
                validation=await call('validate_python_analysis',dict(research_id=rid,
                    analysis_id=calc['analysis_id'],validation_code=VERIFY_CODE,
                    validation_reason='通过真实MCP复算同一冻结数据，并核对零分母与相等输入边界',
                    boundary_cases=[{'name':'zero_denominator','reason':'零利润不能作分母','overrides':zero,'expected_error':'ZeroDivisionError'},
                                    {'name':'equal_inputs','reason':'相等金额比例为1','overrides':equal,'expected_result':{'cash_conversion':1.0}}]))
                assert validation['validation_status']=='validated'
                result={'transport':'stdio','tools':sorted(required),'calculation_status':calc['status'],
                    'chart_id':chart['chart_id'],'image_mime_type':'image/png','image_sha256':image_hash,
                    'matches_visually_inspected_image':True,'chart_review_id':review['artifact_id'],
                    'validation':validation,'scope':'真实MCP协议验收，不替代Codex/Claude Code双宿主完整研究验收'}
                dump(output/'MCP调用核验.json',result)
                print(json.dumps(result,ensure_ascii=False))
    asyncio.run(check())


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--stage',choices=['run','report','mcp'],default='run')
    ap.add_argument('--output',type=Path,default=Path('output/research/Python沙盒验收-20260919'));args=ap.parse_args()
    {'run':run,'report':report,'mcp':protocol}[args.stage](args.output.resolve())
