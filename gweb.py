import asyncio
import os
import random
from collections import defaultdict
from pathlib import Path
from typing import List, Dict

from pyrogram import Client, filters, enums
from pyrogram.types import Message
from pyrogram.errors import FloodWait

from gemini_webapi import GeneratedImage, WebImage

from utils.db import db
from utils.misc import modules_help, prefix
from modules.custom_modules.ai_gemini import get_client, TEMP_IMAGE_DIR, TEMP_FILE_DIR

os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
os.makedirs(TEMP_FILE_DIR, exist_ok=True)

GWEB_HISTORY_COLLECTION = "custom.gweb"
GWEB_SETTINGS = "custom.gweb_settings"
DEFAULT_HISTORY_COMBINE_SECONDS = 8

enabled_users = db.get(GWEB_SETTINGS, "enabled_users") or []
disabled_users = db.get(GWEB_SETTINGS, "disabled_users") or []
gweb_for_all = db.get(GWEB_SETTINGS, "gweb_for_all") or False

reply_queue = asyncio.Queue()
reply_worker_started = False


async def reply_worker(client: Client):
    while True:
        reply_func, args, kwargs = await reply_queue.get()
        cleanup_file = kwargs.pop("cleanup_file", None)
        try:
            try:
                await reply_func(*args, **kwargs)
            except FloodWait as e:
                try:
                    await client.send_message("me", f"FloodWait: sleeping {e.value}s")
                except Exception:
                    pass
                await asyncio.sleep(e.value + 1)
                await reply_func(*args, **kwargs)
        except Exception as e:
            try:
                await client.send_message("me", f"Reply queue error:\n{e}")
            except Exception:
                pass
        finally:
            if cleanup_file and os.path.exists(cleanup_file):
                try:
                    os.remove(cleanup_file)
                except Exception:
                    pass
        await asyncio.sleep(2.1)


def ensure_reply_worker(client: Client):
    global reply_worker_started
    if not reply_worker_started:
        asyncio.create_task(reply_worker(client))
        reply_worker_started = True


async def send_reply(reply_func, args, kwargs, client):
    ensure_reply_worker(client)
    if isinstance(args, tuple):
        args = list(args)
    await reply_queue.put((reply_func, args, kwargs))


smileys = ["-.-", "):", ":)", "*.*", ")*", ";)"]
sticker_gif_buffer = defaultdict(list)
sticker_gif_timer = {}

async def process_sticker_gif_buffer(client: Client, user_id):
    try:
        await asyncio.sleep(8)
        msgs = sticker_gif_buffer.pop(user_id, [])
        sticker_gif_timer.pop(user_id, None)
        if not msgs:
            return
        last_msg = msgs[-1]
        random_smiley = random.choice(smileys)
        await asyncio.sleep(random.uniform(2, 6))
        await send_reply(last_msg.reply_text, [random_smiley], {}, client)
    except Exception:
        try:
            await client.send_message("me", "Sticker/GIF buffer error")
        except Exception:
            pass


async def send_typing_action(client: Client, chat_id: int, user_message: str):
    try:
        await client.send_chat_action(chat_id=chat_id, action=enums.ChatAction.TYPING)
        await asyncio.sleep(min(len(user_message) / 10, 5))
    except Exception:
        pass


async def _download_reply_media(replied: Message) -> List[Path]:
    files: List[Path] = []
    if not replied:
        return files

    for attr in ["document", "audio", "video", "voice", "video_note"]:
        media_obj = getattr(replied, attr, None)
        if media_obj:
            filename = getattr(media_obj, "file_name", None)
            ext_map = {
                "voice": ".ogg",
                "video_note": ".mp4",
                "video": ".mp4",
                "audio": ".mp3",
                "document": ".bin",
            }
            ext = Path(filename).suffix if filename else ext_map.get(attr, ".bin")
            path = os.path.join(TEMP_FILE_DIR, f"{media_obj.file_unique_id}{ext}")
            try:
                await replied.download(path)
                files.append(Path(path))
            except Exception:
                pass
            return files

    if replied.photo:
        path = os.path.join(TEMP_FILE_DIR, f"{replied.photo.file_unique_id}.jpg")
        try:
            await replied.download(path)
            files.append(Path(path))
        except Exception:
            pass

    return files


