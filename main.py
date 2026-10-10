import os
import time
import asyncio
import logging
from aiohttp import web
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import ChannelParticipantsAdmins
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.contacts import GetBirthdaysRequest
from telethon.errors import SlowModeWaitError, FloodWaitError

# --- Логирование ---
logging.basicConfig(
    format='[%(levelname)s %(asctime)s] %(name)s: %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Загрузка .env ---
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

if not all([API_ID, API_HASH, SESSION_STRING]):
    raise SystemExit("Ошибка: проверь .env — не все переменные заданы")

# --- Клиент Telegram ---
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# --- Настройки автоответа ---
AUTO_REPLY_TEXT = "Скоро отвечу. Ответ написан автоответчиком, созданным владельцем этого аккаунта."
OWNER_ACTIVITY_WINDOW = 30 * 60  # 30 минут

# {peer_id: timestamp последнего ТВОЕГО сообщения}
owner_last_message = {}

# --- Время запуска бота ---
BOT_START_TIME = time.time()


# --- HTTP-сервер для Render ---
async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"HTTP-сервер запущен на порту {port}")


# --- Отправка с обработкой FloodWait ---
async def safe_reply(event, text):
    try:
        return await event.reply(text)
    except SlowModeWaitError as e:
        logger.info(f"Медленный режим: ждём {e.seconds} сек...")
        await asyncio.sleep(e.seconds)
        return await event.reply(text)
    except FloodWaitError as e:
        logger.warning(f"FloodWait: ждём {e.seconds} сек...")
        await asyncio.sleep(e.seconds)
        return await event.reply(text)


# --- Отправка ошибки: "Error" в чат + полный текст в Saved Messages ---
async def send_error(event, error, context=""):
    try:
        await event.reply("Error")
    except Exception as e:
        logger.error(f"Не удалось отправить 'Error' в чат: {e}")

    try:
        error_text = (
            f"⚠️ ОШИБКА\n"
            f"Контекст: {context}\n"
            f"Чат: {event.chat_id}\n"
            f"Тип: {type(error).__name__}\n"
            f"Описание: {error}"
        )
        await client.send_message("me", error_text)
    except Exception as e:
        logger.error(f"Не удалось отправить ошибку в Saved Messages: {e}")


# --- Запоминаем ТВОИ сообщения ---
@client.on(events.NewMessage(outgoing=True))
async def owner_message_handler(event):
    if not event.is_private:
        return
    peer_id = event.chat_id
    owner_last_message[peer_id] = time.time()
    logger.info(f"Твоё сообщение в {peer_id}, таймер 30 мин пошёл")


# --- АВТООТВЕТ только в личке ---
@client.on(events.NewMessage(incoming=True))
async def autoreply_handler(event):
    if not event.is_private:
        return

    sender = await event.get_sender()
    if getattr(sender, "bot", False):
        return

    me = await client.get_me()
    if sender.id == me.id:
        return

    peer_id = event.chat_id
    last_owner_msg = owner_last_message.get(peer_id, 0)
    now = time.time()

    if now - last_owner_msg < OWNER_ACTIVITY_WINDOW:
        logger.info(f"Автоответ: ты писал в {peer_id} недавно, пропуск")
        return

    try:
        await safe_reply(event, AUTO_REPLY_TEXT)
        logger.info(f"Автоответ отправлен: {sender.id}")
    except Exception as e:
        logger.error(f"Ошибка автоответа: {e}")
        await send_error(event, e, context="autoreply_handler")


# --- Команда ping ---
@client.on(events.NewMessage(outgoing=True, pattern=r"^ping$"))
async def ping_command_handler(event):
    try:
        start = time.perf_counter_ns()
        await client.get_me()
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000

        uptime_seconds = int(time.time() - BOT_START_TIME)
        hours = uptime_seconds // 3600
        minutes = (uptime_seconds % 3600) // 60
        seconds = uptime_seconds % 60

        if hours > 0:
            uptime_str = f"{hours}ч {minutes}м {seconds}с"
        elif minutes > 0:
            uptime_str = f"{minutes}м {seconds}с"
        else:
            uptime_str = f"{seconds}с"

        text = (
            f"Ping: {elapsed_ms}мс\n"
            f"Bot: alive\n"
            f"Uptime: {uptime_str}"
        )

        await safe_reply(event, text)
        logger.info(f"Команда ping: {elapsed_ms}мс, uptime {uptime_str}")

    except Exception as e:
        logger.error(f"Ошибка команды ping: {e}")
        await send_error(event, e, context="ping_command_handler")


# --- Команда admins ---
@client.on(events.NewMessage(outgoing=True, pattern=r"^admins$"))
async def admins_command_handler(event):
    try:
        chat = await event.get_chat()

        if not hasattr(chat, "title"):
            await send_error(event, Exception("Это не группа"), context="admins")
            return

        admins_list = []
        try:
            async for user in client.iter_participants(chat, filter=ChannelParticipantsAdmins):
                if user.username:
                    admins_list.append(f"@{user.username}")
                else:
                    name = user.first_name or ""
                    if user.last_name:
                        name += f" {user.last_name}"
                    admins_list.append(name.strip() or f"id{user.id}")
        except Exception as e:
            logger.error(f"Ошибка получения админов: {e}")
            await send_error(event, e, context="admins / iter_participants")
            return

        if not admins_list:
            await safe_reply(event, "Admins: не найдены.")
            return

        admins_str = ", ".join(admins_list)
        if len(admins_str) > 3500:
            admins_str = admins_str[:3500] + "..."

        await safe_reply(event, f"Admins ({len(admins_list)}): {admins_str}")
        logger.info(f"Команда admins: {len(admins_list)} админов")

    except Exception as e:
        logger.error(f"Ошибка команды admins: {e}")
        await send_error(event, e, context="admins_command_handler")


