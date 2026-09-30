"""Synthetic multi-industry asset corpus for Skill template migration checks.

The corpus is intentionally structured like the marine governance demo assets
so the same deterministic knowledge pipeline and the same Skill workflow
templates can be run against another asset source with only configuration
changes (asset source, vocabulary, standard codes, question set).

Level: demo corpus, NOT real de-identified industry data. Every title carries
an explicit 演示 marker so it can never be confused with a real dataset.
"""

from __future__ import annotations

from typing import Any


def medical_assets() -> list[dict[str, Any]]:
    """Three synthetic healthcare-policy assets (医疗政策演示语料)."""
    return [
        {
            "asset_id": "MIG-MED-POLICY-001",
            "asset_type": "document",
            "title": "县域医保基金监管工作方案（演示）",
            "region": "演示市",
            "township": "演示县",
            "standard_codes": ["medical-insurance", "demo"],
            "content_text": (
                "县域医保基金监管工作方案（演示）明确：医保结算数据中发现异常住院、"
                "重复报销、自费转医保等线索后，由基金监管科室复核，再通过稽核平台"
                "派发核查任务。重点覆盖定点医疗机构和零售药店；核查完成后回传处理"
                "结果与退费金额，形成稽核处置闭环。"
            ),
        },
        {
            "asset_id": "MIG-MED-LEDGER-001",
            "asset_type": "table",
            "title": "县域医保结算异常月度台账（演示）",
            "region": "演示市",
            "township": "演示县",
            "standard_codes": ["medical-insurance", "demo"],
            "content_text": (
                "县域医保结算异常月度台账（演示）显示：重复报销线索占比最高，"
                "自费转医保集中在部分定点药店，异常住院集中在高龄患者群体。"
                "每条核查任务关联医保结算单号、复核人、派发时间和退费回执；"
                "经办机构按区域协同处理，未形成处置闭环的计为未完成。"
            ),
        },
        {
            "asset_id": "MIG-MED-DISPATCH-001",
            "asset_type": "document",
            "title": "医保稽核线索分级处置规则（演示）",
            "region": "演示市",
            "township": "演示县",
            "standard_codes": ["medical-insurance", "demo"],
            "content_text": (
                "医保稽核线索分级处置规则（演示）：重复报销和自费转医保线索由系统"
                "自动标记，基金监管科室依据风险等级决定人工复核还是派发核查任务。"
                "定点医疗机构与零售药店分别对应不同处置时限；每条任务回传处理结果"
                "、退费金额与复核人，形成稽核处置闭环，供决策追溯。"
            ),
        },
    ]


def government_assets() -> list[dict[str, Any]]:
    """Three synthetic government-service assets (政务事项演示语料)."""
    return [
        {
            "asset_id": "MIG-GOV-POLICY-001",
            "asset_type": "document",
            "title": "政务服务事项办理监督工作方案（演示）",
            "region": "演示市",
            "township": "演示县",
            "standard_codes": ["government-service", "demo"],
            "content_text": (
                "政务服务事项办理监督工作方案（演示）明确：办事窗口受理企业开办、"
                "建筑工程许可等事项后，由督查科对超时办件和重复申请进行复核，"
                "再通过政务督办平台派发整改任务。重点覆盖乡镇便民服务中心；"
                "整改完成后回传办理结果与满意度评价，形成监督处置闭环。"
            ),
        },
        {
            "asset_id": "MIG-GOV-LEDGER-001",
            "asset_type": "table",
            "title": "政务办件超时月度台账（演示）",
            "region": "演示市",
            "township": "演示县",
            "standard_codes": ["government-service", "demo"],
            "content_text": (
                "政务办件超时月度台账（演示）显示：企业开办超时件集中在材料补正环节"
                "，建筑工程许可重复申请占比高，乡镇便民服务中心整改时限压力最大。"
                "每条督办任务关联事项编号、督查科复核人、派发时间和整改回执；"
                "办理结果与满意度评价进入监督处置闭环。"
            ),
        },
        {
            "asset_id": "MIG-GOV-DISPATCH-001",
            "asset_type": "document",
            "title": "政务督办事项分级处置规则（演示）",
            "region": "演示市",
            "township": "演示县",
            "standard_codes": ["government-service", "demo"],
            "content_text": (
                "政务督办事项分级处置规则（演示）：超时办件和重复申请由系统自动"
                "标记，督查科依据事项类型和办件时限决定督办等级。乡镇便民服务中心"
                "与县级部门分别对应不同整改时限；每条督办任务回传办理结果与满意度"
                "评价，形成监督处置闭环，供决策追溯。"
            ),
        },
    ]


