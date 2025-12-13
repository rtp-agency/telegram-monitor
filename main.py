import os
import asyncio
import json
from datetime import datetime, timedelta
from collections import defaultdict
from telethon import TelegramClient, events
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.errors import FloodWaitError
from dotenv import load_dotenv

load_dotenv()

# Конфигурация
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
MAIN_GROUP_ID = int(os.getenv('MAIN_GROUP_ID', 0))
MAIN_ADMIN_ID = int(os.getenv('MAIN_ADMIN_ID'))
DATA_FILE = 'bot_data.json'

# Глобальное хранилище
bot_data = {
    'accounts': {},
    'admins': set([MAIN_ADMIN_ID]),
    'daily_stats': {},
    'pending_verifications': {},  # {session_name: phone_code_hash}
    'message_cache': {}  # {session_name: {msg_id: message_data}}
}

# Клиенты
user_clients = {}
bot = None

# Загрузка/сохранение данных
def load_data():
    global bot_data
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            bot_data['accounts'] = data.get('accounts', {})
            bot_data['admins'] = set(data.get('admins', [MAIN_ADMIN_ID]))
            bot_data['daily_stats'] = data.get('daily_stats', {})
            for acc in bot_data['accounts'].values():
                if 'dialogs' in acc:
                    acc['dialogs'] = set(acc['dialogs'])
    except FileNotFoundError:
        save_data()

def save_data():
    data_to_save = {
        'accounts': {},
        'admins': list(bot_data['admins']),
        'daily_stats': bot_data['daily_stats']
    }
    for name, acc in bot_data['accounts'].items():
        acc_copy = acc.copy()
        if 'dialogs' in acc_copy:
            acc_copy['dialogs'] = list(acc_copy['dialogs'])
        data_to_save['accounts'][name] = acc_copy
    
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data_to_save, f, ensure_ascii=False, indent=2)

def is_admin(user_id):
    return user_id in bot_data['admins']

def get_next_report_time():
    now = datetime.now()
    moscow_offset = timedelta(hours=3)
    moscow_now = now + moscow_offset
    
    report_hours = [4, 8, 12, 16, 20, 0]
    
    for hour in report_hours:
        report_time = moscow_now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if report_time > moscow_now:
            return report_time - moscow_offset
    
    next_report = moscow_now.replace(hour=report_hours[0], minute=0, second=0, microsecond=0) + timedelta(days=1)
    return next_report - moscow_offset

async def send_report(session_name):
    if session_name not in bot_data['accounts']:
        return
    
    acc = bot_data['accounts'][session_name]
    today = datetime.now().strftime('%Y-%m-%d')
    count = bot_data['daily_stats'].get(session_name, {}).get(today, 0)
    
    if 'group_id' in acc and acc['group_id'] and bot:
        try:
            moscow_time = datetime.now() + timedelta(hours=3)
            report_text = f"📊 Отчёт по проекту {session_name}\n"
            report_text += f"📅 Дата: {moscow_time.strftime('%d.%m.%Y')}\n"
            report_text += f"⏰ Время: {moscow_time.strftime('%H:%M')} МСК\n"
            report_text += f"💬 Новых диалогов: {count}\n"
            report_text += f"🕐 Период: с 04:00 МСК"
            
            # Отправляем в топик если указан thread_id
            send_kwargs = {'message': report_text}
            if acc.get('thread_id'):
                send_kwargs['reply_to'] = int(acc['thread_id'])
            
            await bot.send_message(int(acc['group_id']), **send_kwargs)
        except Exception as e:
            print(f"Ошибка отправки отчёта для {session_name}: {e}")
    else:
        print(f"⚠️ Для {session_name} не назначен чат. Используйте /assign_chat")

async def report_scheduler():
    while True:
        try:
            next_report = get_next_report_time()
            wait_seconds = (next_report - datetime.now()).total_seconds()
            
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            
            for session_name in list(bot_data['accounts'].keys()):
                await send_report(session_name)
            
            moscow_now = datetime.now() + timedelta(hours=3)
            if moscow_now.hour == 4:
                today = datetime.now().strftime('%Y-%m-%d')
                for session_name in bot_data['accounts']:
                    if session_name not in bot_data['daily_stats']:
                        bot_data['daily_stats'][session_name] = {}
                    bot_data['daily_stats'][session_name] = {today: bot_data['daily_stats'][session_name].get(today, 0)}
                save_data()
        except Exception as e:
            print(f"Ошибка в планировщике отчётов: {e}")
            await asyncio.sleep(60)

