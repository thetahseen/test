import asyncio
import os
import random
from collections import defaultdict
from pathlib import Path
from typing import List, Optional, Dict

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

_enabled_users = db.get(GWEB_SETTINGS, "enabled_users") or []
_disabled_users = db.get(GWEB_SETTINGS, "disabled_users") or []
_gweb_for_all = db.get(GWEB_SETTINGS, "gweb_for_all") or False

_reply_queue: asyncio.Queue = asyncio.Queue()
_reply_worker_started = False

_smileys = ["-.-", "):", ":)", "*.*", ")*", ";)"]
_sticker_buffer = defaultdict(list)
_sticker_timers: Dict[int, asyncio.Task] = {}

_gem_client = None
_gem_client_lock = asyncio.Lock()
_user_locks: Dict[int, asyncio.Lock] = {}


async def _reply_worker(py_client: Client):
    while True:
        reply_func, args, kwargs = await _reply_queue.get()
        cleanup_file = kwargs.pop("cleanup_file", None)
        try:
            try:
                await reply_func(*args, **kwargs)
            except FloodWait as e:
                try:
                    await py_client.send_message("me", f"FloodWait: sleeping {e.value}s")
                except Exception:
                    pass
                await asyncio.sleep(e.value + 1)
                await reply_func(*args, **kwargs)
        except Exception as e:
            try:
                await py_client.send_message("me", f"Reply queue error:\n{e}")
            except Exception:
                pass
        finally:
            if cleanup_file:
                try:
                    if os.path.exists(cleanup_file):
                        os.remove(cleanup_file)
                except Exception:
                    pass
        await asyncio.sleep(2.1)


def _ensure_reply_worker(py_client: Client):
    global _reply_worker_started
    if not _reply_worker_started:
        asyncio.create_task(_reply_worker(py_client))
        _reply_worker_started = True


async def _queue_reply(reply_func, args, kwargs, py_client: Client):
    _ensure_reply_worker(py_client)
    if isinstance(args, tuple):
        args = list(args)
    await _reply_queue.put((reply_func, args, kwargs))


async def _safe_send_to_me(py_client: Client, text: str):
    try:
        await _queue_reply(py_client.send_message, ["me", text], {}, py_client)
    except Exception:
        pass


async def _typing_task(py_client: Client, chat_id: int):
    try:
        try:
            await py_client.send_chat_action(chat_id=chat_id, action=enums.ChatAction.TYPING)
        except Exception:
            pass
        while True:
            await asyncio.sleep(4)
            try:
                await py_client.send_chat_action(chat_id=chat_id, action=enums.ChatAction.TYPING)
            except Exception:
                pass
    except asyncio.CancelledError:
        return


async def _get_gem_client():
    global _gem_client
    async with _gem_client_lock:
        if _gem_client:
            return _gem_client
        try:
            _gem_client = await get_client()
            return _gem_client
        except Exception as e:
            _gem_client = None
            raise


async def _start_chat_for_user(gem_client, user_id: int):
    user_gem = db.get(GWEB_SETTINGS, f"user_gem.{user_id}", None)
    default_gem = db.get(GWEB_SETTINGS, "default_gem", None)
    gem_to_use = user_gem or default_gem
    meta = db.get(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}", None)
    try:
        if gem_to_use is not None:
            try:
                await gem_client.fetch_gems(include_hidden=True)
            except Exception:
                pass
        chat = gem_client.start_chat(metadata=meta, gem=gem_to_use) if gem_to_use else gem_client.start_chat(metadata=meta)
        return chat
    except Exception:
        try:
            db.remove(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}")
        except Exception:
            pass
        return gem_client.start_chat(gem=gem_to_use) if gem_to_use else gem_client.start_chat()