def marine_assets() -> list[dict[str, Any]]:
    """Three synthetic marine-governance assets (海洋治理演示语料)."""
    return [
        {
            "asset_id": "MIG-SEA-POLICY-001",
            "asset_type": "document",
            "title": "连江县海漂垃圾治理工作方案（演示）",
            "region": "连江县",
            "township": "马鼻镇",
            "standard_codes": ["marine-litter", "demo"],
            "content_text": (
                "连江县海漂垃圾治理工作方案（演示）明确：岸基摄像头发现泡沫、塑料、"
                "渔网等海漂垃圾后，由值班研判人员复核，再通过派单平台调度打捞机器人"
                "或机械臂执行拾取。拾取完成后回传照片与称重数据，形成处置闭环。"
            ),
        },
        {
            "asset_id": "MIG-SEA-LEDGER-001",
            "asset_type": "table",
            "title": "连江重点区域海漂垃圾月度台账（演示）",
            "region": "连江县",
            "township": "马鼻镇",
            "standard_codes": ["marine-litter", "demo"],
            "content_text": (
                "连江重点区域海漂垃圾月度台账（演示）显示：马鼻镇泡沫聚集次数最多，"
                "黄岐镇塑料和渔网占比高，筱埕镇养殖区存在网绳缠绕风险。处置资源包括"
                "打捞机器人、岸基机械臂和人工回收队伍；每条任务记录关联摄像头编号、"
                "值班审批人、派单时间和拾取回执。"
            ),
        },
        {
            "asset_id": "MIG-SEA-DISPATCH-001",
            "asset_type": "document",
            "title": "岸基监测与处置资源调度规范（演示）",
            "region": "连江县",
            "township": "马鼻镇",
            "standard_codes": ["marine-litter", "demo"],
            "content_text": (
                "岸基监测与处置资源调度规范（演示）：岸基摄像头发现泡沫、塑料、渔网"
                "等海漂垃圾后自动生成事件；值班研判人员复核后形成派单指令。打捞机器人"
                "、岸基机械臂与人工回收队伍按区域协同处置；每条处置记录回传拾取照片"
                "、称重数据与审批人，形成处置闭环。"
            ),
        },
    ]


SKILL_TO_DOMAIN_ROLE = {
    "policy-evidence-qa": "政策/规范依据问答：把“政策证据问答”模板中的领域词与工具参数替换为医疗或政务语义",
    "marine-event-assessment": "事件研判：医疗为“稽核线索研判”，政务为“督办事项研判”，触发条件与工具顺序保持一致",
    "cross-document-decision": "跨文档决策：同一套“检索-推理”模板跨三领域复用",
    "dispatch-work-order-orchestration": "工单编排：医疗为“稽核任务派发”，政务为“督办任务派发”",
    "decision-trace-audit": "决策轨迹审计：三领域共用同一证据链审计模板",
}

SEARCH_QUESTIONS = {
    "medical": "重复报销线索如何形成稽核处置闭环？",
    "government": "企业开办超时办件如何进入督办处置闭环？",
    "marine": "泡沫海漂垃圾如何形成派单与处置闭环？",
}
