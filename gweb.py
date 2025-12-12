import asyncio
import os
import random
from collections import defaultdict
from pathlib import Path
from typing import List, Optional

from pyrogram import Client, filters, enums
from pyrogram.types import Message
from pyrogram.errors import FloodWait

from gemini_webapi import GeneratedImage, WebImage

from utils.db import db
from utils.misc import modules_help, prefix
from modules.custom_modules.ai_gemini import get_client, TEMP_IMAGE_DIR, TEMP_FILE_DIR

# Ensure temp dirs exist
os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
os.makedirs(TEMP_FILE_DIR, exist_ok=True)

# Persistence keys / defaults
GWEB_HISTORY_COLLECTION = "custom.gweb"
GWEB_SETTINGS = "custom.gweb_settings"
DEFAULT_HISTORY_COMBINE_SECONDS = 8

# Controls (persisted in DB)
enabled_users = db.get(GWEB_SETTINGS, "enabled_users") or []
disabled_users = db.get(GWEB_SETTINGS, "disabled_users") or []
gweb_for_all = db.get(GWEB_SETTINGS, "gweb_for_all") or False

# Reply queue to handle FloodWait and rate limits (similar to gchat)
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
        # slight cooldown between sends
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


# Sticker/GIF buffer behaviour (send a small emoji/smiley after a short period)
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
        # Slight random delay to look natural
        await asyncio.sleep(random.uniform(2, 6))
        await send_reply(last_msg.reply_text, [random_smiley], {}, client)
    except Exception as e:
        try:
            await client.send_message("me", f"Sticker/GIF buffer error:\n{e}")
        except Exception:
            pass


async def send_typing_action(client: Client, chat_id: int, user_message: str):
    try:
        await client.send_chat_action(chat_id=chat_id, action=enums.ChatAction.TYPING)
        # typing time proportional to message length, capped
        await asyncio.sleep(min(len(user_message) / 10, 5))
    except Exception:
        # ignore errors sending typing
        pass


async def _download_reply_media(replied: Message) -> List[Path]:
    files: List[Path] = []
    if not replied:
        return files

    # first look for common media types (document, audio, video, voice, video_note)
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
            return files  # only the first supported media is used

    # photos (if any)
    if replied.photo:
        path = os.path.join(TEMP_FILE_DIR, f"{replied.photo.file_unique_id}.jpg")
        try:
            await replied.download(path)
            files.append(Path(path))
        except Exception:
            pass

    return files