@Client.on_message((filters.sticker | filters.animation) & filters.private & ~filters.me & ~filters.bot, group=1)
async def handle_sticker_gif_buffered(client: Client, message: Message):
    user = message.from_user
    if not user:
        return
    user_id = user.id
    global enabled_users, disabled_users, gweb_for_all
    enabled_users = db.get(GWEB_SETTINGS, "enabled_users") or enabled_users
    disabled_users = db.get(GWEB_SETTINGS, "disabled_users") or disabled_users
    gweb_for_all = db.get(GWEB_SETTINGS, "gweb_for_all") or gweb_for_all

    if user_id in disabled_users or (not gweb_for_all and user_id not in enabled_users):
        return

    sticker_gif_buffer[user_id].append(message)
    if sticker_gif_timer.get(user_id):
        sticker_gif_timer[user_id].cancel()
    sticker_gif_timer[user_id] = asyncio.create_task(process_sticker_gif_buffer(client, user_id))


@Client.on_message(filters.text & filters.private & ~filters.me & ~filters.bot, group=1)
async def gweb_message_handler(client: Client, message: Message):
    try:
        user = message.from_user
        if not user:
            return
        user_id = user.id
        user_name = user.first_name or "User"
        user_message = message.text.strip()
        global enabled_users, disabled_users, gweb_for_all
        enabled_users = db.get(GWEB_SETTINGS, "enabled_users") or enabled_users
        disabled_users = db.get(GWEB_SETTINGS, "disabled_users") or disabled_users
        gweb_for_all = db.get(GWEB_SETTINGS, "gweb_for_all") or gweb_for_all

        if user_id in disabled_users or (not gweb_for_all and user_id not in enabled_users):
            return

        if not hasattr(client, "gweb_message_buffer"):
            client.gweb_message_buffer = {}
            client.gweb_message_timers = {}

        if user_id not in client.gweb_message_buffer:
            client.gweb_message_buffer[user_id] = []
            client.gweb_message_timers[user_id] = None

        client.gweb_message_buffer[user_id].append(user_message)

        if client.gweb_message_timers[user_id]:
            client.gweb_message_timers[user_id].cancel()

        async def process_combined_messages():
            await asyncio.sleep(DEFAULT_HISTORY_COMBINE_SECONDS)
            buffered_messages = client.gweb_message_buffer.pop(user_id, [])
            client.gweb_message_timers[user_id] = None
            if not buffered_messages:
                return
            combined_message = " ".join(buffered_messages)
            await asyncio.sleep(random.choice([1, 2, 3]))
            await send_typing_action(client, message.chat.id, combined_message)
            files = []
            if message.reply_to_message:
                files = await _download_reply_media(message.reply_to_message)
            history = db.get(GWEB_HISTORY_COLLECTION, f"chat_history.{user_id}") or []
            history.append(f"{user_name}: {combined_message}")
            db.set(GWEB_HISTORY_COLLECTION, f"chat_history.{user_id}", history)
            try:
                gem_client = await get_client()
            except Exception as e:
                await send_reply(client.send_message, [message.chat.id, f"❌ Gemini client error: {e}"], {}, client)
                return

            user_gem_id = db.get(GWEB_SETTINGS, f"user_gem.{user_id}", None)
            default_gem_id = db.get(GWEB_SETTINGS, "default_gem", None)
            gem_to_use = user_gem_id or default_gem_id

            if gem_to_use is not None:
                try:
                    await gem_client.fetch_gems(include_hidden=True)
                except Exception:
                    pass

            metadata = db.get(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}", None)
            chat = gem_client.start_chat(metadata=metadata, gem=gem_to_use) if gem_to_use else gem_client.start_chat(metadata=metadata)

            send_prompt = combined_message or "."
            try:
                response = await chat.send_message(send_prompt, files=files if files else None)
            except Exception as e:
                await send_reply(client.send_message, [message.chat.id, f"❌ Gemini error: {e}"], {}, client)
                return

            try:
                db.set(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}", chat.metadata)
            except Exception:
                pass

            bot_response = response.text or "❌ No answer found."

            full_history = db.get(GWEB_HISTORY_COLLECTION, f"chat_history.{user_id}") or []
            full_history.append(f"Bot: {bot_response}")
            db.set(GWEB_HISTORY_COLLECTION, f"chat_history.{user_id}", full_history)

            if getattr(response, "images", None):
                for i, image in enumerate(response.images):
                    try:
                        if isinstance(image, GeneratedImage):
                            file_path = os.path.join(TEMP_IMAGE_DIR, f"gweb_gen_{user_id}_{i}.png")
                            await image.save(path=TEMP_IMAGE_DIR, filename=f"gweb_gen_{user_id}_{i}.png", verbose=True)
                            await send_reply(client.send_photo, [message.chat.id, file_path], {"reply_to_message_id": message.id, "cleanup_file": file_path}, client)
                        elif isinstance(image, WebImage):
                            await send_reply(client.send_photo, [message.chat.id, image.url], {"reply_to_message_id": message.id}, client)
                    except Exception:
                        pass

            await send_reply(message.reply_text, [bot_response], {"reply_to_message_id": message.id}, client)

            for f in files:
                try:
                    if f and os.path.exists(f):
                        os.remove(f)
                except Exception:
                    pass

        client.gweb_message_timers[user_id] = asyncio.create_task(process_combined_messages())

    except Exception as e:
        try:
            await send_reply(client.send_message, ["me", f"gweb message handler error:\n\n{e}"], {}, client)
        except Exception:
            pass