# --- Команда members: файл со всеми участниками ---
@client.on(events.NewMessage(outgoing=True, pattern=r"^members$"))
async def members_command_handler(event):
    try:
        chat = await event.get_chat()

        if not hasattr(chat, "title"):
            await send_error(event, Exception("Это не группа"), context="members")
            return

        members_list = []
        try:
            async for user in client.iter_participants(chat):
                if user.username:
                    members_list.append(f"@{user.username}")
                else:
                    name = user.first_name or ""
                    if user.last_name:
                        name += f" {user.last_name}"
                    name = name.strip() or f"id{user.id}"
                    members_list.append(name)
        except Exception as e:
            logger.error(f"Ошибка получения участников: {e}")
            await send_error(event, e, context="members / iter_participants")
            return

        if not members_list:
            await safe_reply(event, "Members: не найдены.")
            return

        content = f"Members: {', '.join(members_list)}"

        filename = "members.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)

        try:
            await client.send_file(
                event.chat_id,
                filename,
                caption=f"Members: {len(members_list)}",
                reply_to=event.id
            )
            logger.info(f"Команда members: {len(members_list)} участников, файл отправлен ответом")
        finally:
            try:
                os.remove(filename)
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Ошибка команды members: {e}")
        await send_error(event, e, context="members_command_handler")


# --- Команда user @username ---
@client.on(events.NewMessage(outgoing=True, pattern=r"^user\s+@?(\w+)$"))
async def user_command_handler(event):
    try:
        username = event.pattern_match.group(1)

        try:
            user = await client.get_entity(username)
        except Exception:
            await safe_reply(event, f"Пользователь @{username} не найден.")
            return

        # Полная инфа (bio)
        full_info = None
        about = "Not specified"
        try:
            full_info = await client(GetFullUserRequest(user))
            if full_info.full_user.about:
                about = full_info.full_user.about
        except Exception as e:
            logger.warning(f"Не удалось получить full info: {e}")

        # Имя
        name = user.first_name or ""
        if user.last_name:
            name += f" {user.last_name}"
        name = name.strip() or "Not specified"

        # Username
        user_name = f"@{user.username}" if user.username else "Not specified"

        # Дата рождения
        birthday_str = "Not specified"
        try:
            birthdays = await client(GetBirthdaysRequest())
            for b in birthdays.birthdays:
                if hasattr(b, 'user_id') and b.user_id == user.id:
                    day = b.birthday.day
                    month = b.birthday.month
                    year = getattr(b.birthday, 'year', 0) or 0
                    if year:
                        birthday_str = f"{day:02d}.{month:02d}.{year}"
                    else:
                        birthday_str = f"{day:02d}.{month:02d}"
                    break
        except Exception as e:
            logger.warning(f"Не удалось получить дни рождения: {e}")

        # Номер телефона
        phone_str = "Not specified"
        if getattr(user, "phone", None):
            phone_str = f"+{user.phone}"
        elif full_info and getattr(full_info.full_user, "phone", None):
            phone_str = f"+{full_info.full_user.phone}"

        # Обрезаем about
        if len(about) > 500:
            about = about[:500] + "..."

        caption = (
            f"Name: {name}\n"
            f"UserName: {user_name}\n"
            f"About: {about}\n"
            f"Date of birth: {birthday_str}\n"
            f"Phone Number: {phone_str}"
        )

        # --- Получаем аватарку ---
        avatar_path = None
        try:
            photos = await client.get_profile_photos(user, limit=1)
            if photos:
                photo = photos[0]
                avatar_path = await client.download_media(photo, file="temp_avatar.jpg")
        except Exception as e:
            logger.error(f"Ошибка скачивания аватарки: {e}")

        # Отправляем
        if avatar_path:
            try:
                await client.send_file(
                    event.chat_id,
                    avatar_path,
                    caption=caption,
                    reply_to=event.id
                )
            finally:
                try:
                    os.remove(avatar_path)
                except Exception:
                    pass
        else:
            await safe_reply(event, f"(Аватарка отсутствует)\n\n{caption}")

        logger.info(f"Команда user: {username}")

    except Exception as e:
        logger.error(f"Ошибка команды user: {e}")
        await send_error(event, e, context="user_command_handler")


# --- Команда help ---
@client.on(events.NewMessage(outgoing=True, pattern=r"^help$"))
async def help_command_handler(event):
    try:
        text = (
            "Доступные команды:\n\n"
            "ping\n"
            "admins\n"
            "members\n"
            "user @username\n"
            "help"
        )
        await safe_reply(event, text)
        logger.info("Команда help обработана")
    except Exception as e:
        logger.error(f"Ошибка команды help: {e}")
        await send_error(event, e, context="help_command_handler")


# --- Запуск ---
async def main():
    await start_web_server()

    logger.info("Подключение к Telegram...")
    await client.start()
    me = await client.get_me()
    logger.info(f"Залогинен как: {me.first_name} (@{me.username})")
    logger.info("Бот запущен. Команды: ping, admins, members, user, help.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