@Client.on_message(
    (filters.sticker | filters.animation) & filters.private & ~filters.me & ~filters.bot, group=1
)
async def handle_sticker_gif_buffered(client: Client, message: Message):
    # respect enabled/disabled lists and global flag
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
    """
    Buffer messages from the same user for a short time (DEFAULT_HISTORY_COMBINE_SECONDS),
    then send as a single combined prompt to Gemini Web API. Use per-user chat metadata
    so conversations persist. Uses typing action and reply queue to emulate natural behaviour.
    """
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

        # Prepare per-client buffers (attach to client instance)
        if not hasattr(client, "gweb_message_buffer"):
            client.gweb_message_buffer = {}
            client.gweb_message_timers = {}

        if user_id not in client.gweb_message_buffer:
            client.gweb_message_buffer[user_id] = []
            client.gweb_message_timers[user_id] = None

        client.gweb_message_buffer[user_id].append(user_message)

        # reset timer
        if client.gweb_message_timers[user_id]:
            client.gweb_message_timers[user_id].cancel()

        async def process_combined_messages():
            await asyncio.sleep(DEFAULT_HISTORY_COMBINE_SECONDS)
            buffered_messages = client.gweb_message_buffer.pop(user_id, [])
            client.gweb_message_timers[user_id] = None
            if not buffered_messages:
                return
            combined_message = " ".join(buffered_messages)

            # Optionally add small random delay to seem natural
            await asyncio.sleep(random.choice([1, 2, 3]))

            # typing action
            await send_typing_action(client, message.chat.id, combined_message)

            # prepare any replied media if the last message was a reply with media
            files = []
            if message.reply_to_message:
                files = await _download_reply_media(message.reply_to_message)

            # Build an input prompt similar to gchat: include a simple chat history if present
            history = db.get(GWEB_HISTORY_COLLECTION, f"chat_history.{user_id}") or []
            # append user message to history (we store only textual history here to keep DB light)
            history.append(f"{user_name}: {combined_message}")
            db.set(GWEB_HISTORY_COLLECTION, f"chat_history.{user_id}", history)

            # Use gemini_webapi client and per-user metadata
            try:
                gem_client = await get_client()
            except Exception as e:
                await send_reply(client.send_message, [message.chat.id, f"❌ Gemini client error: {e}"], {}, client)
                return

            # resolve gem to use (per-user override or default)
            user_gem_id = db.get(GWEB_SETTINGS, f"user_gem.{user_id}", None)
            default_gem_id = db.get(GWEB_SETTINGS, "default_gem", None)
            gem_to_use = user_gem_id or default_gem_id

            # Ensure gems are fetched (so client.gems exists) if we need to resolve names/validate
            if gem_to_use is not None:
                try:
                    await gem_client.fetch_gems(include_hidden=True)
                except Exception:
                    # ignore fetch errors; starting chat with gem id may still work
                    pass

            metadata = db.get(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}", None)
            chat = gem_client.start_chat(metadata=metadata, gem=gem_to_use) if gem_to_use else gem_client.start_chat(metadata=metadata)

            # send prompt (if no prompt but files exist, ask to describe)
            send_prompt = combined_message or "Please describe the attached media."

            try:
                response = await chat.send_message(send_prompt, files=files if files else None)
            except Exception as e:
                await send_reply(client.send_message, [message.chat.id, f"❌ Gemini error: {e}"], {}, client)
                return

            # persist metadata for user
            try:
                db.set(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}", chat.metadata)
            except Exception:
                pass

            bot_response = response.text or "❌ No answer found."

            # Save response into history
            full_history = db.get(GWEB_HISTORY_COLLECTION, f"chat_history.{user_id}") or []
            full_history.append(f"Bot: {bot_response}")
            db.set(GWEB_HISTORY_COLLECTION, f"chat_history.{user_id}", full_history)

            # Handle images in response
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

            # Finally send textual response (if images already sent, still send text)
            await send_reply(message.reply_text, [bot_response], {"reply_to_message_id": message.id}, client)

            # cleanup downloaded files
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
    """
    Handle files (video, audio, pdfs, documents) sent directly (not as text).
    This mirrors the behavior in ai_gemini/gchat: upload the file to Gemini (gemini_webapi accepts file paths)
    and ask Gemini to process/describe/answer.
    """
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

        # Download and determine file type
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

        # Use gemini_webapi to send file + prompt
        try:
            gem_client = await get_client()
        except Exception as e:
            await send_reply(client.send_message, [message.chat.id, f"❌ Gemini client error: {e}"], {}, client)
            return

        # resolve gem to use (per-user override or default)
        user_gem_id = db.get(GWEB_SETTINGS, f"user_gem.{user_id}", None)
        default_gem_id = db.get(GWEB_SETTINGS, "default_gem", None)
        gem_to_use = user_gem_id or default_gem_id

        metadata = db.get(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}", None)
        chat = gem_client.start_chat(metadata=metadata, gem=gem_to_use) if gem_to_use else gem_client.start_chat(metadata=metadata)

        prompt_text = f"User sent a {file_type}." + (f" Caption: {caption}" if caption else "")
        try:
            response = await chat.send_message(prompt_text, files=[Path(file_path)] if file_path else None)
        except Exception as e:
            await send_reply(client.send_message, [message.chat.id, f"❌ Gemini error: {e}"], {}, client)
            return

        # persist metadata and reply
        try:
            db.set(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}", chat.metadata)
        except Exception:
            pass

        bot_response = response.text or "❌ No answer found."

        # send images if present
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