@Client.on_message(filters.private & ~filters.me & ~filters.bot, group=1)
async def gweb_file_handler(client: Client, message: Message):
    file_path = None
    try:
        user = message.from_user
        if not user:
            return
        user_id = user.id
        user_name = user.first_name or "User"
        global enabled_users, disabled_users, gweb_for_all
        enabled_users = db.get(GWEB_SETTINGS, "enabled_users") or enabled_users
        disabled_users = db.get(GWEB_SETTINGS, "disabled_users") or disabled_users
        gweb_for_all = db.get(GWEB_SETTINGS, "gweb_for_all") or gweb_for_all

        if user_id in disabled_users or (not gweb_for_all and user_id not in enabled_users):
            return

        caption = message.caption.strip() if message.caption else ""
        chat_history = db.get(GWEB_HISTORY_COLLECTION, f"chat_history.{user_id}") or []
        chat_history.append(f"{user_name}: {caption}")
        db.set(GWEB_HISTORY_COLLECTION, f"chat_history.{user_id}", chat_history)

        if message.media_group_id:
            if not hasattr(client, "media_buffer"):
                client.media_buffer = defaultdict(list)
                client.media_timers = {}

            if message.photo:
                path = await client.download_media(message.photo)
            else:
                path = await client.download_media(message.document or message.video or message.audio or message.voice or message.video_note)
            client.media_buffer[message.media_group_id].append({"path": path, "caption": caption})

            if client.media_timers.get(message.media_group_id):
                client.media_timers[message.media_group_id].cancel()

            async def process_media_group(media_group_id: str, owner_id: int, chat_id: int):
                await asyncio.sleep(3)
                entries = client.media_buffer.pop(media_group_id, [])
                client.media_timers.pop(media_group_id, None)
                if not entries:
                    return
                files = [Path(e["path"]) for e in entries if e.get("path")]
                caption_text = ""
                for e in entries:
                    if e.get("caption"):
                        caption_text = e.get("caption")
                        break
                send_prompt = caption_text or "."
                try:
                    gem_client = await get_client()
                except Exception as e:
                    await send_reply(client.send_message, [chat_id, f"❌ Gemini client error: {e}"], {}, client)
                    for p in files:
                        try:
                            if p.exists():
                                os.remove(p)
                        except Exception:
                            pass
                    return

                user_gem_id = db.get(GWEB_SETTINGS, f"user_gem.{owner_id}", None)
                default_gem_id = db.get(GWEB_SETTINGS, "default_gem", None)
                gem_to_use = user_gem_id or default_gem_id
                metadata = db.get(GWEB_HISTORY_COLLECTION, f"chat_metadata.{owner_id}", None)
                chat = gem_client.start_chat(metadata=metadata, gem=gem_to_use) if gem_to_use else gem_client.start_chat(metadata=metadata)

                await send_typing_action(client, chat_id, send_prompt)

                try:
                    response = await chat.send_message(send_prompt, files=files if files else None)
                except Exception as e:
                    await send_reply(client.send_message, [chat_id, f"❌ Gemini error: {e}"], {}, client)
                    for p in files:
                        try:
                            if p.exists():
                                os.remove(p)
                        except Exception:
                            pass
                    return

                try:
                    db.set(GWEB_HISTORY_COLLECTION, f"chat_metadata.{owner_id}", chat.metadata)
                except Exception:
                    pass

                bot_response = response.text or "❌ No answer found."
                full_history = db.get(GWEB_HISTORY_COLLECTION, f"chat_history.{owner_id}") or []
                full_history.append(f"Bot: {bot_response}")
                db.set(GWEB_HISTORY_COLLECTION, f"chat_history.{owner_id}", full_history)

                if getattr(response, "images", None):
                    for i, image in enumerate(response.images):
                        try:
                            if isinstance(image, GeneratedImage):
                                file_path_img = os.path.join(TEMP_IMAGE_DIR, f"gweb_gen_{owner_id}_{i}.png")
                                await image.save(path=TEMP_IMAGE_DIR, filename=f"gweb_gen_{owner_id}_{i}.png", verbose=True)
                                await send_reply(client.send_photo, [chat_id, file_path_img], {"reply_to_message_id": message.id, "cleanup_file": file_path_img}, client)
                            elif isinstance(image, WebImage):
                                await send_reply(client.send_photo, [chat_id, image.url], {"reply_to_message_id": message.id}, client)
                        except Exception:
                            pass

                await send_reply(message.reply_text, [bot_response], {"reply_to_message_id": message.id}, client)

                for p in files:
                    try:
                        if p.exists():
                            os.remove(p)
                    except Exception:
                        pass

            client.media_timers[message.media_group_id] = asyncio.create_task(process_media_group(message.media_group_id, user_id, message.chat.id))
            return

        if message.video or message.video_note:
            file_path = await client.download_media(message.video or message.video_note)
            file_type = "video"
        elif message.audio or message.voice:
            file_path = await client.download_media(message.audio or message.voice)
            file_type = "audio"
        elif message.document and message.document.file_name.endswith(".pdf"):
            file_path = await client.download_media(message.document)
            file_type = "pdf"
        elif message.document:
            file_path = await client.download_media(message.document)
            file_type = "document"
        elif message.photo:
            file_path = await client.download_media(message.photo)
            file_type = "image"
        else:
            return

        try:
            gem_client = await get_client()
        except Exception as e:
            await send_reply(client.send_message, [message.chat.id, f"❌ Gemini client error: {e}"], {}, client)
            return

        user_gem_id = db.get(GWEB_SETTINGS, f"user_gem.{user_id}", None)
        default_gem_id = db.get(GWEB_SETTINGS, "default_gem", None)
        gem_to_use = user_gem_id or default_gem_id

        metadata = db.get(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}", None)
        chat = gem_client.start_chat(metadata=metadata, gem=gem_to_use) if gem_to_use else gem_client.start_chat(metadata=metadata)

        prompt_text = caption or "."
        await send_typing_action(client, message.chat.id, prompt_text)
        try:
            response = await chat.send_message(prompt_text, files=[Path(file_path)] if file_path else None)
        except Exception as e:
            await send_reply(client.send_message, [message.chat.id, f"❌ Gemini error: {e}"], {}, client)
            return

        try:
            db.set(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}", chat.metadata)
        except Exception:
            pass

        bot_response = response.text or "❌ No answer found."

        if getattr(response, "images", None):
            for i, image in enumerate(response.images):
                try:
                    if isinstance(image, GeneratedImage):
                        file_path_img = os.path.join(TEMP_IMAGE_DIR, f"gweb_gen_{user_id}_{i}.png")
                        await image.save(path=TEMP_IMAGE_DIR, filename=f"gweb_gen_{user_id}_{i}.png", verbose=True)
                        await send_reply(client.send_photo, [message.chat.id, file_path_img], {"reply_to_message_id": message.id, "cleanup_file": file_path_img}, client)
                    elif isinstance(image, WebImage):
                        await send_reply(client.send_photo, [message.chat.id, image.url], {"reply_to_message_id": message.id}, {}, client)
                except Exception:
                    pass

        await send_reply(message.reply_text, [bot_response], {"reply_to_message_id": message.id}, client)

    except Exception as e:
        await send_reply(client.send_message, ["me", f"gweb file handler error:\n\n{e}"], {}, client)
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


