"""终端扫码登录入口。"""

import asyncio

import qrcode

from .authentication import LoginSession, save_auth


async def login_qr(config, log=print):
    session = LoginSession()
    qr_file = config.data_dir / "login-qr.png"
    previous_url = ""
    interacted = False
    try:
        await session.start_qr()
        while True:
            if session.challenge_url != previous_url:
                qr = qrcode.QRCode(border=4)
                qr.add_data(session.challenge_url)
                qr.make(fit=True)
                qr.make_image().resize((440, 440)).save(qr_file)
                qr.print_ascii(invert=True)
                previous_url = session.challenge_url
                log(f"请使用 Steam 手机 App 扫码并确认登录。二维码图片：{qr_file}")
            if await session.poll():
                save_auth(config.data_dir / "auth.json", session.refresh_token)
                log("Steam 登录成功；长期凭据已保存。现在可以启动服务。")
                return
            if session.remote_interaction and not interacted:
                interacted = True
                log("已扫描二维码，请在 Steam 手机 App 中确认此次登录。")
            await asyncio.sleep(session.interval)
    finally:
        await session.close()
        qr_file.unlink(missing_ok=True)