async def _send_to_gemini(
    py_client: Client,
    user_id: int,
    chat_id: int,
    prompt: str,
    files: Optional[List[Path]],
    reply_to: Optional[int],
):
    lock = _user_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        try:
            gem_client = await _get_gem_client()
        except Exception as e:
            await _safe_send_to_me(py_client, f"❌ Gemini client error: {e}")
            return

        chat = await _start_chat_for_user(gem_client, user_id)

        files_for_gem = None
        if files:
            files_for_gem = [str(p) for p in files]

        typing = asyncio.create_task(_typing_task(py_client, chat_id))
        try:
            try:
                response = await chat.send_message(prompt or ".", files=files_for_gem)
                await asyncio.sleep(0.25)
            except Exception as e:
                err_text = str(e)
                await _safe_send_to_me(py_client, f"❌ Gemini send error (will retry once): {err_text}")
                try:
                    db.remove(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}")
                except Exception:
                    pass
                try:
                    chat = gem_client.start_chat()
                    response = await chat.send_message(prompt or ".", files=files_for_gem)
                except Exception as e2:
                    await _safe_send_to_me(py_client, f"❌ Gemini send failed after retry: {e2}")
                    if files:
                        for p in files:
                            try:
                                if p.exists():
                                    os.remove(p)
                            except Exception:
                                pass
                    return
        finally:
            typing.cancel()
            try:
                await typing
            except Exception:
                pass

        try:
            db.set(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}", chat.metadata)
        except Exception:
            pass

        bot_response = response.text or ""

        if getattr(response, "images", None):
            for i, image in enumerate(response.images):
                try:
                    if isinstance(image, GeneratedImage):
                        fp = os.path.join(TEMP_IMAGE_DIR, f"gweb_gen_{user_id}_{i}.png")
                        await image.save(path=TEMP_IMAGE_DIR, filename=f"gweb_gen_{user_id}_{i}.png", verbose=True)
                        await _queue_reply(py_client.send_photo, [chat_id, fp], {"reply_to_message_id": reply_to, "cleanup_file": fp}, py_client)
                    elif isinstance(image, WebImage):
                        await _queue_reply(py_client.send_photo, [chat_id, image.url], {"reply_to_message_id": reply_to}, py_client)
                except Exception:
                    pass

        kwargs = {"reply_to_message_id": reply_to} if reply_to else {}
        await _queue_reply(py_client.send_message, [chat_id, bot_response], kwargs, py_client)

        if files:
            for p in files:
                try:
                    if p.exists():
                        os.remove(p)
                except Exception:
                    pass


async def _download_media_from_message(py_client: Client, message: Message) -> (List[Path], str):
    files: List[Path] = []
    caption = message.caption.strip() if message.caption else ""
    for attr in ["document", "audio", "video", "voice", "video_note"]:
        media_obj = getattr(message, attr, None)
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
                await message.download(path)
                files.append(Path(path))
            except Exception:
                pass
            return files, caption

    if message.photo:
        path = os.path.join(TEMP_FILE_DIR, f"{message.photo.file_unique_id}.jpg")
        try:
            await message.download(path)
            files.append(Path(path))
        except Exception:
            pass
    return files, caption


@Client.on_message((filters.sticker | filters.animation) & filters.private & ~filters.me & ~filters.bot, group=1)
async def _sticker_handler(client: Client, message: Message):
    user = message.from_user
    if not user:
        return
    user_id = user.id
    global _enabled_users, _disabled_users, _gweb_for_all
    _enabled_users = db.get(GWEB_SETTINGS, "enabled_users") or _enabled_users
    _disabled_users = db.get(GWEB_SETTINGS, "disabled_users") or _disabled_users
    _gweb_for_all = db.get(GWEB_SETTINGS, "gweb_for_all") or _gweb_for_all

    if user_id in _disabled_users or (not _gweb_for_all and user_id not in _enabled_users):
        return

    meta = db.get(GWEB_HISTORY_COLLECTION, f"chat_metadata.{user_id}", None)
    if meta is None:
        try:
            await _send_to_gemini(client, user_id, message.chat.id, "Hello", None, None)
        except Exception as e:
            await _safe_send_to_me(client, f"❌ gweb sticker seed error: {e}")
        return

    _sticker_buffer[user_id].append(message)
    if _sticker_timers.get(user_id):
        _sticker_timers[user_id].cancel()

    async def _process_sticker_buffer(uid: int):
        await asyncio.sleep(8)
        msgs = _sticker_buffer.pop(uid, [])
        _sticker_timers.pop(uid, None)
        if not msgs:
            return
        last_msg = msgs[-1]
        await asyncio.sleep(random.uniform(2, 6))
        try:
            await _queue_reply(client.send_message, [last_msg.chat.id, random.choice(_smileys)], {}, client)
        except Exception:
            pass

    _sticker_timers[user_id] = asyncio.create_task(_process_sticker_buffer(user_id))


