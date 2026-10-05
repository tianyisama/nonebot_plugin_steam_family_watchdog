"""OneBot v11 推送内容与超级用户 ID 解析。"""

from datetime import datetime, timedelta, timezone

from nonebot.adapters.onebot.v11 import Message, MessageSegment


def notification(events: list[dict], images: bool = True) -> Message:
    message = Message()
    for index, event in enumerate(events):
        if index:
            message += MessageSegment.text("\n\n")
        label = "家庭库新增游戏" if event["type"] == "game_added" else "家庭库新增拥有者"
        owners = "、".join(owner.get("name") or owner["steamid"] for owner in event.get("added_owners", [])) or "未知"
        try:
            at = datetime.fromisoformat(event["detected_at"].replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8)))
            at_text = at.strftime("%Y-%m-%d %H:%M:%S") + "（北京时间）"
        except (ValueError, KeyError):
            at_text = event.get("detected_at", "未知")
        context = "\n来源：新成员加入家庭" if event.get("source_context") == "member_joined" else ""
        message += MessageSegment.text(f"{label}\n{event['name']}\n新增来源：{owners}\n发现时间：{at_text}{context}\nhttps://store.steampowered.com/app/{event['appid']}/")
        if images and event.get("image_url"):
            message += MessageSegment.image(event["image_url"])
    return message


def superuser_ids(superusers, adapter_name: str = "OneBot V11") -> list[str]:
    result = []
    for user in sorted(superusers):
        text = str(user)
        if ":" in text:
            prefix, _, text = text.rpartition(":")
            if prefix != adapter_name:
                continue
        if text.isascii() and text.isdigit() and 0 < int(text) <= 2**63 - 1:
            normalized = str(int(text))
            if normalized not in result:
                result.append(normalized)
    return result