# Management commands: similar to gchat, allow toggling gweb on/off per user and clearing metadata
@Client.on_message(filters.command(["gweb", "gw"], prefix) & filters.me)
async def gweb_command(client: Client, message: Message):
    """
    Usage:
    gweb on/off/del/all/r [user_id]
    - on: enable auto-reply for a user (or current chat)
    - off: disable auto-reply for a user
    - del: delete chat history and metadata for a user
    - all: toggle gweb_for_all
    - r: remove user from both lists (reset)
    """
    try:
        parts = message.text.strip().split()
        if len(parts) < 2:
            await send_reply(message.edit_text, ["Usage: gweb [on|off|del|all|r] [user_id]"], {}, client)
            return
        command = parts[1].lower()
        user_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else message.chat.id

        # refresh lists from DB
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
                [f"<spoiler>Removed: {user_id}</spoiler>" if changed else f"<spoiler>Not found: {user_id}</spoiler>"],
                {}, client)
        else:
            await send_reply(message.edit_text, ["Usage: gweb [on|off|del|all|r] [user_id]"], {}, client)

        # attempt to delete the management command message after a short interval
        await send_reply(message.delete, [], {}, client)
    except Exception as e:
        await send_reply(client.send_message, ["me", f"gweb command error:\n\n{e}"], {}, client)


@Client.on_message(filters.command(["setgw", "setgweb"], prefix) & filters.me)
async def setgw_command(client: Client, message: Message):
    """
    Manage Gemini 'gems' for gweb.
    - setgw            -> list all gems (requires gem client + fetch_gems)
    - setgw role <GemNameOrId>  -> set global default gem by name or id (applies to all chats)
    - setgw role        -> show current default gem
    Notes:
    - gem ids are stable; storing id is preferred.
    - This command uses the same gemini_webapi client/cookies configured with set_gemini.
    """
    try:
        parts = message.text.strip().split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else None
        gem_client = None

        # If no args, list gems
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
                    # show name and truncated id
                    gid = getattr(g, "id", "") or ""
                    lines.append(f"{i}. {g.name}  —  {gid}")
                await message.edit_text("Available gems:\n\n" + "\n".join(lines))
            except Exception as e:
                await message.edit_text(f"Failed to fetch gems: {e}")
            return

        # Handle "role" subcommand
        if sub == "role":
            # show current default if no gem provided
            if len(parts) == 2:
                current = db.get(GWEB_SETTINGS, "default_gem") or "None"
                # Attempt to resolve id->name if possible
                try:
                    gem_client = await get_client()
                    await gem_client.fetch_gems(include_hidden=True)
                    gem_obj = gem_client.gems.get(id=current) if current != "None" else None
                    name = gem_obj.name if gem_obj else current
                    await message.edit_text(f"Current default gem: {name}")
                except Exception:
                    await message.edit_text(f"Current default gem id: {current}")
                return

            # set default gem (name or id)
            gem_identifier = parts[2].strip()
            try:
                gem_client = await get_client()
                await gem_client.fetch_gems(include_hidden=True)
                # try id exact match first
                gem_obj = None
                try:
                    gem_obj = gem_client.gems.get(id=gem_identifier)
                except Exception:
                    gem_obj = None
                # try name match (case-insensitive)
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

        # unknown subcommand
        await message.edit_text("Usage:\n- setgw -> list gems\n- setgw role <GemNameOrId> -> set default gem\n- setgw role -> show current default")
    except Exception as e:
        await send_reply(client.send_message, ["me", f"setgw error:\n\n{e}"], {}, client)


# Add help entry
modules_help["gweb"] = {
    "gweb on/off/del/all/r [user_id]": "Manage gweb auto-replies for users.",
    "setgw / setgweb": "List gems and set global default gem. Usage: setgw, setgw role <GemNameOrId>, setgw role",
    "Auto-reply to private messages": "Uses gemini_webapi (cookie-based) to reply and saves per-user chat metadata. Supports buffered messages, sticker/GIF buffering, typing actions and sending images returned by Gemini.",
}
