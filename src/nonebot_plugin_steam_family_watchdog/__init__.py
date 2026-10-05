import asyncio
from datetime import datetime, timezone

from nonebot import get_bots, get_driver, get_plugin_config, on_command, require
from nonebot.adapters import Bot as BaseBot
from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from apscheduler.jobstores.base import JobLookupError

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from .config import Config
from .service import Service
from .web import WebManager

__plugin_meta__ = PluginMetadata(
    name="Steam 家庭库监控", description="Steam 家庭库变化推送与轻量配置管理",
    usage="超级用户：steam启动 / steam停止 / steam检查 / steam状态",
    homepage="https://github.com/tianyisama/nonebot_plugin_steam_family_watchdog",
    type="application", config=Config, supported_adapters={"~onebot.v11"},
)

driver = get_driver()
plugin_config = get_plugin_config(Config)
service = Service(plugin_config, get_bots, lambda: driver.config.superusers, logger.info)
web_manager = WebManager(service, logger.info)
JOB_ID = "steam_family_watchdog:pulse"
background: set[asyncio.Task] = set()


async def safe_pulse(bot=None):
    try:
        await service.pulse(bot)
    except Exception:
        service.startup_error = "后端处理异常，已暂停轮询；请检查目录或数据库权限，停止监控后再重新启动。"
        logger.error(service.startup_error)


async def scheduled_pulse():
    # APScheduler shutdown may cancel its executor future. Keep the scan owned
    # by this plugin so our shutdown hook can await it before closing SQLite.
    task = asyncio.create_task(safe_pulse())
    background.add(task)
    task.add_done_callback(background.discard)
    await asyncio.shield(task)


@driver.on_startup
async def startup():
    await service.startup()
    await web_manager.start()
    scheduler.add_job(scheduled_pulse, "interval", seconds=5, id=JOB_ID,
        replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=30,
        next_run_time=datetime.now(timezone.utc))


@driver.on_shutdown
async def shutdown():
    service.closing = True
    try:
        scheduler.remove_job(JOB_ID)
    except JobLookupError:
        pass
    await web_manager.close()
    if background:
        await asyncio.gather(*list(background), return_exceptions=True)
    await service.shutdown()


@driver.on_bot_connect
async def bot_connected(bot: BaseBot):
    if isinstance(bot, Bot):
        await safe_pulse(bot)


activate = on_command("steam启动", aliases={"steam启用"}, permission=SUPERUSER, priority=10, block=True)
deactivate = on_command("steam停止", aliases={"steam停用"}, permission=SUPERUSER, priority=10, block=True)
check_config = on_command("steam检查", permission=SUPERUSER, priority=10, block=True)
show_status = on_command("steam状态", permission=SUPERUSER, priority=10, block=True)


def check_message(result):
    lines = ["Steam 配置检查通过。" if result["ok"] else "Steam 配置检查未通过："]
    lines.extend(result.get("errors", []))
    lines.extend(result.get("warnings", []))
    if result["ok"]:
        lines.append(f"群聊 {result['groups']} 个，个人 {result['users']} 个。")
    return "\n".join(lines)


@activate.handle()
async def activate_handler(bot: Bot):
    result = await service.enable(bot, from_command=True)
    if result["ok"]:
        await activate.finish("Steam 监控已启用，状态已保存；以后随 Bot 启动自动恢复。\n首次成功扫描建立静默基线，后续变化按配置推送。")
    await activate.finish(check_message(result))


@deactivate.handle()
async def deactivate_handler():
    try:
        await service.disable()
    except Exception:
        await deactivate.finish("停止过程未正常完成，请检查数据目录权限和插件状态。")
    await deactivate.finish("Steam 监控已停止，数据锁已释放；重启 Bot 也会保持停止，可重新登录后发送 steam启动。")


@check_config.handle()
async def check_handler(bot: Bot):
    await check_config.finish(check_message(await service.check(bot)))


@show_status.handle()
async def status_handler():
    state = service.status()
    error = state.get("last_error")
    lines = [
        f"Steam 监控：{'已启用' if state['enabled'] else '已停止'}；{'已完成首次启用' if state['activated'] else '等待首次启用'}",
        f"Bot：{'已连接' if state['bot_connected'] else '未连接'}；Steam：{state['auth_state']}",
        f"推送：群聊 {state['groups']} 个，个人 {state['users']} 个",
        f"基线：{'已建立' if state.get('baseline_ready') else '未建立或监控已停止'}；累计事件 {state.get('event_count', '暂无')} 条",
        f"上次成功：{state.get('last_success_at') or '暂无'}",
        f"下次检查：{state.get('next_check_at') or '暂无'}（UTC）",
    ]
    if error:
        lines.append(f"最近错误：{error['code']}，{error['message']}")
    lines.extend(item for item in (state["startup_error"], state["web_error"]) if item)
    await show_status.finish("\n".join(lines))