async def create_project_subgroup(session_name):
    # Боты не могут создавать группы через API
    # Пользователь должен вручную создать топик и назначить через /assign_chat
    print(f"⚠️ Для {session_name} нужно вручную создать топик и назначить через /assign_chat")
    return None

async def start_user_client(session_name, api_id, api_hash, phone):
    try:
        client = TelegramClient(f'sessions/{session_name}', api_id, api_hash)
        await client.connect()
        
        if not await client.is_user_authorized():
            result = await client.send_code_request(phone)
            # Сохраняем phone_code_hash для последующей верификации
            bot_data['pending_verifications'][session_name] = {
                'phone_code_hash': result.phone_code_hash,
                'client': client
            }
            return None, "CODE_REQUIRED"
        
        # Инициализация кэша сообщений для этой сессии
        if session_name not in bot_data['message_cache']:
            bot_data['message_cache'][session_name] = {}
        
        # Загружаем существующие диалоги при первом запуске
        acc = bot_data['accounts'].get(session_name, {})
        if 'dialogs' not in acc or not acc.get('initialized'):
            print(f"📥 Загружаем существующие диалоги для {session_name}...")
            acc['dialogs'] = set()
            
            # Получаем все диалоги
            async for dialog in client.iter_dialogs(limit=None):
                # Добавляем только диалоги с сообщениями (исключаем пустые)
                if dialog.message:
                    acc['dialogs'].add(dialog.id)
            
            acc['initialized'] = True
            bot_data['accounts'][session_name] = acc
            save_data()
            print(f"✅ Загружено {len(acc['dialogs'])} существующих диалогов для {session_name}")
        
        # Регистрация обработчиков
        @client.on(events.NewMessage)
        async def message_cache_handler(event):
            try:
                if session_name not in bot_data['accounts']:
                    return
                
                # Сохраняем сообщение в кэш (для отслеживания удалений)
                try:
                    chat = await event.get_chat()
                    if not chat:
                        return
                    
                    chat_id = chat.id
                    msg_id = event.message.id
                    
                    # Сохраняем данные сообщения
                    if session_name not in bot_data['message_cache']:
                        bot_data['message_cache'][session_name] = {}
                    
                    # Используем msg_id как ключ (без chat_id, так как он может быть недоступен при удалении)
                    bot_data['message_cache'][session_name][msg_id] = {
                        'text': event.message.text or '',
                        'media': event.message.media,
                        'message': event.message,
                        'chat_id': chat_id,
                        'chat_name': getattr(chat, 'title', None) or getattr(chat, 'first_name', 'Unknown'),
                        'date': datetime.now()
                    }
                    
                    # Проверяем новый диалог (только входящие)
                    if event.message.out:
                        return
                    
                    acc = bot_data['accounts'][session_name]
                    if 'dialogs' not in acc:
                        acc['dialogs'] = set()
                    
                    # НОВЫЙ ДИАЛОГ только если его НЕТ в существующих
                    if chat_id not in acc['dialogs']:
                        acc['dialogs'].add(chat_id)
                        
                        today = datetime.now().strftime('%Y-%m-%d')
                        if session_name not in bot_data['daily_stats']:
                            bot_data['daily_stats'][session_name] = {}
                        if today not in bot_data['daily_stats'][session_name]:
                            bot_data['daily_stats'][session_name][today] = 0
                        
                        bot_data['daily_stats'][session_name][today] += 1
                        save_data()
                        
                        print(f"📬 Новый диалог для {session_name}: {getattr(chat, 'title', None) or getattr(chat, 'first_name', 'Unknown')}")
                except Exception as e:
                    print(f"Ошибка сохранения сообщения в кэш: {e}")
            except Exception as e:
                print(f"Ошибка кэширования сообщения: {e}")
        
        @client.on(events.MessageDeleted)
        async def deleted_handler(event):
            try:
                if session_name not in bot_data['accounts']:
                    return
                
                acc = bot_data['accounts'][session_name]
                if 'group_id' not in acc or not acc['group_id'] or not bot:
                    return
                
                # Обрабатываем каждое удалённое сообщение
                for msg_id in event.deleted_ids:
                    # Ищем сообщение в кэше по msg_id
                    cached_msg = bot_data['message_cache'].get(session_name, {}).get(msg_id)
                    
                    if not cached_msg:
                        # Сообщение не найдено в кэше, пропускаем
                        print(f"⚠️ Сообщение {msg_id} не найдено в кэше (было до запуска бота)")
                        continue
                    
                    chat_id = cached_msg.get('chat_id', 'Unknown')
                    chat_name = cached_msg.get('chat_name', f'Chat {chat_id}')
                    
                    msg_text = f"🗑️ **Удалённое сообщение**\n\n"
                    msg_text += f"👤 **Из диалога:** {chat_name}\n"
                    msg_text += f"🆔 **ID чата:** `{chat_id}`\n"
                    msg_text += f"📝 **ID сообщения:** `{msg_id}`\n"
                    msg_text += f"⏰ **Время удаления:** {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n"
                    
                    msg_text += f"\n📄 **Содержимое:**\n"
                    if cached_msg['text']:
                        # Ограничиваем длину текста
                        text_content = cached_msg['text']
                        if len(text_content) > 3000:
                            text_content = text_content[:3000] + "... (текст обрезан)"
                        msg_text += f"{text_content}\n"
                    else:
                        msg_text += "_Текст отсутствует_\n"
                    
                    # Отправляем текст
                    send_kwargs = {}
                    if acc.get('thread_id'):
                        send_kwargs['reply_to'] = int(acc['thread_id'])
                    
                    await bot.send_message(int(acc['group_id']), msg_text, **send_kwargs)
                    
                    # Отправляем медиа если есть
                    if cached_msg['media']:
                        try:
                            original_msg = cached_msg['message']
                            media_caption = f"🗑️ Медиа из удалённого сообщения\n👤 Из: {chat_name}\n📝 ID: `{msg_id}`"
                            
                            from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
                            import tempfile
                            import os
                            
                            # Определяем тип медиа и расширение файла
                            is_photo = isinstance(original_msg.media, MessageMediaPhoto)
                            file_ext = '.jpg'
                            is_voice = False
                            is_video_note = False
                            
                            if hasattr(original_msg.media, 'document'):
                                doc = original_msg.media.document
                                mime = doc.mime_type
                                
                                # Получаем расширение из mime_type
                                if '/' in mime:
                                    file_ext = '.' + mime.split('/')[-1]
                                    if file_ext == '.jpeg':
                                        file_ext = '.jpg'
                                
                                # Проверяем атрибуты
                                for attr in doc.attributes:
                                    attr_type = type(attr).__name__
                                    if attr_type == 'DocumentAttributeFilename':
                                        # Используем оригинальное расширение файла
                                        original_name = attr.file_name
                                        if '.' in original_name:
                                            file_ext = '.' + original_name.split('.')[-1]
                                    elif attr_type == 'DocumentAttributeAudio' and hasattr(attr, 'voice') and attr.voice:
                                        is_voice = True
                                        file_ext = '.ogg'
                                    elif attr_type == 'DocumentAttributeVideo':
                                        if hasattr(attr, 'round_message') and attr.round_message:
                                            is_video_note = True
                                            file_ext = '.mp4'
                            
                            # Создаём временный файл с правильным расширением
                            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=file_ext)
                            temp_path = temp_file.name
                            temp_file.close()
                            
                            # Скачиваем файл
                            await client.download_media(original_msg, file=temp_path)
                            
                            # Отправляем
                            send_kwargs = {
                                'caption': media_caption,
                                'reply_to': int(acc['thread_id']) if acc.get('thread_id') else None
                            }
                            
                            if is_voice:
                                # Голосовое сообщение
                                await bot.send_file(
                                    int(acc['group_id']),
                                    temp_path,
                                    voice_note=True,
                                    **send_kwargs
                                )
                            elif is_video_note:
                                # Видео-кружок
                                await bot.send_file(
                                    int(acc['group_id']),
                                    temp_path,
                                    video_note=True,
                                    **send_kwargs
                                )
                            else:
                                # Все остальные типы (фото, видео, документы)
                                # force_document=False позволит Telegram автоматически определить тип
                                await bot.send_file(
                                    int(acc['group_id']),
                                    temp_path,
                                    force_document=False,
                                    **send_kwargs
                                )
                            
                            # Удаляем временный файл
                            try:
                                os.unlink(temp_path)
                            except:
                                pass
                            
                        except Exception as e:
                            print(f"Ошибка отправки медиа: {e}")
                            import traceback
                            traceback.print_exc()
                            # Отправляем уведомление о проблеме с медиа
                            error_msg = f"⚠️ Не удалось отправить медиа из сообщения `{msg_id}`\n"
                            error_msg += f"Тип медиа: {type(cached_msg['media']).__name__}\n"
                            error_msg += f"Ошибка: `{str(e)[:200]}`"
                            await bot.send_message(
                                int(acc['group_id']), 
                                error_msg,
                                reply_to=int(acc['thread_id']) if acc.get('thread_id') else None
                            )
                    
                    # Удаляем сообщение из кэша после обработки
                    del bot_data['message_cache'][session_name][msg_id]
                
                # Очищаем старые сообщения из кэша (старше 7 дней)
                if session_name in bot_data['message_cache']:
                    to_delete = []
                    for msg_id, cached in bot_data['message_cache'][session_name].items():
                        if (datetime.now() - cached['date']).days > 7:
                            to_delete.append(msg_id)
                    
                    for msg_id in to_delete:
                        del bot_data['message_cache'][session_name][msg_id]
                
            except Exception as e:
                print(f"Ошибка обработки удалённого сообщения: {e}")
                import traceback
                traceback.print_exc()
        
        # Запускаем клиент
        await client.catch_up()
        user_clients[session_name] = client
        return client, "OK"
        
    except Exception as e:
        return None, str(e)