@Client.on_message(filters.text & filters.private & ~filters.me & ~filters.bot, group=1)
async def _text_handler(client: Client, message: Message):
    user = message.from_user
    if not user:
        return
    user_id = user.id
    user_name = user.first_name or "User"
    global _enabled_users, _disabled_users, _gweb_for_all
    _enabled_users = db.get(GWEB_SETTINGS, "enabled_users") or _enabled_users
    _disabled_users = db.get(GWEB_SETTINGS, "disabled_users") or _disabled_users
    _gweb_for_all = db.get(GWEB_SETTINGS, "gweb_for_all") or _gweb_for_all

    if user_id in _disabled_users or (not _gweb_for_all and user_id not in _enabled_users):
        return

    if not hasattr(client, "gweb_buffer"):
        client.gweb_buffer = {}
        client.gweb_timers = {}

    if user_id not in client.gweb_buffer:
        client.gweb_buffer[user_id] = []
        client.gweb_timers[user_id] = None

    client.gweb_buffer[user_id].append(message.text.strip())

    if client.gweb_timers[user_id]:
        client.gweb_timers[user_id].cancel()

    async def _process_text_buffer(msg: Message):
        await asyncio.sleep(DEFAULT_HISTORY_COMBINE_SECONDS)
        texts = client.gweb_buffer.pop(user_id, [])
        client.gweb_timers[user_id] = None
        if not texts:
            return
        combined = "\n".join(t.strip() for t in texts if t and t.strip())
        await asyncio.sleep(random.choice([1, 2, 3]))
        files = []
        if message.reply_to_message:
            files, cap = await _download_media_from_message(client, message.reply_to_message)
            if cap:
                combined = f"{cap}\n\n{combined}"
        reply_to = message.reply_to_message.id if message.reply_to_message and files else None
        await _send_to_gemini(client, user_id, message.chat.id, combined or ".", files or None, reply_to)

    client.gweb_timers[user_id] = asyncio.create_task(_process_text_buffer(message))


@Client.on_message(filters.private & ~filters.me & ~filters.bot, group=1)
async def _file_handler(client: Client, message: Message):
    user = message.from_user
    if not user:
        return
    user_id = user.id
    user_name = user.first_name or "User"
    global _enabled_users, _disabled_users, _gweb_for_all
    _enabled_users = db.get(GWEB_SETTINGS, "enabled_users") or _enabled_users
    _disabled_users = db.get(GWEB_SETTINGS, "disabled_users") or _disabled_users
    _gweb_for_all = db.get(GWEB_SETTINGS, "gweb_for_all") or _gweb_for_all

    if user_id in _disabled_users or (not _gweb_for_all and user_id not in _enabled_users):
        return

    caption = message.caption.strip() if message.caption else ""

    if message.media_group_id:
        if not hasattr(client, "media_buffer"):
            client.media_buffer = defaultdict(list)
            client.media_timers = {}

        if message.photo:
            path = await client.download_media(message.photo)
        else:
            path = await client.download_media(message.document or message.video or message.audio or message.voice or message.video_note)

        client.media_buffer[message.media_group_id].append({"path": path, "caption": caption, "reply_to": message.id, "owner": user_id, "chat_id": message.chat.id})

        if client.media_timers.get(message.media_group_id):
            client.media_timers[message.media_group_id].cancel()

        async def _process_media_group(media_group_id: str):
            await asyncio.sleep(3)
            entries = client.media_buffer.pop(media_group_id, [])
            client.media_timers.pop(media_group_id, None)
            if not entries:
                return
            files = [Path(e["path"]) for e in entries if e.get("path")]
            reply_to = entries[0].get("reply_to")
            owner = entries[0].get("owner")
            chat_id = entries[0].get("chat_id")
            caption_text = ""
            for e in entries:
                if e.get("caption"):
                    caption_text = e.get("caption")
                    break
            prompt = caption_text or "."
            await _send_to_gemini(client, owner, chat_id, prompt, files, reply_to)

        client.media_timers[message.media_group_id] = asyncio.create_task(_process_media_group(message.media_group_id))
        return

    file_path = None
    try:
        if message.video or message.video_note:
            file_path = await client.download_media(message.video or message.video_note)
        elif message.audio or message.voice:
            file_path = await client.download_media(message.audio or message.voice)
        elif message.document and message.document.file_name.endswith(".pdf"):
            file_path = await client.download_media(message.document)
        elif message.document:
            file_path = await client.download_media(message.document)
        elif message.photo:
            file_path = await client.download_media(message.photo)
        else:
            return
    except Exception as e:
        await _safe_send_to_me(client, f"❌ media download error: {e}")
        return

    files = [Path(file_path)] if file_path else None
    prompt = caption or "."
    await _send_to_gemini(client, user_id, message.chat.id, prompt, files, message.id)


