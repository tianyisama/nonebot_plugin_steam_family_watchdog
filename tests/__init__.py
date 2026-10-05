import nonebot

nonebot.init(driver="~none", _env_file=None, superusers=["999"], apscheduler_autostart=False, steam_family_web_enabled=False)
assert nonebot.load_plugin("nonebot_plugin_steam_family_watchdog") is not None
