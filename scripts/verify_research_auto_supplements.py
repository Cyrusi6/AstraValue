"""Read-only acceptance of registered automatic supplements and public catalog states."""
import argparse
from collections import Counter
import json
from pathlib import Path
from analysis.research.workspace import ResearchWorkspace,sha
from analysis.research.catalog import Catalog
from analysis.research.tools import operations
from analysis.research.material_status import present,LABELS
from analysis.research.supplement_transport import dump


def verify(w,rid):
    ops=operations(w);api=Catalog(w);state,path,_=w.pack(rid);before=sha(path/'manifest.json')
    _,data=api._load(rid);brief=ops['get_research_brief'](rid);lists=[]
    for category in brief['categories']:
        cat=category['category'];page=1;found=[]
        while page:
            view=ops['list_materials'](rid,cat,page=page,page_size=40)
            found.extend(view['items']);page=view['next_page']
        assert dict(Counter(x['status_label'] for x in found))==category['status_counts']
        assert all(x['status'] in LABELS for x in found)
        lists.extend(found)
    # Read each substituted supplier field/requirement, and a sample of each other reader/state.
    selected=[];seen=set()
    for item in data['items']:
        signature=(item['reader'],present(item)['status'])
        if item.get('availability') or item['reader']=='sw_classification' or signature not in seen:
            selected.append(item);seen.add(signature)
    reads=[]
    for item in selected:
        periods=item.get('availability',{}).get('available_periods',[])
        period=periods[-1] if periods else None
        result=ops['read_material'](rid,item['material_id'],period=period,max_tokens=4000)
        assert result['status']==present(item,period)['status'],item['title']
        if item.get('availability'):
            chunks=[result['content']];page=result.get('next_page')
            while page:
                more=ops['read_material'](rid,item['material_id'],period=period,page=page,max_tokens=4000)
                chunks.append(more['content']);page=more['next_page']
            content=json.loads(''.join(chunks))
            assert content['status']==result['status']
            assert all(r['state'] in LABELS for r in content['materials'])
        reads.append({'title':item['title'],'material_id':item['material_id'],'period':period,
                      'status':result['status'],'status_label':result['status_label']})
    sw=next(x for x in data['items'] if x['reader']=='sw_classification')
    classification=json.loads(ops['read_material'](rid,sw['material_id'],max_tokens=4000)['content'])
    assert classification['original_provider_value'] is None
    for item in data['items']:
        if item['reader']=='field' and item['payload']['field'] in {'SWINDUSTRY_CODE2','SWINDUSTRY_NAME2'}:
            parent=next(x for x in data['items'] if x['material_id']==item['payload']['parent_id'])
            assert all(r['fields'].get(item['payload']['field']) is None for r in parent['payload']['rows'])
    assert sha(path/'manifest.json')==before
    return {'research_id':rid,'company':state['company'],'snapshot_id':state['snapshot_id'],
            'frozen_manifest_sha256':before,'frozen_manifest_unchanged':True,'brief':brief,'top_level_items':lists,
            'all_item_status_counts':dict(Counter(present(x)['status_label'] for x in data['items'])),
            'read_probes':reads,'classification':classification,
            'remaining':[{'title':x['title'],**present(x)} for x in data['items'] if present(x)['status'] in {'processing','unavailable'}]}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--research-id',action='append',required=True)
    parser.add_argument('--task-id',action='append',default=[])
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();w=ResearchWorkspace();ops=operations(w);tasks=[]
    for ident in args.task_id:
        task=ops['get_task'](ident);assert task['state']=='completed',(ident,task['state'])
        before=task.get('network_requests',0)
        again=ops['request_materials'](task['research_id'],'重复请求核验','验证已有结果复用',
                                      material_types=task['material_types'],refresh=task.get('refresh',False))
        assert again['state']=='completed' and again.get('network_requests',0)==before
        tasks.append(task)
    companies=[verify(w,rid) for rid in args.research_id]
    dump(args.output/'自动补充与目录验收.json',{'companies':companies,'tasks':tasks,'repeated_requests_reused':True})
    lines=['# 自动补充与目录验收','','申万分类已纳入研究准备，API资料可自动请求、校验、登记；目录按六态及具体期间返回。','']
    for c in companies:
        counts='、'.join(f'{label}{count}项' for label,count in c['all_item_status_counts'].items())
        lines += [f"- {c['company']}：{len(c['read_probes'])}次实际读取通过，冻结包哈希不变；申万二级{c['classification']['industry_name']}，指数代码{c['classification']['industry_index_code']}。",
                  f"  当前目录：{counts}；待处理/尚未取得{len(c['remaining'])}项，具体范围见JSON。"]
    lines += ['', '目录条目数包含字段与各期资料，不等于文件数或研究完成比例。',
              '', '后台任务完成只表示此次资料查询结束。API空记录不代表无事项；分类为当前观察，不回填历史；原供应商空字段及财务原表空白保持不变。',
              '', '本次核验覆盖自动补充与目录读取，不代表五粮液完整数据层或新研报已验收。后台任务原件和协议证据保留于任务目录。']
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'验收说明.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    print(json.dumps({'companies':[{'company':c['company'],'read_probes':len(c['read_probes']),'remaining':len(c['remaining']),
                                  'statuses':c['all_item_status_counts']} for c in companies],
                      'tasks':[{'id':t['task_id'],'state':t['state'],'result':t['result']} for t in tasks]},ensure_ascii=False))


if __name__=='__main__':main()