@Client.on_message(filters.command(["gwrole"], prefix) & filters.me)
async def _gwrole(client: Client, message: Message):
    try:
        parts = message.text.strip().split(maxsplit=1)
        target_id = message.chat.id
        if len(parts) == 1:
            db.remove(GWEB_SETTINGS, f"user_gem.{target_id}")
            await _queue_reply(message.edit_text, [f"Cleared gem for this chat. Now using global default."], {}, client)
            return
        gem_identifier = parts[1].strip()
        try:
            gem_client = await _get_gem_client()
            await gem_client.fetch_gems(include_hidden=True)
        except Exception as e:
            await _safe_send_to_me(client, f"❌ failed to get gem client: {e}")
            return
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
            await _queue_reply(message.edit_text, [f"Gem not found: {gem_identifier}"], {}, client)
            return
        db.set(GWEB_SETTINGS, f"user_gem.{target_id}", gem_obj.id)
        await _queue_reply(message.edit_text, [f"Gem for this chat set to: {gem_obj.name} ({gem_obj.id})"], {}, client)
    except Exception as e:
        await _safe_send_to_me(client, f"gwrole error:\n\n{e}")


@Client.on_message(filters.command(["gweb", "gw"], prefix) & filters.me)
async def _gweb_admin(client: Client, message: Message):
    try:
        parts = message.text.strip().split()
        if len(parts) < 2:
            await _queue_reply(message.edit_text, ["Usage: gweb [on|off|del|all|r] [user_id]"], {}, client)
            return
        cmd = parts[1].lower()
        target = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else message.chat.id

        global _enabled_users, _disabled_users, _gweb_for_all
        _enabled_users = db.get(GWEB_SETTINGS, "enabled_users") or _enabled_users
        _disabled_users = db.get(GWEB_SETTINGS, "disabled_users") or _disabled_users
        _gweb_for_all = db.get(GWEB_SETTINGS, "gweb_for_all") or _gweb_for_all

        if cmd == "on":
            if target in _disabled_users:
                _disabled_users.remove(target)
                db.set(GWEB_SETTINGS, "disabled_users", _disabled_users)
            if target not in _enabled_users:
                _enabled_users.append(target)
                db.set(GWEB_SETTINGS, "enabled_users", _enabled_users)
            await _queue_reply(message.edit_text, [f"<spoiler>ON: {target}</spoiler>"], {}, client)
        elif cmd == "off":
            if target not in _disabled_users:
                _disabled_users.append(target)
                db.set(GWEB_SETTINGS, "disabled_users", _disabled_users)
            if target in _enabled_users:
                _enabled_users.remove(target)
                db.set(GWEB_SETTINGS, "enabled_users", _enabled_users)
            await _queue_reply(message.edit_text, [f"<spoiler>OFF: {target}</spoiler>"], {}, client)
        elif cmd == "del":
            db.remove(GWEB_HISTORY_COLLECTION, f"chat_metadata.{target}")
            await _queue_reply(message.edit_text, [f"<spoiler>Deleted: {target}</spoiler>"], {}, client)
        elif cmd == "all":
            _gweb_for_all = not _gweb_for_all
            db.set(GWEB_SETTINGS, "gweb_for_all", _gweb_for_all)
            await _queue_reply(message.edit_text, [f"All: {'enabled' if _gweb_for_all else 'disabled'}"], {}, client)
        elif cmd == "r":
            changed = False
            if target in _enabled_users:
                _enabled_users.remove(target)
                db.set(GWEB_SETTINGS, "enabled_users", _enabled_users)
                changed = True
            if target in _disabled_users:
                _disabled_users.remove(target)
                db.set(GWEB_SETTINGS, "disabled_users", _disabled_users)
                changed = True
            await _queue_reply(message.edit_text, [f"<spoiler>Removed: {target}</spoiler>" if changed else f"<spoiler>Not found: {target}</spoiler>"], {}, client)
        else:
            await _queue_reply(message.edit_text, ["Usage: gweb [on|off|del/all/r] [user_id]"], {}, client)

        await _queue_reply(message.delete, [], {}, client)
    except Exception as e:
        await _safe_send_to_me(client, f"gweb command error:\n\n{e}")


