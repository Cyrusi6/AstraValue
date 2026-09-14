---
id: cashflow-definition
title: FCFF、经营现金流代理与再投资边界
author: Aswath Damodaran
work: 'Valuation: Lecture Note Packet 1 — Intrinsic Valuation'
version: retrieved-2026-09-14; source edition undated
source_url: https://pages.stern.nyu.edu/~adamodar/pdfiles/eqnotes/packet1.pdf
locator: PDF pages 17 and 94
source_sha256: d41a00b21269aef32369e1e5d70ff530632db03ba440a79a3d4473edbb2a7cb2
content_status: verified
content_nature: source-grounded project synthesis; application cautions are project
  interpretation
topics:
- 现金流
- FCFF
- DCF
- 营运资本
- cash_flow_quality
---

## 核心概念与公式

企业自由现金流（FCFF）是偿付全部资本提供者之前、在维持和扩展经营所需再投资之后的现金流。Damodaran的企业估值框架采用：FCFF = EBIT×(1−税率) − (资本开支−折旧) − 非现金营运资本增加额。经营资产价值是FCFF按WACC折现的现值；加上现金及非经营资产，再扣债务得到权益价值。金额与折现率必须使用一致的币种及名义/实际口径，权益价值除以同一估值基准下的股数才得到每股价值。

本项目的“经营现金流减购建长期资产支付的现金”是可复算代理指标，不能无条件更名为FCFF。经营现金流的会计分类、利息与税的归属、投资活动中的经营与金融资产投资都会影响对应关系。核对输入比给代理值更换名称重要。数据处理层负责公式；模型判断该方法能否代表公司的经济活动。

## 适用商业场景

适用于经营性投入、税负、营运资本和现金流能够解释的企业。评价品牌企业时，可用现金流框架追问新增收入需要多少再投资，以及高账面现金是否都可以分给普通股股东。对含财务公司或金融业务的合并主体，应识别客户存款、贷款、同业存单与经营活动的性质，不能把全部现金或全部投资支出视为普通工业企业的经营资金。

## 研判触发指标

当利润增长而经营现金流下降、资本开支发生跳变、库存增长快于销售，或现金余额很高但实际分配能力不清楚时，优先阅读现金流附注、营运资本及现金限制说明。这里没有通用固定比例阈值。经营现金流/净利润的短期下降是调查线索，须比较同季、完整年度与变化原因；它本身不证明造假，也不直接给出折现率。稳定增长终值要求折现率大于永续增长率，并解释增长所需再投资。

## 反例与失效边界

金融企业的债务与资产常属于经营投入，普通企业FCFF分类可能失真；极端负现金流或转型阶段也可能无法由简单稳定增长解释。高增长不能在预测期和终值中同时假设几乎不需再投资。现金流代理不足以支持FCFF时，可以保留代理趋势，并选用有依据的相对估值或股利路径；不能通过把缺失税率、资本开支或营运资本默认填零来强行完成DCF。卡片中的实务提醒是项目对上述框架的应用解释，不冒充原文逐字结论。