def setup_bot_handlers(bot_client):
    @bot_client.on(events.NewMessage(pattern='/start'))
    async def start_handler(event):
        user_id = event.sender_id
        
        if not is_admin(user_id):
            await event.respond("❌ У вас нет доступа к этому боту.")
            return
        
        help_text = """
🤖 **Бот управления аккаунтами**

📝 **Команды:**

**Управление аккаунтами:**
/add_account <название> <api_id> <api_hash> <телефон>
- Добавить новый аккаунт

/login <название>
- Начать процесс авторизации

/code <название> <код>
- Ввести код подтверждения

/password <название> <пароль>
- Ввести пароль 2FA

/remove_account <название>
- Удалить аккаунт

**Управление чатами:**
/assign_chat <название> <chat_id> [thread_id]
- Привязать чат/топик к аккаунту
  Пример: /assign_chat Ваня -1001234567890 52

/unassign_chat <название>
- Отвязать чат от аккаунта

**Информация:**
/list_accounts
- Список всех аккаунтов

/stats [название]
- Статистика по аккаунту (или общая, если без параметра)
  Примеры:
  /stats - общая статистика по всем
  /stats Ваня - статистика по аккаунту Ваня

**Администраторы:**
/add_admin <user_id>
- Добавить админа (только главный админ)

/list_admins
- Список администраторов
"""
        
        await event.respond(help_text)

    @bot_client.on(events.NewMessage(pattern='/add_account'))
    async def add_account_handler(event):
        if not is_admin(event.sender_id):
            await event.respond("❌ Нет доступа.")
            return
        
        try:
            parts = event.text.split(maxsplit=4)
            if len(parts) < 5:
                await event.respond("❌ Формат: /add_account <название> <api_id> <api_hash> <телефон>")
                return
            
            name = parts[1]
            api_id = int(parts[2])
            api_hash = parts[3]
            phone = parts[4]
            
            if name in bot_data['accounts']:
                await event.respond("❌ Аккаунт с таким названием уже существует.")
                return
            
            # Проверяем, авторизован ли уже клиент
            test_client = TelegramClient(f'sessions/{name}', api_id, api_hash)
            await test_client.connect()
            
            if await test_client.is_user_authorized():
                await test_client.disconnect()
                
                # Клиент уже авторизован, просто добавляем
                await event.respond(
                    f"✅ Аккаунт {name} уже авторизован!\n\n"
                    f"Теперь создайте топик в вашей супергруппе и используйте:\n"
                    f"/assign_chat {name} <chat_id>\n\n"
                    f"Чтобы получить chat_id:\n"
                    f"1. Перешлите любое сообщение из топика боту @JsonDumpBot\n"
                    f"2. Найдите message_thread_id (это ID топика)\n"
                    f"3. Используйте формат: -100XXXXXXXXX (ID супергруппы)"
                )
                
                bot_data['accounts'][name] = {
                    'api_id': api_id,
                    'api_hash': api_hash,
                    'phone': phone,
                    'group_id': None,
                    'dialogs': set(),
                    'authorized': True
                }
                save_data()
                
                client, status = await start_user_client(name, api_id, api_hash, phone)
            else:
                # Нужна авторизация
                await test_client.disconnect()
                
                await event.respond(
                    f"🔐 Для аккаунта {name} требуется авторизация.\n\n"
                    f"Используйте команду:\n"
                    f"/login {name}\n\n"
                    f"И следуйте инструкциям."
                )
                
                # Сохраняем предварительные данные
                bot_data['accounts'][name] = {
                    'api_id': api_id,
                    'api_hash': api_hash,
                    'phone': phone,
                    'group_id': None,
                    'dialogs': set(),
                    'authorized': False
                }
                save_data()
                
        except Exception as e:
            await event.respond(f"❌ Ошибка: {e}")

    @bot_client.on(events.NewMessage(pattern='/login'))
    async def login_handler(event):
        if not is_admin(event.sender_id):
            await event.respond("❌ Нет доступа.")
            return
        
        try:
            parts = event.text.split()
            if len(parts) < 2:
                await event.respond("❌ Формат: /login <название>")
                return
            
            name = parts[1]
            
            if name not in bot_data['accounts']:
                await event.respond("❌ Аккаунт не найден. Сначала добавьте его через /add_account")
                return
            
            acc = bot_data['accounts'][name]
            
            # Создаём временный клиент для авторизации
            client = TelegramClient(f'sessions/{name}', acc['api_id'], acc['api_hash'])
            await client.connect()
            
            if await client.is_user_authorized():
                await client.disconnect()
                await event.respond(f"✅ Аккаунт {name} уже авторизован!")
                
                # Запускаем если ещё не запущен
                if name not in user_clients:
                    await start_user_client(name, acc['api_id'], acc['api_hash'], acc['phone'])
                return
            
            # Отправляем код
            result = await client.send_code_request(acc['phone'])
            
            # Сохраняем данные для верификации
            bot_data['pending_verifications'][name] = {
                'phone_code_hash': result.phone_code_hash,
                'client': client
            }
            
            await event.respond(
                f"📱 Код отправлен на номер {acc['phone']}\n\n"
                f"⚡ ВАЖНО: Введите код БЫСТРО (в течение 1-2 минут)!\n\n"
                f"Отправьте команду:\n"
                f"/code {name} <код>\n\n"
                f"Пример: /code {name} 12345\n\n"
                f"⚠️ Если у вас включена 2FA (облачный пароль), после ввода кода будет запрошен пароль."
            )
            
        except Exception as e:
            await event.respond(f"❌ Ошибка: {e}")

    @bot_client.on(events.NewMessage(pattern='/code'))
    async def code_handler(event):
        if not is_admin(event.sender_id):
            await event.respond("❌ Нет доступа.")
            return
        
        try:
            parts = event.text.split()
            if len(parts) < 3:
                await event.respond("❌ Формат: /code <название> <код>")
                return
            
            name = parts[1]
            code = parts[2]
            
            if name not in bot_data['pending_verifications']:
                await event.respond("❌ Нет активной сессии авторизации. Используйте /login сначала.")
                return
            
            acc = bot_data['accounts'][name]
            verification_data = bot_data['pending_verifications'][name]
            client = verification_data['client']
            phone_code_hash = verification_data['phone_code_hash']
            
            try:
                # Пытаемся войти
                await client.sign_in(acc['phone'], code, phone_code_hash=phone_code_hash)
                
                # Успешно!
                await client.disconnect()
                del bot_data['pending_verifications'][name]
                
                acc['authorized'] = True
                save_data()
                
                # Запускаем клиент
                new_client, status = await start_user_client(name, acc['api_id'], acc['api_hash'], acc['phone'])
                
                if status == "OK":
                    await event.respond(
                        f"✅ Аккаунт {name} успешно авторизован и запущен!\n\n"
                        f"Теперь создайте топик в супергруппе и используйте:\n"
                        f"/assign_chat {name} <chat_id>"
                    )
                else:
                    await event.respond(f"⚠️ Авторизация прошла, но ошибка запуска: {status}")
                    
            except Exception as e:
                error_msg = str(e)
                
                # Проверяем, нужен ли 2FA пароль
                if "password" in error_msg.lower() or "2fa" in error_msg.lower():
                    await event.respond(
                        f"🔐 Требуется облачный пароль (2FA).\n\n"
                        f"Отправьте команду:\n"
                        f"/password {name} <ваш_пароль>\n\n"
                        f"Пример: /password {name} mySecretPass123"
                    )
                else:
                    await event.respond(f"❌ Ошибка входа: {e}\n\nПопробуйте /login {name} заново.")
                    if name in bot_data['pending_verifications']:
                        try:
                            await bot_data['pending_verifications'][name]['client'].disconnect()
                        except:
                            pass
                        del bot_data['pending_verifications'][name]
                    
        except Exception as e:
            await event.respond(f"❌ Ошибка: {e}")

    @bot_client.on(events.NewMessage(pattern='/password'))
    async def password_handler(event):
        if not is_admin(event.sender_id):
            await event.respond("❌ Нет доступа.")
            return
        
        try:
            parts = event.text.split(maxsplit=2)
            if len(parts) < 3:
                await event.respond("❌ Формат: /password <название> <пароль>")
                return
            
            name = parts[1]
            password = parts[2]
            
            if name not in bot_data['pending_verifications']:
                await event.respond("❌ Нет активной сессии авторизации. Сначала введите код через /code")
                return
            
            acc = bot_data['accounts'][name]
            verification_data = bot_data['pending_verifications'][name]
            client = verification_data['client']
            
            try:
                # Вводим пароль 2FA
                await client.sign_in(password=password)
                
                # Успешно!
                await client.disconnect()
                del bot_data['pending_verifications'][name]
                
                acc['authorized'] = True
                save_data()
                
                # Запускаем клиент
                new_client, status = await start_user_client(name, acc['api_id'], acc['api_hash'], acc['phone'])
                
                if status == "OK":
                    await event.respond(
                        f"✅ Аккаунт {name} успешно авторизован и запущен!\n\n"
                        f"Теперь создайте топик в супергруппе и используйте:\n"
                        f"/assign_chat {name} <chat_id>"
                    )
                else:
                    await event.respond(f"⚠️ Авторизация прошла, но ошибка запуска: {status}")
                    
            except Exception as e:
                await event.respond(f"❌ Ошибка: {e}\n\nПопробуйте /login {name} заново.")
                if name in bot_data['pending_verifications']:
                    try:
                        await bot_data['pending_verifications'][name]['client'].disconnect()
                    except:
                        pass
                    del bot_data['pending_verifications'][name]
                    
        except Exception as e:
            await event.respond(f"❌ Ошибка: {e}")
        finally:
            # Удаляем сообщение с паролем для безопасности
            try:
                await event.delete()
            except:
                pass

    @bot_client.on(events.NewMessage(pattern='/remove_account'))
    async def remove_account_handler(event):
        if not is_admin(event.sender_id):
            await event.respond("❌ Нет доступа.")
            return
        
        try:
            parts = event.text.split()
            if len(parts) < 2:
                await event.respond("❌ Формат: /remove_account <название>")
                return
            
            name = parts[1]
            
            if name not in bot_data['accounts']:
                await event.respond("❌ Аккаунт не найден.")
                return
            
            if name in user_clients:
                await user_clients[name].disconnect()
                del user_clients[name]
            
            del bot_data['accounts'][name]
            if name in bot_data['daily_stats']:
                del bot_data['daily_stats'][name]
            save_data()
            
            await event.respond(f"✅ Аккаунт {name} удалён.")
            
        except Exception as e:
            await event.respond(f"❌ Ошибка: {e}")

    @bot_client.on(events.NewMessage(pattern='/list_accounts'))
    async def list_accounts_handler(event):
        if not is_admin(event.sender_id):
            await event.respond("❌ Нет доступа.")
            return
        
        if not bot_data['accounts']:
            await event.respond("📋 Нет добавленных аккаунтов.")
            return
        
        text = "📋 **Список аккаунтов:**\n\n"
        for name, acc in bot_data['accounts'].items():
            status = "🟢 Активен" if name in user_clients else "🔴 Неактивен"
            chat_status = "✅ Привязан" if acc.get('group_id') else "⚠️ Не привязан"
            
            text += f"• **{name}** - {status}\n"
            text += f"  📞 {acc['phone']}\n"
            text += f"  💬 Диалогов: {len(acc.get('dialogs', []))}\n"
            text += f"  📊 Чат: {chat_status}\n"
            if acc.get('group_id'):
                text += f"  🆔 Chat ID: `{acc['group_id']}`\n"
            if acc.get('thread_id'):
                text += f"  🧵 Thread ID: `{acc['thread_id']}`\n"
            text += "\n"
        
        await event.respond(text)

    @bot_client.on(events.NewMessage(pattern='/stats'))
    async def stats_handler(event):
        if not is_admin(event.sender_id):
            await event.respond("❌ Нет доступа.")
            return
        
        try:
            parts = event.text.split()
            
            # Если указан конкретный аккаунт
            if len(parts) >= 2:
                name = parts[1]
                
                if name not in bot_data['accounts']:
                    await event.respond("❌ Аккаунт не найден.")
                    return
                
                today = datetime.now().strftime('%Y-%m-%d')
                count = bot_data['daily_stats'].get(name, {}).get(today, 0)
                total_dialogs = len(bot_data['accounts'][name].get('dialogs', []))
                
                text = f"📊 **Статистика {name}**\n\n"
                text += f"📅 Сегодня ({today}):\n"
                text += f"💬 Новых диалогов: {count}\n"
                text += f"📝 Всего диалогов: {total_dialogs}\n"
                
                await event.respond(text)
            else:
                # Общая статистика по всем аккаунтам
                if not bot_data['accounts']:
                    await event.respond("📊 Нет аккаунтов для статистики.")
                    return
                
                today = datetime.now().strftime('%Y-%m-%d')
                
                text = f"📊 **Общая статистика по всем аккаунтам**\n"
                text += f"📅 Дата: {today}\n\n"
                
                total_new_today = 0
                total_all_dialogs = 0
                
                for name, acc in bot_data['accounts'].items():
                    new_today = bot_data['daily_stats'].get(name, {}).get(today, 0)
                    all_dialogs = len(acc.get('dialogs', []))
                    
                    total_new_today += new_today
                    total_all_dialogs += all_dialogs
                    
                    status = "🟢" if name in user_clients else "🔴"
                    text += f"{status} **{name}**\n"
                    text += f"   💬 Новых сегодня: {new_today}\n"
                    text += f"   📝 Всего диалогов: {all_dialogs}\n\n"
                
                text += f"━━━━━━━━━━━━━━━\n"
                text += f"**📈 ИТОГО:**\n"
                text += f"💬 Новых сегодня: **{total_new_today}**\n"
                text += f"📝 Всего диалогов: **{total_all_dialogs}**\n"
                text += f"👥 Аккаунтов: **{len(bot_data['accounts'])}**\n"
                
                await event.respond(text)
            
        except Exception as e:
            await event.respond(f"❌ Ошибка: {e}")

    @bot_client.on(events.NewMessage(pattern='/add_admin'))
    async def add_admin_handler(event):
        if event.sender_id != MAIN_ADMIN_ID:
            await event.respond("❌ Только главный администратор может добавлять других админов.")
            return
        
        try:
            parts = event.text.split()
            if len(parts) < 2:
                await event.respond("❌ Формат: /add_admin <user_id>")
                return
            
            new_admin_id = int(parts[1])
            bot_data['admins'].add(new_admin_id)
            save_data()
            
            await event.respond(f"✅ Пользователь {new_admin_id} добавлен в администраторы.")
            
        except Exception as e:
            await event.respond(f"❌ Ошибка: {e}")

    @bot_client.on(events.NewMessage(pattern='/assign_chat'))
    async def assign_chat_handler(event):
        if not is_admin(event.sender_id):
            await event.respond("❌ Нет доступа.")
            return
        
        try:
            parts = event.text.split()
            if len(parts) < 3:
                await event.respond(
                    "❌ **Формат:** `/assign_chat <название> <chat_id> [thread_id]`\n\n"
                    "**Как получить IDs:**\n\n"
                    "**1. Chat ID (ID супергруппы):**\n"
                    "   • Перешлите сообщение из группы боту @JsonDumpBot\n"
                    "   • Найдите `\"id\": -1001234567890`\n\n"
                    "**2. Thread ID (ID топика, необязательно):**\n"
                    "   • Перешлите сообщение из ТОПИКА боту @JsonDumpBot\n"
                    "   • Найдите `message_thread_id: 52`\n\n"
                    "**Примеры:**\n"
                    "`/assign_chat Ваня -1001234567890` - без топика\n"
                    "`/assign_chat Ваня -1001234567890 52` - с топиком ID 52"
                )
                return
            
            name = parts[1]
            chat_id = parts[2]
            thread_id = parts[3] if len(parts) > 3 else None
            
            # Пытаемся преобразовать в int
            try:
                chat_id_int = int(chat_id)
                thread_id_int = int(thread_id) if thread_id else None
            except:
                await event.respond("❌ Неверный формат ID. Должны быть числа.")
                return
            
            if name not in bot_data['accounts']:
                await event.respond("❌ Аккаунт не найден.")
                return
            
            # Проверяем доступ к чату
            try:
                test_msg_text = f"✅ Чат успешно привязан к аккаунту **{name}**!"
                if thread_id_int:
                    test_msg_text += f"\n🧵 Топик ID: {thread_id_int}"
                
                # Отправляем тестовое сообщение
                send_kwargs = {'message': test_msg_text}
                if thread_id_int:
                    send_kwargs['reply_to'] = thread_id_int
                
                await bot.send_message(chat_id_int, **send_kwargs)
                
                # Сохраняем настройки
                bot_data['accounts'][name]['group_id'] = chat_id_int
                bot_data['accounts'][name]['thread_id'] = thread_id_int
                save_data()
                
                response = f"✅ Чат `{chat_id}` успешно привязан к аккаунту **{name}**!\n\n"
                if thread_id_int:
                    response += f"🧵 Топик ID: `{thread_id_int}`\n"
                response += f"\nТеперь все удалённые сообщения и отчёты будут отправляться туда."
                
                await event.respond(response)
                
            except Exception as e:
                await event.respond(
                    f"❌ Не удалось отправить сообщение в чат `{chat_id}`\n"
                    f"**Ошибка:** `{e}`\n\n"
                    f"**Убедитесь что:**\n"
                    f"1. Бот добавлен в группу\n"
                    f"2. У бота есть права на отправку сообщений\n"
                    f"3. Chat ID указан правильно\n"
                    f"4. Thread ID существует (если указан)"
                )
                
        except Exception as e:
            await event.respond(f"❌ Ошибка: {e}")

    @bot_client.on(events.NewMessage(pattern='/list_admins'))
    async def list_admins_handler(event):
        if not is_admin(event.sender_id):
            await event.respond("❌ Нет доступа.")
            return
        
        text = "👥 **Список администраторов:**\n\n"
        for admin_id in bot_data['admins']:
            marker = "⭐" if admin_id == MAIN_ADMIN_ID else "•"
            text += f"{marker} {admin_id}\n"
        
        await event.respond(text)

async def main():
    global bot
    
    os.makedirs('sessions', exist_ok=True)
    load_data()
    
    # Инициализация бота управления
    bot = TelegramClient('sessions/manager_bot', API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    
    # Регистрация обработчиков
    setup_bot_handlers(bot)
    
    print("🤖 Бот управления запущен...")
    
    # Запуск существующих клиентов
    for name, acc in list(bot_data['accounts'].items()):
        try:
            client, status = await start_user_client(name, acc['api_id'], acc['api_hash'], acc['phone'])
            if status == "OK":
                print(f"✅ Клиент {name} запущен")
            else:
                print(f"⚠️ Клиент {name}: {status}")
        except Exception as e:
            print(f"❌ Ошибка запуска клиента {name}: {e}")
    
    # Запуск планировщика отчётов
    asyncio.create_task(report_scheduler())
    
    # Основной цикл
    print("✅ Система запущена. Ожидание команд...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Остановка бота...")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")