@Client.on_message(filters.command(["setgw", "setgweb"], prefix) & filters.me)
async def _setgw(client: Client, message: Message):
    try:
        tokens = message.text.strip().split()
        sub = tokens[1].lower() if len(tokens) > 1 else None

        if sub is None:
            try:
                gem_client = await _get_gem_client()
                await gem_client.fetch_gems(include_hidden=True)
                all_gems = list(gem_client.gems)
                custom_gems = [g for g in all_gems if not getattr(g, "predefined", False)]
                if not custom_gems:
                    await message.edit_text("No custom gems found for this account.")
                    return
                default_id = db.get(GWEB_SETTINGS, "default_gem") or ""
                lines = []
                for i, g in enumerate(custom_gems, start=1):
                    gid = getattr(g, "id", "") or ""
                    marker = " (default)" if gid == default_id else ""
                    name = getattr(g, "name", "") or gid
                    lines.append(f"{i}. {name}  —  {gid}{marker}")
                await message.edit_text("Available custom gems:\n\n" + "\n".join(lines))
            except Exception as e:
                await message.edit_text(f"Failed to fetch gems: {e}")
            return

        if sub == "role":
            if len(tokens) == 2:
                current = db.get(GWEB_SETTINGS, "default_gem") or "None"
                try:
                    gem_client = await _get_gem_client()
                    await gem_client.fetch_gems(include_hidden=True)
                    gem_obj = gem_client.gems.get(id=current) if current != "None" else None
                    name = gem_obj.name if gem_obj else current
                    await message.edit_text(f"Current default gem: {name}")
                except Exception:
                    await message.edit_text(f"Current default gem id: {current}")
                return

            gem_identifier = " ".join(tokens[2:])
            try:
                gem_client = await _get_gem_client()
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

        await message.edit_text("Usage:\n- setgw -> list gems\n- setgw role <GemNameOrId> -> set global default\n- setgw role -> show current default")
    except Exception as e:
        await _safe_send_to_me(client, f"setgw error:\n\n{e}")


modules_help["gweb"] = {
    "gweb on/off/del/all/r [user_id]": "Manage gweb auto-replies for users.",
    "setgw / setgweb": "List custom gems and set global default gem or per-chat via gwrole. Usage: setgw, setgw role <GemNameOrId>",
    "Auto-reply to private messages": "Uses gemini_webapi (cookie-based) to reply and saves per-user Gemini chat metadata (no local transcript). Supports buffered messages, sticker/GIF buffering, typing actions and sending images returned by Gemini.",
}
