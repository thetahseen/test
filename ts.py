__id__ = "whisper_transcribe"
__name__ = "Transcriber"
__description__ = "Replaces Telegram's transcription service with free state-of-the-art alternatives."
__author__ = "@ginqusPlugins"
__version__ = "2.1.0"
__icon__ = "ginqusExteraPlugins/1"
__min_version__ = "11.12.0"


import requests
import time
from client_utils import run_on_queue, get_user_config, get_last_fragment, get_messages_controller
from hook_utils import find_class, get_private_field, set_private_field
from ui.settings import Header, Input, Divider, Text, Selector, Switch
from base_plugin import BasePlugin, MethodReplacement, MethodHook
from android_utils import run_on_ui_thread, log
from java.util import HashMap, Objects, Locale
from ui.alert import AlertDialogBuilder
from ui.bulletin import BulletinHelper
from android.content import Intent
from android.net import Uri


class LocalizationManager:  # Credits for the logic: Command List plugin by @mihailkotovski
    strings = {
        "en": {
            "header_settings": "Plugin settings",
            "header_warning": "Warning",
            "header_info": "Information",
            "header_other": "Other",
            "settings_provider": "Provider",
            "settings_token": "Token",
            "settings_convert_to_audio": "Send audio only",
            "settings_convert_to_audio_subtext": "Extracts audio from video messages, speeding up the upload process when on slow internet. (EXPERIMENTAL)",
            "warning_mistral_phone": "You need a phone number to get the Mistral token.",
            "info_usage": "Plugin usage",
            "info_provider_choice": "What provider do I choose?",
            "info_pricing": "Pricing",
            "info_get_token": "Get token",
            "pricing_assemblyai": "Free up to 185 hours total, then $0.15/hour.\n\nhttps://www.assemblyai.com/pricing",
            "pricing_deepgram": "Free up to ~300 hours total, then $4.8/hour.\n\nhttps://deepgram.com/pricing",
            "pricing_mistral": "Free. Has a rate limit of 1 request per second.\n\nhttps://mistral.ai/pricing",
            "pricing_gemini": "Free tier available, then pay-as-you-go.\n\nhttps://ai.google.dev/pricing",
            "other_open_channel": "Open channel",
            "alert_close": "Close",
            "alert_open": "Open",
            "alert_provider_choice": "𝗔𝘀𝘀𝗲𝗺𝗯𝗹𝘆𝗔𝗜:\n  • Speed: low (~8 seconds on average)\n  • Quality: best\n\n𝗗𝗲𝗲𝗽𝗴𝗿𝗮𝗺:\n  • Speed: high (~3 seconds on average)\n  • Quality: average (sometimes incorrect punctuation)\n\n𝗠𝗶𝘀𝘁𝗿𝗮𝗹:\n  • Speed: high (~3 seconds on average)\n  • Quality: average (may hallucinate)\n\n𝗚𝗲𝗺𝗶𝗻𝗶:\n  • Speed: high (~3-5 seconds on average)\n  • Quality: best (accurate and contextual)\n\nP.S. These are the results of my tests, yours may vary.",
            "alert_provider_choice_title": "Provider choice",
            "alert_usage_title": "Usage",
            "alert_usage": "To transcribe an audio/video message, just tap the transcribe button next to it. No commands needed.",
            "error_get_audio_path": "Failed to get audio path: ",
            "error_transcribing": "Transcription error: ",
            "error_no_token": "Please specify your token in plugin settings.",
            "error_load_file_timeout": "Couldn't load file: timed out.",
            "error_audio_not_found": "Video stream doesn't have an audio track.",
            "error_extraction": "Error extracting audio: ",
        },
        "ru": {
            "header_settings": "Настройки плагина",
            "header_warning": "Предупреждение",
            "header_info": "Информация",
            "header_other": "Другое",
            "settings_provider": "Провайдер",
            "settings_token": "Токен",
            "settings_convert_to_audio": "Отправлять только аудио",
            "settings_convert_to_audio_subtext": "Конвертирует видеосообщения в аудио, ускоряя отправку при медленном интернете. (ЭКСПЕРИМЕНТАЛЬНО)",
            "warning_mistral_phone": "Для получения токена Mistral нужен номер телефона. Подойдет и российский.",
            "info_usage": "Использование плагина",
            "info_provider_choice": "Какого провайдера выбрать?",
            "info_pricing": "Стоимость",
            "info_get_token": "Получить токен",
            "pricing_assemblyai": "Бесплатно первые 185 часов аудио, затем 0,15$/час.\n\nhttps://www.assemblyai.com/pricing",
            "pricing_deepgram": "Бесплатно первые ~300 часов аудио, затем 4,8$/час.\n\nhttps://deepgram.com/pricing",
            "pricing_mistral": "Бесплатно. Максимум 1 запрос в секунду.\n\nhttps://mistral.ai/pricing",
            "pricing_gemini": "Бесплатная тіера доступна, затем рассчитание по тарифу.\n\nhttps://ai.google.dev/pricing",
            "other_open_channel": "Открыть канал",
            "alert_close": "Закрыть",
            "alert_open": "Открыть",
            "alert_provider_choice": "𝗔𝘀𝘀𝗲𝗺𝗯𝗹𝘆𝗔𝗜:\n  • Скорость: низкая (~8 секунд в среднем)\n  • Качество: лучшее\n\n𝗗𝗲𝗲𝗽𝗴𝗿𝗮𝗺:\n  • Скорость: высокая (~3 секунды в среднем)\n  • Качество: среднее (иногда некорректная пунктуация)\n\n𝗠𝗶𝘀𝘁𝗿𝗮𝗹:\n  • Скорость: высокая (~3 секунды в среднем)\n  • Качество: среднее (может галлюцинировать)\n\n𝗚𝗲𝗺𝗶𝗻𝗶:\n  • Скорость: высокая (~3-5 секунды в среднем)\n  • Качество: лучшее (точное и контекстное)",
            "alert_provider_choice_title": "Выбор провайдера",
            "alert_usage_title": "Использование",
            "alert_usage": "Чтобы расшифровать аудио/видеосообщение, просто нажмите на кнопку расшифровки рядом с ним. Никаких команд.",
            "error_get_audio_path": "Ошибка при скачивании аудиофайла: ",
            "error_transcribing": "Ошибка при расшифровке: ",
            "error_no_token": "Пожалуйста, укажите токен в настройках плагина.",
            "error_load_file_timeout": "Не удалось загрузить файл: время ожидания истекло.",
            "error_audio_not_found": "Видео не содержит аудиодорожки.",
            "error_extraction": "Ошибка при конвертации видео в аудио: ",
        },
    }

    def __init__(self):
        self.language = Locale.getDefault().getLanguage()
        self.language = self.language if self.language in self.strings else "en"

    def get_string(self, key):
        return self.strings[self.language].get(key, self.strings["en"].get(key, key))


