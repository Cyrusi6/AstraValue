# 对象级数据流程

这张图描述一项公司研究从任务准备到报告导出的主要对象和边界。它补充 GitDiagram 的模块依赖图，重点回答“数据经过哪些阶段、在哪个阶段形成可追溯对象”。

```mermaid
flowchart TD
    task["研究任务<br/>公司 · 截止日 · 资料范围"] --> researcher["宿主模型 / 研究者<br/>解释 · 假设 · 评级 · 写作"]

    researcher --> workspace["研究工作区<br/>workspace.py"]
    workspace --> identity["公司与证券身份<br/>identity.py"]
    workspace --> scope["研究范围与问题路由<br/>scope.py"]
    workspace --> catalog["资料目录<br/>catalog.py"]

    scope --> plan["结构化数据计划<br/>字段 · 期间 · 来源版本"]
    plan --> acquire["采集运行<br/>runtime.py"]
    acquire --> orchestrator["采集编排<br/>orchestrator.py"]
    orchestrator --> adapter["官方来源适配器"]
    adapter --> raw["原始响应 / 报告正文"]
    raw --> snapshot["来源快照<br/>哈希 · 获取时间 · 定位"]
    snapshot --> repository["采集仓储<br/>repository.py"]
    snapshot --> catalog

    catalog -->|问题触发| reading["按需证据读取<br/>公告 · 事件 · 页码/段落"]
    reading --> evidence["Evidence / SourceRecord"]

    plan --> structured["结构化服务与运行时"]
    structured --> registry["字段注册表<br/>定义 · 单位 · 主源"]
    structured --> materialize["事实物化与跨来源核验"]
    snapshot --> materialize
    materialize --> facts["FactRecord / DimensionalFactRecord / EventRecord"]

    facts --> governance["分析与治理<br/>规范化 · 勾稽 · 冲突状态"]
    governance --> research_pack["八步研究包 ResearchPack<br/>core-pack · coverage · evidence"]
    facts --> research_pack
    evidence --> research_pack

    research_pack --> researcher
    researcher --> analysis_request["研究分析请求<br/>claims · sections · assumptions"]
    facts --> valuation["估值与情景计算<br/>代码执行确定性计算"]
    researcher -->|提出假设| valuation
    valuation --> model_run["ModelRun / Calculation"]

    research_pack --> bridge["报告桥接<br/>冻结包校验 · lineage"]
    analysis_request --> bridge
    model_run --> bridge
    bridge --> report["ReportVersion<br/>八步正文 · 结论卡 · 审计附录"]
    report --> exports["Markdown / HTML / XLSX / PDF"]

    researcher -.->|按需查阅| knowledge["知识服务"]

    control["共享控制面<br/>run · checkpoint · snapshot · registry"] -.-> acquire
    control -.-> structured
    control -.-> bridge

    classDef actor fill:#ede9fe,stroke:#7c3aed,color:#3b0764;
    classDef workspace fill:#dbeafe,stroke:#2563eb,color:#172554;
    classDef acquisition fill:#fef3c7,stroke:#d97706,color:#78350f;
    classDef structured fill:#dcfce7,stroke:#16a34a,color:#14532d;
    classDef analysis fill:#ffe4e6,stroke:#e11d48,color:#881337;
    classDef output fill:#e0e7ff,stroke:#4f46e5,color:#312e81;
    classDef control fill:#f8fafc,stroke:#475569,color:#0f172a;

    class task,researcher actor;
    class workspace,identity,scope,catalog,reading,evidence workspace;
    class plan,acquire,orchestrator,adapter,raw,snapshot,repository acquisition;
    class structured,registry,materialize,facts,research_pack structured;
    class governance,valuation,model_run,analysis_request analysis;
    class bridge,report,exports output;
    class knowledge,control control;
```

实线表示研究数据或报告对象的主路径；虚线表示按需知识读取和共享控制面。`ResearchPack` 是模型读取结构化事实和证据的边界，`ReportVersion` 是报告导出前的版本化结果。

图中把宿主模型和代码职责分开：模型提出解释、假设、评级和写作内容；采集、快照、字段整理、确定性计算、血缘校验和多格式导出由代码完成。资料缺口、冲突和未确认假设沿对象状态进入研究包和报告，不通过补值隐藏。

这张图描述目标主链和当前代码中已经明确的对象边界。API、CLI、SQLite、DuckDB/Parquet 和前端属于运行承载层，未展开在业务数据流中。