@Client.on_message(filters.command(["gweb", "gw"], prefix) & filters.me)
async def gweb_command(client: Client, message: Message):
    try:
        parts = message.text.strip().split()
        if len(parts) < 2:
            await send_reply(message.edit_text, ["Usage: gweb [on|off|del|all|r] [user_id]"], {}, client)
            return
        command = parts[1].lower()
        user_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else message.chat.id

        global enabled_users, disabled_users, gweb_for_all
        enabled_users = db.get(GWEB_SETTINGS, "enabled_users") or enabled_users
        disabled_users = db.get(GWEB_SETTINGS, "disabled_users") or disabled_users
        gweb_for_all = db.get(GWEB_SETTINGS, "gweb_for_all") or gweb_for_all

        if command == "on":
            if user_id in disabled_users:
                disabled_users.remove(user_id)
                db.set(GWEB_SETTINGS, "disabled_users", disabled_users)
            if user_id not in enabled_users:
                enabled_users.append(user_id)
                db.set(GWEB_SETTINGS, "enabled_users", enabled_users)
            await send_reply(message.edit_text, [f"<spoiler>ON: {user_id}</spoiler>"], {}, client)
        elif command == "off":
            if user_id not in disabled_users:
                disabled_users.append(user_id)
                db.set(GWEB_SETTINGS, "disabled_users", disabled_users)
            if user_id in enabled_users:
                enabled_users.remove(user_id)
                db.set(GWEB_SETTINGS, "enabled_users", enabled_users)
            await send_reply(message.edit_text, [f"<spoiler>OFF: {user_id}</spoiler>"], {}, client)
        elif command == "del":
            db.remove(GWEB_HISTORY_COLLECTION, f"chat_history.{user_id}")
            db.remove(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}")
            await send_reply(message.edit_text, [f"<spoiler>Deleted: {user_id}</spoiler>"], {}, client)
        elif command == "all":
            gweb_for_all = not gweb_for_all
            db.set(GWEB_SETTINGS, "gweb_for_all", gweb_for_all)
            await send_reply(message.edit_text, [f"All: {'enabled' if gweb_for_all else 'disabled'}"], {}, client)
        elif command == "r":
            changed = False
            if user_id in enabled_users:
                enabled_users.remove(user_id)
                db.set(GWEB_SETTINGS, "enabled_users", enabled_users)
                changed = True
            if user_id in disabled_users:
                disabled_users.remove(user_id)
                db.set(GWEB_SETTINGS, "disabled_users", disabled_users)
                changed = True
            await send_reply(
                message.edit_text,
                [f"<spoiler>Removed: {user_id}</spoiler>" if changed else f"<spoiler>Not found: {user_id}</spoiler>"], {}, client)
        else:
            await send_reply(message.edit_text, ["Usage: gweb [on|off|del|all|r] [user_id]"], {}, client)

        await send_reply(message.delete, [], {}, client)
    except Exception as e:
        await send_reply(client.send_message, ["me", f"gweb command error:\n\n{e}"], {}, client)


