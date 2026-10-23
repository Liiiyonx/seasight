"""告警外发 —— 把高优先级事件推到企业微信机器人。

背景（产品完整性缺口 4）：告警只在「大屏滚动」，操作员不会一直盯着大屏，
告警必须推到人。这里做企业微信机器人外发 —— 政务场景最轻量、零 App 依赖，
一个群机器人 webhook 就能把告警投递到群、@ 到人。

★ 诚实降级：webhook 未配置或发送失败时，只落日志、绝不抛异常。
告警外发是「尽力而为」的旁路，不能反过来阻塞事件派单主流程。
（历史上项目反复栽在「旁路失败把主流程带崩」上 —— 这里用 try/except
把外发彻底隔离，并默认 webhook 为空 = 不启用。）
"""

from __future__ import annotations

from loguru import logger

from app.core.config import settings


def build_alert_text(
    main_class_label: str,
    device_id: str,
    lng: float,
    lat: float,
    ts: str,
) -> str:
    """构造告警正文（纯文本，供企业微信 text 消息使用）。"""
    return (
        f"【海漂垃圾告警】\n"
        f"类别：{main_class_label}\n"
        f"设备：{device_id}\n"
        f"位置：({lng:.4f}, {lat:.4f})\n"
        f"时间：{ts}"
    )


async def send_wecom_alert(content: str) -> bool:
    """发送企业微信机器人告警，返回是否成功；任何失败都只记日志、不抛出。

    webhook 未配置时直接跳过（返回 False）—— 这是「未启用」，不是「失败」。
    """
    webhook = settings.wecom_webhook
    if not webhook:
        return False

    payload: dict = {
        "msgtype": "text",
        "text": {"content": content},
    }
    mobiles = [m for m in settings.wecom_mention_mobile.split(",") if m.strip()]
    if mobiles:
        payload["text"]["mentioned_mobile_list"] = mobiles

    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(webhook, json=payload)
        if resp.status_code == 200:
            logger.info("[通知] 企业微信告警已发送")
            return True
        logger.warning(f"[通知] 企业微信返回 {resp.status_code}：{resp.text[:200]}")
        return False
    except Exception as exc:   # noqa: BLE001
        logger.warning(f"[通知] 企业微信发送失败：{exc}")
        return False
