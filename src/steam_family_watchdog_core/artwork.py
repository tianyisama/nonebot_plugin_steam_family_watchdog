"""仅把 Steam 返回的安全路径转换为固定 Steam CDN 图片地址。"""

import re
from urllib.parse import quote


def artwork(appid: int, capsule_filename: object = None, icon_hash: object = None) -> dict:
    capsule = capsule_filename if (
        isinstance(capsule_filename, str) and len(capsule_filename) <= 512
        and re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", capsule_filename)
        and not any(part in (".", "..") for part in capsule_filename.split("/"))
    ) else None
    icon = icon_hash.lower() if isinstance(icon_hash, str) and re.fullmatch(r"[a-fA-F0-9]{40}", icon_hash) else None
    capsule_url = (
        f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/"
        + "/".join(quote(part) for part in capsule.split("/"))
    ) if capsule else None
    icon_url = f"https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon}.jpg" if icon else None
    return {
        "capsule_filename": capsule, "img_icon_hash": icon,
        "capsule_image_url": capsule_url, "icon_image_url": icon_url,
        "image_url": capsule_url or icon_url,
    }