@Client.on_message(filters.command(["setgw", "setgweb"], prefix) & filters.me)
async def setgw_command(client: Client, message: Message):
    try:
        parts = message.text.strip().split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else None
        gem_client = None

        if sub is None:
            try:
                gem_client = await get_client()
                await gem_client.fetch_gems(include_hidden=True)
                gems = list(gem_client.gems)
                if not gems:
                    await message.edit_text("No gems found for this account.")
                    return
                lines = []
                for i, g in enumerate(gems, start=1):
                    gid = getattr(g, "id", "") or ""
                    lines.append(f"{i}. {g.name}  —  {gid}")
                await message.edit_text("Available gems:\n\n" + "\n".join(lines))
            except Exception as e:
                await message.edit_text(f"Failed to fetch gems: {e}")
            return

        if sub == "role":
            if len(parts) == 2:
                current = db.get(GWEB_SETTINGS, "default_gem") or "None"
                try:
                    gem_client = await get_client()
                    await gem_client.fetch_gems(include_hidden=True)
                    gem_obj = gem_client.gems.get(id=current) if current != "None" else None
                    name = gem_obj.name if gem_obj else current
                    await message.edit_text(f"Current default gem: {name}")
                except Exception:
                    await message.edit_text(f"Current default gem id: {current}")
                return

            gem_identifier = parts[2].strip()
            try:
                gem_client = await get_client()
                await gem_client.fetch_gems(include_hidden=True)
                gem_obj = None
                try:
                    gem_obj = gem_client.gems.get(id=gem_identifier)
                except Exception:
                    gem_obj = None
                if not gem_obj:
                    for g in gem_client.gems:
                        if g.name and g.name.strip().lower() == gem_identifier.strip().lower():
                            gem_obj = g
                            break
                if not gem_obj:
                    await message.edit_text(f"Gem not found: {gem_identifier}")
                    return
                db.set(GWEB_SETTINGS, "default_gem", gem_obj.id)
                await message.edit_text(f"Default gem set to: {gem_obj.name} ({gem_obj.id})")
            except Exception as e:
                await message.edit_text(f"Failed to set default gem: {e}")
            return

        await message.edit_text("Usage:\n- setgw -> list gems\n- setgw role <GemNameOrId> -> set default gem\n- setgw role -> show current default")
    except Exception as e:
        await send_reply(client.send_message, ["me", f"setgw error:\n\n{e}"], {}, client)


modules_help["gweb"] = {
    "gweb on/off/del/all/r [user_id]": "Manage gweb auto-replies for users.",
    "setgw / setgweb": "List gems and set global default gem. Usage: setgw, setgw role <GemNameOrId>, setgw role",
    "Auto-reply to private messages": "Uses gemini_webapi (cookie-based) to reply and saves per-user chat metadata. Supports buffered messages, sticker/GIF buffering, typing actions and sending images returned by Gemini.",
}