localization = LocalizationManager()


class TestPlugin(BasePlugin):
    def on_plugin_load(self):
        TranscribeButton = find_class("org.telegram.ui.Components.TranscribeButton")
        ChatMessageCell = find_class("org.telegram.ui.Cells.ChatMessageCell")

        if TranscribeButton is not None:
            on_tap_method = TranscribeButton.getClass().getDeclaredMethod("onTap")
            update_waveform_method = ChatMessageCell.getClass().getDeclaredMethod("updateWaveform")
        self.hook_method(on_tap_method, self.CreateTranscribeButtonHook(self))
        self.hook_method(update_waveform_method, self.CreateUpdateWaveformHook())

        return super().on_plugin_load()

    def open_pricing_alert(self, view):
        try:
            provider = self.get_setting("provider", 0)

            if provider == 0:
                text = localization.get_string("pricing_assemblyai")
                title = "AssemblyAI"
            elif provider == 1:
                text = localization.get_string("pricing_deepgram")
                title = "Deepgram"
            elif provider == 2:
                text = localization.get_string("pricing_mistral")
                title = "Mistral"
            else:
                text = localization.get_string("pricing_gemini")
                title = "Gemini"
            current_fragment = get_last_fragment()
            if not current_fragment or not current_fragment.getParentActivity():
                return
            context = current_fragment.getParentActivity()
            builder = AlertDialogBuilder(context, AlertDialogBuilder.ALERT_TYPE_MESSAGE)
            builder.set_title(title)
            builder.set_message(text)
            builder.set_positive_button(localization.get_string("alert_close"), lambda b, w: b.dismiss())
            builder.set_neutral_button(localization.get_string("alert_open"), self.open_pricing_page)
            builder.show()
        except Exception as e:
            log(f"[TRANSCRIBER] Error showing pricing alert: {e}")

    def open_model_choice_alert(self, view):
        try:
            current_fragment = get_last_fragment()
            if not current_fragment or not current_fragment.getParentActivity():
                return
            context = current_fragment.getParentActivity()
            builder = AlertDialogBuilder(context, AlertDialogBuilder.ALERT_TYPE_MESSAGE)
            builder.set_title(localization.get_string("alert_provider_choice_title"))
            builder.set_message(localization.get_string("alert_provider_choice"))
            builder.set_positive_button(localization.get_string("alert_close"), lambda b, w: b.dismiss())
            builder.show()
        except Exception as e:
            log(f"[TRANSCRIBER] Error showing model choice alert: {e}")

    def open_usage_alert(self, view):
        try:
            current_fragment = get_last_fragment()
            if not current_fragment or not current_fragment.getParentActivity():
                return
            context = current_fragment.getParentActivity()
            builder = AlertDialogBuilder(context, AlertDialogBuilder.ALERT_TYPE_MESSAGE)
            builder.set_title(localization.get_string("alert_usage_title"))
            builder.set_message(localization.get_string("alert_usage"))
            builder.set_positive_button(localization.get_string("alert_close"), lambda b, w: b.dismiss())
            builder.show()
        except Exception as e:
            log(f"[TRANSCRIBER] Error showing usage alert: {e}")

    def open_pricing_page(self, view, idk):
        provider = self.get_setting("provider", 0)

        current_fragment = get_last_fragment()
        if not current_fragment or not current_fragment.getParentActivity():
            return
        context = current_fragment.getParentActivity()
        if provider == 0:
            uri = "https://www.assemblyai.com/pricing"
        elif provider == 1:
            uri = "https://deepgram.com/pricing"
        elif provider == 2:
            uri = "https://mistral.ai/pricing"
        else:
            uri = "https://ai.google.dev/pricing"
        intent = Intent(Intent.ACTION_VIEW, Uri.parse(uri))
        context.startActivity(intent)

    def open_token_page(self, view):
        provider = self.get_setting("provider", 0)

        current_fragment = get_last_fragment()
        if not current_fragment or not current_fragment.getParentActivity():
            return
        context = current_fragment.getParentActivity()
        if provider == 0:
            uri = "https://www.assemblyai.com/dashboard/api-keys"
        elif provider == 1:
            uri = "https://console.deepgram.com"
        elif provider == 2:
            uri = "https://admin.mistral.ai/organization/api-keys"
        else:
            uri = "https://ai.google.dev/aistudio"
        intent = Intent(Intent.ACTION_VIEW, Uri.parse(uri))
        context.startActivity(intent)

    def open_channel(self, view):
        mc = get_messages_controller()
        current_fragment = get_last_fragment()
        if not current_fragment or not current_fragment.getParentActivity():
            return
        if mc and hasattr(mc, "openByUserName"):
            mc.openByUserName("ginqusPlugins", current_fragment, 0)

    def create_settings(self):
        provider = self.get_setting("provider", 0)
        settings = [
            Header(text=localization.get_string("header_settings")),
            Selector(key="provider", text=localization.get_string("settings_provider"), default=0, items=["AssemblyAI", "Deepgram", "Mistral", "Gemini"], icon="menu_feature_voice"),
        ]
        if provider == 0:
            settings.append(Input(key="token_assemblyai", text=localization.get_string("settings_token"), icon="menu_privacy" if len(self.get_setting("token_assemblyai", "")) > 0 else "menu_unlock"))
        elif provider == 1:
            settings.append(Input(key="token_deepgram", text=localization.get_string("settings_token"), icon="menu_privacy" if len(self.get_setting("token_deepgram", "")) > 0 else "menu_unlock"))
        elif provider == 2:
            settings.append(Input(key="token_mistral", text=localization.get_string("settings_token"), icon="menu_privacy" if len(self.get_setting("token_mistral", "")) > 0 else "menu_unlock"))
        else:
            settings.append(Input(key="token_gemini", text=localization.get_string("settings_token"), icon="menu_privacy" if len(self.get_setting("token_gemini", "")) > 0 else "menu_unlock"))
        settings.append(Switch(key="convert_to_audio", text=localization.get_string("settings_convert_to_audio"), default=True, subtext=localization.get_string("settings_convert_to_audio_subtext"), icon="msg_speed"))
        if provider == 2:
            settings.extend([
                Header(text=localization.get_string("header_warning")),
                Text(text=localization.get_string("warning_mistral_phone"), icon="msg_report", accent=True, on_click=lambda _: run_on_ui_thread(BulletinHelper.show_info(localization.get_string("warning_mistral_phone")))),
            ])
        settings.extend([
            Header(text=localization.get_string("header_info")),
            Text(text=localization.get_string("info_usage"), icon="msg_help", on_click=self.open_usage_alert),
            Text(text=localization.get_string("info_provider_choice"), icon="msg_replace", on_click=self.open_model_choice_alert),
            Text(text=localization.get_string("info_pricing"), icon="menu_feature_paid", on_click=self.open_pricing_alert),
            Text(text=localization.get_string("info_get_token"), icon="msg_openin", on_click=self.open_token_page, accent=True),
            Header(text=localization.get_string("header_other")),
            Text(text=localization.get_string("other_open_channel"), icon="msg_discuss", on_click=self.open_channel, accent=True),
        ])
        return settings

    class CreateTranscribeButtonHook(MethodReplacement):
        def __init__(self, plugin_instance):
            super().__init__()
            self.plugin = plugin_instance

        def replace_hooked_method(self, param):
            self.onTap(param)

        # The logic is mostly identical to TranscribeButton.java but with minor changes
        def onTap(self, param):
            # log("[TRANSCRIBER] onTap() called")
            this = param.thisObject

            parent = get_private_field(this, "parent")

            shouldBeOpen = get_private_field(this, "shouldBeOpen")
            loading = get_private_field(this, "loading")
            selectorDrawable = get_private_field(this, "selectorDrawable")

            RippleDrawable = find_class("android.graphics.drawable.RippleDrawable")
            StateSet = find_class("android.util.StateSet")

            if parent == None:
                return
            set_private_field(this, "clickedToOpen", False)
            processClick = not shouldBeOpen
            toOpen = not shouldBeOpen
            if not shouldBeOpen:
                processClick = not loading
                this.setLoading(True, True)
            else:
                processClick = True
                this.setOpen(False, True)
                this.setLoading(False, True)
            if not RippleDrawable is None and isinstance(selectorDrawable, RippleDrawable):
                try:
                    selectorDrawable.setState(StateSet.NOTHING)
                    parent.invalidate()
                except:
                    pass
            set_private_field(this, "pressed", False)
            if processClick:
                # log("[TRANSCRIBER] processClick is True")
                if toOpen:
                    set_private_field(this, "clickedToOpen", True)

                self.transcribePressed(param, parent.getMessageObject(), toOpen)
                
            # The logic is mostly identical to TranscribeButton.java but with minor changes
        def transcribePressed(self, param, messageObject, open):
            # log("[TRANSCRIBER] transcribePressed() called")
            provider = self.plugin.get_setting("provider", 0)
            if provider == 0:
                token = self.plugin.get_setting("token_assemblyai", "")
            elif provider == 1:
                token = self.plugin.get_setting("token_deepgram", "")
            elif provider == 2:
                token = self.plugin.get_setting("token_mistral", "")
            else:
                token = self.plugin.get_setting("token_gemini", "")

            if token == "":
                run_on_ui_thread(lambda: BulletinHelper.show_error(localization.get_string("error_no_token")))
                return

            TranscribeButton = find_class("org.telegram.ui.Components.TranscribeButton")
            MessagesStorage = find_class("org.telegram.messenger.MessagesStorage")
            DialogObject = find_class("org.telegram.messenger.DialogObject")
            MessagesController = find_class("org.telegram.messenger.MessagesController")
            NotificationCenter = find_class("org.telegram.messenger.NotificationCenter")

            this = param.thisObject
            transcribeOperationsByDialogPosition = get_private_field(this, "transcribeOperationsByDialogPosition")
            transcribeOperationsById = get_private_field(this, "transcribeOperationsById")

            if messageObject == None or messageObject.messageOwner == None or not messageObject.isSent():
                return

            account = messageObject.currentAccount
            peer = MessagesController.getInstance(account).getInputPeer(messageObject.messageOwner.peer_id)
            dialogId = DialogObject.getPeerDialogId(peer)
            messageId = messageObject.messageOwner.id
            if open:
                # log("[TRANSCRIBER] open is True")
                if messageObject.messageOwner.voiceTranscription != None and messageObject.messageOwner.voiceTranscriptionFinal:
                    # log("[TRANSCRIBER] The message already has transcription")
                    TranscribeButton.openVideoTranscription(messageObject)
                    messageObject.messageOwner.voiceTranscriptionOpen = True
                    MessagesStorage.getInstance(account).updateMessageVoiceTranscriptionOpen(dialogId, messageId, messageObject.messageOwner)
                    run_on_ui_thread(lambda: NotificationCenter.getInstance(account).postNotificationName(NotificationCenter.voiceTranscriptionUpdate, messageObject, None, None, True, True))
                else:
                    # log("[TRANSCRIBER] Message doesn't have transcription")
                    id = messageId  # Not ideal, but good enough

                    if transcribeOperationsByDialogPosition == None:
                        set_private_field(this, "transcribeOperationsByDialogPosition", HashMap())
                    transcribeOperationsByDialogPosition = get_private_field(this, "transcribeOperationsByDialogPosition")
                    transcribeOperationsByDialogPosition.put(int(self.reqInfoHash(messageObject)), messageObject)

                    if transcribeOperationsById == None:
                        set_private_field(this, "transcribeOperationsById", HashMap())
                    transcribeOperationsById = get_private_field(this, "transcribeOperationsById")
                    transcribeOperationsById.put(id, messageObject)

                    messageObject.messageOwner.voiceTranscriptionId = id
                    
                    # Fixes loading animation not playing when reopening the chat
                    MessagesStorage.getInstance(account).updateMessageVoiceTranscription(dialogId, messageId, "", messageObject.messageOwner)

                    run_on_queue(lambda: self.transcribe(messageObject, account, dialogId, messageId, id, this))
            else:
                if transcribeOperationsByDialogPosition != None:
                    transcribeOperationsByDialogPosition.remove(int(self.reqInfoHash(messageObject)))
                messageObject.messageOwner.voiceTranscriptionOpen = False
                MessagesStorage.getInstance(account).updateMessageVoiceTranscriptionOpen(dialogId, messageId, messageObject.messageOwner)
                run_on_ui_thread(lambda: NotificationCenter.getInstance(account).postNotificationName(NotificationCenter.voiceTranscriptionUpdate, messageObject, None, None, False, None))
                # Identical to TranscribeButton.java

        def reqInfoHash(self, messageObject):
            if messageObject == None:
                return 0
            return Objects.hash(messageObject.currentAccount, messageObject.getDialogId(), messageObject.getId())

        def transcribe(self, messageObject, account, dialogId, messageId, id, this):
            # log("[TRANSCRIBER] transcribe() called")
            TranscribeButton = find_class("org.telegram.ui.Components.TranscribeButton")
            MessagesStorage = find_class("org.telegram.messenger.MessagesStorage")
            transcribeOperationsByDialogPosition = get_private_field(this, "transcribeOperationsByDialogPosition")
            
            path = self.get_audio_path(messageObject)
            if isinstance(path, Exception):
                run_on_ui_thread(lambda: BulletinHelper.show_error(f"{localization.get_string('error_get_audio_path')}: {path}"))
                log("[TRANSCRIBER] " + str(path))
                self.stop_animations(this, messageObject, account, dialogId, messageId)
                return
                # log("[TRANSCRIBER] Got path")

            if self.plugin.get_setting("convert_to_audio", True) == True and messageObject.type == 5:  # TYPE_ROUND_VIDEO
                path = self.extract_audio_from_mp4(path)
                if isinstance(path, Exception):
                    run_on_ui_thread(lambda: BulletinHelper.show_error(f"{localization.get_string('error_get_audio_path')}: {path}"))
                    log("[TRANSCRIBER] " + str(path))
                    self.stop_animations(this, messageObject, account, dialogId, messageId)
                    return
                    # log("[TRANSCRIBER] Converted to audio")

            text = self.send_transcription_request(path)
            if isinstance(text, Exception):
                run_on_ui_thread(lambda: BulletinHelper.show_error(f"{localization.get_string('error_transcribing')}: {text}"))
                log("[TRANSCRIBER] " + str(path))
                self.stop_animations(this, messageObject, account, dialogId, messageId)
                return
                # log("[TRANSCRIBER] Got text")

            finalText = text
            finalId = id
            TranscribeButton.openVideoTranscription(messageObject)
            messageObject.messageOwner.voiceTranscriptionOpen = True
            messageObject.messageOwner.voiceTranscriptionFinal = True
            MessagesStorage.getInstance(account).updateMessageVoiceTranscription(dialogId, messageId, finalText, messageObject.messageOwner)
            transcribeOperationsByDialogPosition = get_private_field(this, "transcribeOperationsByDialogPosition")
            transcribeOperationsByDialogPosition.remove(self.reqInfoHash(messageObject))
            run_on_ui_thread(lambda: this.finishTranscription(messageObject, finalId, finalText))
            this.showOffTranscribe(messageObject)
            # log("[TRANSCRIBER] Transcription successful")

        def stop_animations(self, this, messageObject, account, messageId):
            transcribeOperationsByDialogPosition = get_private_field(this, "transcribeOperationsByDialogPosition")
            transcribeOperationsById = get_private_field(this, "transcribeOperationsById")
            NotificationCenter = find_class("org.telegram.messenger.NotificationCenter")

            if transcribeOperationsByDialogPosition != None:
                transcribeOperationsByDialogPosition.remove(int(self.reqInfoHash(messageObject)))
            if transcribeOperationsById != None:
                transcribeOperationsById.remove(messageId, messageObject)

            run_on_ui_thread(lambda: NotificationCenter.getInstance(account).postNotificationName(NotificationCenter.voiceTranscriptionUpdate, messageObject, None, None, False, None))

        def get_audio_path(self, msg):
            try:
                UserConfig = find_class("org.telegram.messenger.UserConfig")
                FileLoader = find_class("org.telegram.messenger.FileLoader")

                current_account = UserConfig.selectedAccount
                file_loader = FileLoader.getInstance(current_account)
                file_path_obj = file_loader.getPathToMessage(msg.messageOwner)

                if not file_path_obj.exists():
                    document = msg.messageOwner.media.document
                    file_loader.loadFile(document, msg, 1, 0)

                for _ in range(20):
                    if not file_path_obj.exists():
                        time.sleep(1)
                    else:
                        break
                else:
                    raise TimeoutError(localization.get_string("error_load_file_timeout"))

                return str(file_path_obj.getAbsolutePath())

            except Exception as e:
                return e

        def extract_audio_from_mp4(self, in_path):
            try:
                MediaExtractor = find_class("android.media.MediaExtractor")
                MediaMuxer = find_class("android.media.MediaMuxer")
                MediaFormat = find_class("android.media.MediaFormat")
                ByteBuffer = find_class("java.nio.ByteBuffer")
                BufferInfo = find_class("android.media.MediaCodec$BufferInfo")()

                extractor = MediaExtractor()
                muxer = None

                out_path = in_path.rsplit(".", 1)[0] + ".m4a"

                extractor.setDataSource(in_path)

                audio_track = -1
                for i in range(extractor.getTrackCount()):
                    format = extractor.getTrackFormat(i)
                    mime = format.getString(MediaFormat.KEY_MIME)
                    if mime.startswith("audio/"):
                        audio_track = i
                        break

                if audio_track == -1:
                    raise RuntimeError(localization.get_string("error_audio_not_found"))

                muxer = MediaMuxer(out_path, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)

                extractor.selectTrack(audio_track)
                audio_format = extractor.getTrackFormat(audio_track)
                audio_track_index = muxer.addTrack(audio_format)

                muxer.start()

                buffer = ByteBuffer.allocate(1024 * 1024). # 1MB buffer

                while True:
                    sample_size = extractor.readSampleData(buffer, 0)
                    if sample_size < 0:
                        break

                    BufferInfo.offset = 0
                    BufferInfo.size = sample_size
                    BufferInfo.presentationTimeUs = extractor.getSampleTime()
                    BufferInfo.flags = extractor.getSampleFlags()

                    muxer.writeSampleData(audio_track_index, buffer, BufferInfo)
                    extractor.advance()

                return out_path

            except Exception as e:
                return RuntimeError(str(localization.get_string("error_extraction") + str(e)))

            finally:
                if muxer:
                    muxer.stop()
                    muxer.release()
                extractor.release()

        def send_transcription_request(self, path):
            # log("[TRANSCRIBER] send_transcription_request called")
            provider = self.plugin.get_setting("provider", 0)
            if provider == 0:
                token = self.plugin.get_setting("token_assemblyai", "")
            elif provider == 1:
                token = self.plugin.get_setting("token_deepgram", "")
            elif provider == 2:
                token = self.plugin.get_setting("token_mistral", "")
            else:
                token = self.plugin.get_setting("token_gemini", "")
            try:
                if provider == 0:  # AssemblyAI
                    base_url = "https://api.assemblyai.com"
                    headers = {"authorization": token}

                    with open(path, "rb") as f:
                        response = requests.post(base_url + "/v2/upload", headers=headers, data=f)
                        if response.status_code != 200:
                            raise RuntimeError(f"Failed to upload file: {response.status_code}, Response: {response.text}")
                        upload_url = response.json()["upload_url"]

                    data = {"audio_url": upload_url, "language_detection": True}
                    response = requests.post(base_url + "/v2/transcript", headers=headers, json=data)

                    if response.status_code != 200:
                        raise RuntimeError(f"Failed to start transcription: {response.status_code}, Response: {response.text}")

                    transcript_id = response.json()["id"]
                    polling_endpoint = f"{base_url}/v2/transcript/{transcript_id}"

                    for _ in range(60):
                        transcript = requests.get(polling_endpoint, headers=headers).json()
                        if transcript["status"] == "completed":
                            return str(transcript["text"])
                        elif transcript["status"] == "error":
                            raise RuntimeError(f"{transcript['error']}")
                        else:
                            time.sleep(1)
                    else:
                        raise TimeoutError("Timed out")

                elif provider == 1:  # Doxgram ☠️☠️
                    url = "https://api.deepgram.com/v1/listen?model=nova-3-general&punctuate=true&detect_language=true"
                    headers = {
                        "Authorization": f"Token {token}",
                        "Content-Type": "audio/*",
                    }
                    with open(path, "rb") as audio_file:
                        response = requests.post(url, headers=headers, data=audio_file)

                    if response.status_code != 200:
                        raise RuntimeError(f"{response.status_code}, Response: {response.text}")

                    result = response.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
                    return str(result)

                elif provider == 2:
                    url = "https://api.mistral.ai/v1/audio/transcriptions"
                    headers = {"x-api-key": token}
                    files = {
                        "file": open(path, "rb"),
                        "model": (None, "voxtral-mini-2507"),
                    }
                    response = requests.post(url, headers=headers, files=files)

                    if response.status_code != 200:
                        raise RuntimeError(f"{response.status_code}, Response: {response.text}")

                    transcription = response.json()["text"]
                    return str(transcription)

                else:  # Gemini
                    import os
                    file_size = os.path.getsize(path)
                    file_name = os.path.basename(path)
                    mime_type = 'video/mp4' if path.lower().endswith('.mp4') else 'audio/ogg'

                    headers_start = {
                        "X-Goog-Upload-Protocol": "resumable",
                        "X-Goog-Upload-Command": "start",
                        "X-Goog-Upload-Header-Content-Length": str(file_size),
                        "X-Goog-Upload-Header-Content-Type": mime_type,
                        "Content-Type": "application/json"
                    }
                    json_data_start = {"file": {"display_name": file_name}}
                    start_upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={token}"
                    response_start = requests.post(start_upload_url, headers=headers_start, json=json_data_start, timeout=10)
                    if response_start.status_code != 200:
                        raise RuntimeError(f"Gemini upload start failed: {response_start.text}")

                    upload_url = response_start.headers.get("X-Goog-Upload-Url")

                    headers_upload = {
                        "X-Goog-Upload-Offset": "0",
                        "X-Goog-Upload-Command": "upload, finalize",
                        "Content-Type": mime_type
                    }
                    with open(path, "rb") as f:
                        file_data = f.read()
                    response_upload = requests.post(upload_url, headers=headers_upload, data=file_data, timeout=60)
                    if response_upload.status_code != 200:
                        raise RuntimeError(f"Gemini file upload failed: {response_upload.text}")

                    upload_result_json = response_upload.json()
                    uploaded_file_name = upload_result_json.get("file", {}).get("name")

                    file_api_url = f"https://generativelanguage.googleapis.com/v1beta/{uploaded_file_name}?key={token}"
                    for _ in range(15):
                        response_get = requests.get(file_api_url, timeout=10)
                        if response_get.status_code != 200:
                            raise RuntimeError(f"File status check error: {response_get.status_code}")
                        file_state = response_get.json().get("state")
                        if file_state == "ACTIVE":
                            break
                        if file_state == "FAILED":
                            raise RuntimeError(f"File processing failed on server: {response_get.json()}")
                        time.sleep(1)
                    else:
                        raise RuntimeError("File did not become ACTIVE in time.")

                    file_uri = upload_result_json.get("file", {}).get("uri")
                    generate_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent?key={token}"
                    safety_settings = [
                        {"category": c, "threshold": "BLOCK_NONE"}
                        for c in ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]
                    ]
                    prompt = """Your task is to create an accurate transcript of the audio recording. Follow these rules strictly:

1. **VERBATIM:** Transcribe the speech word for word exactly as spoken.
2. **PRESERVE IMPERFECTIONS:** Keep all filler words (like: um, uh, you know, like), hesitations (uh, um, er), word repetitions, and any self-corrections by the speaker.
3. **NO EDITING:** Do not correct any grammatical, stylistic, or speech errors.
4. **NO ADDITIONS:** Do not add any words, comments, explanations, or descriptions of background sounds (e.g., [laughter], [noise]).
5. **NO FORMATTING:** The entire text should be plain text without paragraphs, headings, lists, or any formatting (bold, italics).

The final result should be a raw, unprocessed textual copy of the spoken words."""
                    json_data_generate = {
                        "contents": [
                            {
                                "parts": [
                                    {"text": prompt},
                                    {"fileData": {"mimeType": mime_type, "fileUri": file_uri}}
                                ]
                            }
                        ],
                        "safetySettings": safety_settings
                    }
                    response_generate = requests.post(generate_url, json=json_data_generate, timeout=30)
                    try:
                        return response_generate.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    except (KeyError, IndexError):
                        raise RuntimeError(f"Unexpected Gemini response: {response_generate.json()}")

            except Exception as e:
                return e

    class CreateUpdateWaveformHook(MethodHook):
        def after_hooked_method(self, param):
            try:
                this = param.thisObject
                set_private_field(this, "useTranscribeButton", True)
            except Exception as e:
                BulletinHelper.show_info(str(e))
