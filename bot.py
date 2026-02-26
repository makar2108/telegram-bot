import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaDocument, Message, InputFile, CallbackQuery
from aiogram.enums import ContentType
from bs4 import BeautifulSoup
import re
try:
    from PIL import Image  # для конвертации WEBP → JPEG (Telegram не принимает webp как фото)
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False
import logging
from io import BytesIO
from playwright.async_api import async_playwright
import time
from urllib.parse import urljoin, urlparse

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

import os

# Токен бота и ID админа
API_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = 198711432

# Инициализация бота
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Эмодзи для анимации загрузки
LOADING_EMOJIS = ['⏳', '🕒', '🕓']

# Максимальный размер файла (50 МБ для Telegram)
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 МБ в байтах

# Поддерживаемые форматы изображений
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')
VIDEO_EXTENSIONS = ('.mp4', '.webm', '.mov', '.avi', '.mkv')

# Счетчик запросов и хранилище активности
request_count = 0
user_activity = {}  # {user_id: [timestamp, ...]}

# Обновление активности пользователя
def update_user_activity(user_id: int):
    current_time = time.time()
    if user_id not in user_activity:
        user_activity[user_id] = []
    user_activity[user_id].append(current_time)
    # Очистка старых меток (старше 7 дней)
    user_activity[user_id] = [t for t in user_activity[user_id] if t > current_time - 604800]

# Подсчёт пользователей
def get_user_stats():
    now = time.time()
    daily_users = set()
    weekly_users = set()
    total_users = set()
    
    for user_id, timestamps in user_activity.items():
        total_users.add(user_id)
        for t in timestamps:
            if t > now - 86400:  # 24 часа
                daily_users.add(user_id)
            if t > now - 604800:  # 7 дней
                weekly_users.add(user_id)
    
    return len(daily_users), len(weekly_users), len(total_users)

# Меню админ-панели
def get_admin_menu():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("📊 Статистика пользователей", callback_data="admin_stats")],
        [InlineKeyboardButton("🚀 Статус бота", callback_data="admin_status")],
        [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
    ])
    return keyboard

# Попытка скачать изображение в альтернативном формате (например, вместо .webp — .jpg/.jpeg/.png)
async def fetch_alt_image_format(session: aiohttp.ClientSession, url: str) -> BytesIO | None:
    try:
        lu = (url or '').lower()
        if not lu.endswith('.webp'):
            return None
        for ext in ('.jpg', '.jpeg', '.png'):
            alt = url[:-5] + ext  # заменяем суффикс .webp
            try:
                async with session.get(alt, timeout=15) as resp:
                    ctype = (resp.headers.get('content-type') or '').lower()
                    if resp.status == 200 and (ctype.startswith('image/jpeg') or ctype.startswith('image/png')):
                        data = await resp.read()
                        if data:
                            return BytesIO(data)
            except Exception:
                continue
    except Exception:
        return None
    return None

# Отправка списка URL с фото (фильтрация, скачивание, конвертация, батчи)
async def process_media_urls(message: Message, urls: list[str], loading_msg: Message, source_hint: str = ""):
    try:
        # Спец-фильтрация для easyhata
        photo_urls = []
        try:
            parsed = urlparse(source_hint or "")
            host = (parsed.netloc or '').lower()
            obj_id = None
            m = re.search(r"/flats/(\d+)/", parsed.path or '')
            if m:
                obj_id = m.group(1)
            def is_target(u: str) -> bool:
                lu = (u or '').lower()
                if any(x in lu for x in ['.svg', 'favicon.ico', '/avatar/']):
                    return False
                if (('easybase.b-cdn.net' in lu and '/realty/' in lu) or ('api.easybase.com.ua' in lu and '/media/realty/' in lu)):
                    if obj_id and f"/{obj_id}/" in lu:
                        return True
                    return True
                return True  # не ограничиваем для других доменов
            if 'easyhata.site' in host:
                urls = [u for u in urls if is_target(u)]
        except Exception:
            pass

        # Проверка изображений (мягкая)
        async with aiohttp.ClientSession() as session:
            for u in urls:
                lu = u.lower()
                if ((('easybase.b-cdn.net' in lu and '/realty/' in lu) or ('api.easybase.com.ua' in lu and '/media/realty/' in lu))
                    and not any(x in lu for x in ['.svg', 'favicon.ico', '/avatar/'])):
                    photo_urls.append(u)
                    continue
                if get_media_type(u) == 'photo' or await is_image_url(session, u):
                    photo_urls.append(u)

        if not photo_urls:
            if loading_msg:
                try:
                    await loading_msg.delete()
                except Exception:
                    pass
            await message.reply("Не удалось найти фотографии. 🚫", reply_markup=get_main_menu())
            return

        media: list[InputMediaPhoto] = []
        doc_fallbacks: list[tuple[BytesIO, str]] = []
        async with aiohttp.ClientSession() as session:
            for i, url in enumerate(photo_urls, 1):
                photo_data, error = await download_media(url, session)
                if not photo_data:
                    continue
                if photo_data.getbuffer().nbytes <= 0:
                    continue
                # По умолчанию пытаемся конвертировать в JPEG (независимо от исходного формата)
                # Сначала пробуем достать альтернативный JPEG/PNG напрямую с CDN
                try:
                    alt_buf = await fetch_alt_image_format(session, url)
                    if alt_buf is not None:
                        photo_data = alt_buf
                except Exception:
                    pass
                if PIL_AVAILABLE:
                    try:
                        photo_data.seek(0)
                        img = Image.open(photo_data)
                        # Если анимированное изображение, берём первый кадр
                        try:
                            if getattr(img, 'is_animated', False):
                                img.seek(0)
                        except Exception:
                            pass
                        if img.mode in ('RGBA', 'P'):
                            img = img.convert('RGB')
                        buf = BytesIO()
                        img.save(buf, format='JPEG', quality=90)
                        buf.seek(0)
                        media.append(InputMediaPhoto(media=InputFile(buf, filename=f"photo_{i}.jpg")))
                        continue
                    except Exception as ce:
                        logging.error(f"Не удалось сконвертировать в JPEG {url}: {ce}")
                        # пойдём во фолбэк ниже
                # Фолбэк: отправим как документ с исходным расширением
                try:
                    ext = '.jpg'
                    m = re.search(r"\.([a-z0-9]{3,4})(?:\?|$)", url.lower())
                    if m:
                        ext = '.' + m.group(1)
                except Exception:
                    ext = '.jpg'
                photo_data.seek(0)
                c = BytesIO(photo_data.read()); c.seek(0)
                doc_fallbacks.append((c, f"photo_{i}{ext}"))

        if loading_msg:
            try:
                await loading_msg.delete()
            except Exception:
                pass

        if len(media) == 1:
            await message.reply_photo(media[0].media, reply_markup=get_main_menu())
            for buf, fname in doc_fallbacks:
                buf.seek(0)
                await message.reply_document(InputFile(buf, filename=fname))
            return

        if 2 <= len(media) <= 10:
            await message.reply_media_group(media)
            for buf, fname in doc_fallbacks:
                buf.seek(0)
                await message.reply_document(InputFile(buf, filename=fname))
            return

        # Батчи
        for start in range(0, len(media), 10):
            batch = media[start:start+10]
            try:
                await message.reply_media_group(batch)
                await asyncio.sleep(0.4)
            except Exception as e:
                logging.error(f"Ошибка отправки батча {(start//10)+1}: {e}")
        for buf, fname in doc_fallbacks:
            buf.seek(0)
            await message.reply_document(InputFile(buf, filename=fname))
    except Exception as e:
        logging.error(f"Ошибка process_media_urls: {e}")

# Извлечение потенциальных ссылок на медиа из HTML
async def extract_potential_urls(url: str) -> list:
    global request_count
    request_count += 1
    try:
        # 1) Быстрый HTTP-парсинг без Playwright: вытянуть все realty-URL из HTML/скриптов
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=20) as r:
                    html_fast = await r.text(errors='ignore')
            # Расшифровка \u002F
            html_fast_unesc = html_fast.replace('\\u002F', '/')
            # Извлекаем id объекта из URL
            _m = re.search(r"/flats/(\d+)/", url)
            obj_id = _m.group(1) if _m else None
            candidates = []
            # Шаблоны CDN
            patterns = [
                r"https?://(?:api\.easybase\.com\.ua|easybase\.b-cdn\.net)[^\s'\"<>]*/realty/(\d+)[^\s'\"<>]*\.(?:webp|jpg|jpeg|png|bmp)",
                r"https?://easybase\.b-cdn\.net/prod/media/realty/(\d+)[^\s'\"<>]*\.(?:webp|jpg|jpeg|png|bmp)"
            ]
            for pat in patterns:
                for m in re.findall(pat, html_fast_unesc, flags=re.IGNORECASE):
                    pass  # нам нужен только id в группе, основную ссылку возьмём вторым проходом
            # Второй проход: просто собрать все ссылки на изображения
            for m in re.findall(r"https?://[^\s'\"<>]+\.(?:webp|jpg|jpeg|png|bmp)", html_fast_unesc, flags=re.IGNORECASE):
                lm = m.lower()
                if any(x in lm for x in ['.svg', 'favicon.ico', '/avatar/']):
                    continue
                if ('/realty/' in lm) and (('easybase.b-cdn.net' in lm) or ('api.easybase.com.ua' in lm)):
                    if (not obj_id) or (f"/{obj_id}/" in lm):
                        candidates.append(m)
            candidates = list(dict.fromkeys(candidates))
            if len(candidates) >= 6:  # достаточно для раннего возврата
                return candidates
        except Exception:
            pass

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            # Коллекция изображений из сетевых ответов
            network_image_urls = []

            async def on_response(response):
                try:
                    resp_url = response.url
                    ctype = (response.headers.get('content-type') or '').lower()
                    if ('image/' in ctype) or resp_url.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')):
                        # минимальный размер, чтобы отсечь иконки
                        clen = int(response.headers.get('content-length', '0'))
                        if clen == 0:
                            # если нет content-length, всё равно добавим
                            clen = 1
                        if clen >= 2048:  # >=2KB – захватываем и небольшие превью
                            network_image_urls.append(resp_url)
                except Exception:
                    pass

            page.on('response', on_response)
            
            # Установка таймаута и ожидание загрузки (мягче: domcontentloaded)
            try:
                await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            except Exception:
                # Даже если навигация с таймаутом, продолжим попытку собрать то, что есть
                pass

            # Быстрый путь: пробуем сразу достать изображения из Nuxt и основного слайдера, и если их достаточно — возвращаем сразу
            try:
                early_nuxt = await page.evaluate('''() => {
                    try {
                        const nuxt = window.__NUXT__;
                        const arr = nuxt && nuxt.data && nuxt.data[0] && nuxt.data[0].shareObject && Array.isArray(nuxt.data[0].shareObject.images)
                            ? nuxt.data[0].shareObject.images.map(x => x && x.img_obj).filter(Boolean)
                            : [];
                        return arr;
                    } catch { return []; }
                }''')
            except Exception:
                early_nuxt = []

            try:
                early_dom = await page.evaluate('''() => {
                    const urls = new Set();
                    const add = u => { if (u) urls.add(String(u)); };
                    document.querySelectorAll('.image-carousel__slider-main-wrap .swiper-slide a.image-carousel__main-img').forEach(a => {
                        add(a.getAttribute('data-src'));
                        const img = a.querySelector('img');
                        if (img) add(img.getAttribute('src'));
                    });
                    return Array.from(urls);
                }''')
            except Exception:
                early_dom = []

            try:
                # Фильтруем на лету только realty-URL на CDN
                def _is_target(u: str) -> bool:
                    lu = u.lower()
                    return ('easybase.b-cdn.net' in lu) and ('/realty/' in lu) and lu.endswith(('.jpg','.jpeg','.png','.webp','.bmp','.gif'))
                early_urls = []
                seen_e = set()
                for u in list(early_nuxt) + list(early_dom):
                    if not u:
                        continue
                    uu = u.strip()
                    if uu.startswith('//'):
                        uu = 'https:' + uu
                    if uu not in seen_e and _is_target(uu):
                        seen_e.add(uu)
                        early_urls.append(uu)
                if len(early_urls) >= 12:
                    await browser.close()
                    return early_urls
            except Exception:
                pass
            # Прокрутка для подгрузки ленивых изображений
            try:
                await page.evaluate('''async () => {
                    await new Promise((resolve) => {
                        let totalHeight = 0;
                        const distance = 400;
                        const timer = setInterval(() => {
                            const scrollHeight = document.body.scrollHeight;
                            window.scrollBy(0, distance);
                            totalHeight += distance;
                            if (totalHeight >= scrollHeight || totalHeight > 12000) {
                                clearInterval(timer);
                                resolve();
                            }
                        }, 120);
                    });
                }''')
            except Exception:
                pass
            await page.wait_for_timeout(3000)  # Небольшая задержка после прокрутки
            
            # Получаем HTML после выполнения JavaScript
            # Дополнительно собираем ссылки на изображения напрямую из DOM через JS
            try:
                dom_urls = await page.evaluate(r'''() => {
                    const urls = new Set();
                    const add = (u) => {
                        if (!u) return;
                        u = String(u).trim();
                        if (u.startsWith('//')) u = 'https:' + u;
                        urls.add(u);
                    };
                    // Все изображения и их srcset
                    document.querySelectorAll('img').forEach(img => {
                        add(img.getAttribute('src'));
                        add(img.getAttribute('data-src'));
                        add(img.getAttribute('data-original'));
                        add(img.getAttribute('data-lazy'));
                        add(img.getAttribute('data-image'));
                        add(img.getAttribute('data-src-large'));
                        const sets = [img.getAttribute('srcset'), img.getAttribute('data-srcset')].filter(Boolean);
                        sets.forEach(ss => {
                            const first = String(ss).split(',')[0].trim().split(' ')[0];
                            add(first);
                        });
                    });
                    // Ссылки, указывающие на изображения
                    document.querySelectorAll('a').forEach(a => {
                        const href = a.getAttribute('href') || '';
                        const ds = a.getAttribute('data-src') || '';
                        if (/(\.jpg|\.jpeg|\.png|\.webp|\.gif|\.bmp)(\?|$)/i.test(href)) {
                            add(href);
                        }
                        if (/(\.jpg|\.jpeg|\.png|\.webp|\.gif|\.bmp)(\?|$)/i.test(ds)) {
                            add(ds);
                        }
                    });
                    // Фоновые изображения
                    document.querySelectorAll('[style*="background"]').forEach(el => {
                        try {
                            const bg = getComputedStyle(el).backgroundImage;
                            if (bg && bg.includes('url(')) {
                                const matches = bg.match(/url\(("|')?(.*?)\1\)/g) || [];
                                matches.forEach(m => {
                                    const u = m.replace(/^url\(("|')?/, '').replace(/\1?\)$/, '');
                                    add(u);
                                });
                            }
                        } catch {}
                    });
                    // Проход по всем атрибутам всех элементов: ищем CDN и расширения изображений
                    const reImg = /(https?:\/\/[^\s'"<>]+\.(?:jpg|jpeg|png|webp|gif|bmp))/ig;
                    const reCdn = /(https?:\/\/[^\s'"<>]*easybase\.b-cdn\.net[^\s'"<>]*)/ig;
                    document.querySelectorAll('*').forEach(el => {
                        for (const attr of el.getAttributeNames ? el.getAttributeNames() : []) {
                            const val = el.getAttribute(attr) || '';
                            let m;
                            while ((m = reImg.exec(val)) !== null) add(m[1]);
                            while ((m = reCdn.exec(val)) !== null) add(m[1]);
                        }
                    });
                    return Array.from(urls);
                }''')
            except Exception:
                dom_urls = []

            html_content = await page.content()
            
            # Дополнительно достаём URL, записанные с экранированными слешами (\u002F) в скриптах Nuxt
            escaped_urls = []
            try:
                esc_pattern = r"https:\\u002F\\u002F[^\s'\"<>]+\\.(?:jpg|jpeg|png|webp|gif|bmp)"
                for m in re.findall(esc_pattern, html_content, flags=re.IGNORECASE):
                    decoded = m.replace("\\u002F", "/")
                    escaped_urls.append(decoded)
            except Exception:
                pass
            
            # Пытаемся открыть модальное окно галереи и пройтись по всем фото
            try:
                # Клики по возможным триггерам галереи
                triggers = [
                    '[data-fancybox]','[data-gallery]','a.fancybox','a.lightbox','a[rel*="gallery"]',
                    '.gallery a','figure a','a.pswp__item','a.lg-item','a[href*=".jpg"], a[href*=".jpeg"], a[href*=".png"], a[href*=".webp"]'
                ]
                opened = False
                for sel in triggers:
                    els = await page.query_selector_all(sel)
                    if els:
                        try:
                            await els[0].click(timeout=1000)
                            opened = True
                            break
                        except Exception:
                            continue
                if not opened:
                    # Попробуем кликнуть по первой крупной картинке
                    hero = await page.query_selector('img')
                    if hero:
                        try:
                            await hero.click(timeout=1000)
                            opened = True
                        except Exception:
                            pass

                if opened:
                    await page.wait_for_timeout(500)
                    # Селекторы текущего изображения в популярных галереях
                    image_selectors = [
                        '.fancybox-image','.pswp__img','.lg-current img','.lg-item img',
                        '.lightgallery img:visible','.modal img','.swiper-slide-active img'
                    ]
                    next_selectors = [
                        '.fancybox-button--arrow_right','.pswp__button--arrow--right',
                        '.lg-next','.slick-next','.swiper-button-next','[aria-label="Next"]','button[title*="Next"]'
                    ]
                    seen = set()
                    for _ in range(40):
                        # собрать текущее изображение
                        for sel in image_selectors:
                            imgs = await page.query_selector_all(sel)
                            for img in imgs:
                                try:
                                    src = await img.get_attribute('src')
                                    if not src:
                                        src = await img.get_attribute('data-src')
                                    if src and src not in seen:
                                        seen.add(src)
                                        # нормализуем и добавляем
                                        u = urljoin(url, src)
                                        if u.startswith('//'):
                                            u = 'https:' + u
                                        dom_urls.append(u)
                                except Exception:
                                    pass
                        # попробовать нажать next
                        clicked = False
                        for nsel in next_selectors:
                            btn = await page.query_selector(nsel)
                            if btn:
                                try:
                                    await btn.click(timeout=800)
                                    clicked = True
                                    await page.wait_for_timeout(250)
                                    break
                                except Exception:
                                    continue
                        if not clicked:
                            break
            except Exception:
                pass

            # Пробуем нажать вкладки/кнопки с текстом Фото/Фотографии/Галерея и пересобрать ссылки
            try:
                await page.evaluate('''() => {
                    const texts = ['фото', 'фотографии', 'галерея', 'photos', 'gallery'];
                    const clickIfMatch = (el) => {
                        try {
                            const t = (el.innerText || el.textContent || '').toLowerCase();
                            if (texts.some(x => t.includes(x))) el.click();
                        } catch {}
                    };
                    document.querySelectorAll('a,button,li,div,span').forEach(clickIfMatch);
                }''')
                await page.wait_for_timeout(500)
                # Собираем дополнительные изображения из популярных контейнеров
                extra_dom_urls = await page.evaluate(r'''() => {
                    const urls = new Set();
                    const add = u => { if (u) urls.add(String(u)); };
                    const collect = root => {
                        root.querySelectorAll('img').forEach(img => {
                            add(img.getAttribute('src'));
                            add(img.getAttribute('data-src'));
                        });
                        root.querySelectorAll('[style*="background"]').forEach(el => {
                            try {
                                const bg = getComputedStyle(el).backgroundImage;
                                if (bg && bg.includes('url(')) {
                                    const m = bg.match(/url\(("|')?(.*?)\1\)/);
                                    if (m && m[2]) add(m[2]);
                                }
                            } catch {}
                        });
                    };
                    ['.swiper','.swiper-container','.gallery','.photos','.thumbnails'].forEach(sel => {
                        document.querySelectorAll(sel).forEach(collect);
                    });
                    return Array.from(urls);
                }''')
                for u in extra_dom_urls:
                    try:
                        # нормализуем при добавлении ниже
                        pass
                    except Exception:
                        pass
                # просто добавим их чуть ниже через add_url вместе с dom_urls
                dom_urls = (dom_urls or []) + (extra_dom_urls or [])
            except Exception:
                pass

            # Попытка извлечь изображения из JSON в script-тегах (встроенные галереи)
            try:
                json_urls = await page.evaluate(r'''() => {
                    const out = [];
                    const push = u => { if (u) out.push(String(u)); };
                    const scripts = Array.from(document.querySelectorAll('script'));
                    for (const s of scripts) {
                        const t = s.textContent || '';
                        // Ищем все URL изображений в тексте скрипта
                        const re = /(https?:\\/\\/[^\s'"<>]+\.(?:jpg|jpeg|png|webp|gif|bmp))/ig;
                        let m; while ((m = re.exec(t)) !== null) { push(m[1]); }
                        // Попробуем простейший JSON.parse, если похоже на массив
                        try {
                            const trimmed = t.trim();
                            if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
                                const arr = JSON.parse(trimmed);
                                if (Array.isArray(arr)) {
                                    for (const v of arr) {
                                        if (typeof v === 'string' && /(\.jpg|\.jpeg|\.png|\.webp|\.gif|\.bmp)(\?|$)/i.test(v)) push(v);
                                        if (v && typeof v === 'object') {
                                            for (const k of Object.keys(v)) {
                                                const val = v[k];
                                                if (typeof val === 'string' && /(\.jpg|\.jpeg|\.png|\.webp|\.gif|\.bmp)(\?|$)/i.test(val)) push(val);
                                            }
                                        }
                                    }
                                }
                            }
                        } catch {}
                    }
                    return out;
                }''')
            except Exception:
                json_urls = []

            # Явное извлечение из Nuxt: window.__NUXT__.data[0].shareObject.images[].img_obj
            try:
                nuxt_images = await page.evaluate('''() => {
                    try {
                        const nuxt = window.__NUXT__;
                        const arr = nuxt && nuxt.data && nuxt.data[0] && nuxt.data[0].shareObject && Array.isArray(nuxt.data[0].shareObject.images)
                            ? nuxt.data[0].shareObject.images.map(x => x && x.img_obj).filter(Boolean)
                            : [];
                        return arr;
                    } catch (e) { return []; }
                }''')
            except Exception:
                nuxt_images = []

            # Закрываем браузер
            await browser.close()
            
            # Парсим HTML
            soup = BeautifulSoup(html_content, 'html.parser')
            urls = []

            def add_url(u: str):
                if not u:
                    return
                u = u.strip()
                # Нормализуем относительные URL
                u = urljoin(url, u)
                # Обрабатываем схемы типа //cdn
                if u.startswith('//'):
                    u = 'https:' + u
                if u.startswith(('http://', 'https://')):
                    urls.append(u)

            # <img src> и data-src
            for img in soup.find_all('img'):
                add_url(img.get('src'))
                add_url(img.get('data-src'))
                # srcset / data-srcset: берём первое значение
                for attr in ('srcset', 'data-srcset'):
                    srcset = img.get(attr)
                    if srcset:
                        first = srcset.split(',')[0].strip().split(' ')[0]
                        add_url(first)

            # <picture><source srcset>
            for source in soup.find_all('source'):
                srcset = source.get('srcset')
                if srcset:
                    first = srcset.split(',')[0].strip().split(' ')[0]
                    add_url(first)

            # <noscript><img>
            for noscr in soup.find_all('noscript'):
                inner = BeautifulSoup(noscr.get_text() or '', 'html.parser')
                for img in inner.find_all('img'):
                    add_url(img.get('src'))
                    add_url(img.get('data-src'))

            # OpenGraph meta og:image
            for meta in soup.find_all('meta', property=lambda v: v in ('og:image', 'og:image:secure_url') if v else False):
                add_url(meta.get('content'))

            # link rel=image_src
            for link in soup.find_all('link', rel=lambda r: r and ('image_src' in r or 'icon' in r)):
                add_url(link.get('href'))

            # Добавляем URL, собранные из DOM
            try:
                for u in dom_urls:
                    add_url(u)
            except Exception:
                pass

            # Добавляем URL из скриптов
            try:
                for u in json_urls:
                    add_url(u)
            except Exception:
                pass

            # Добавляем URL из Nuxt
            try:
                for u in nuxt_images:
                    add_url(u)
            except Exception:
                pass
            # Добавляем расэкранированные URL из скриптов
            try:
                for u in escaped_urls:
                    add_url(u)
            except Exception:
                pass

            # Дополнительно: собираем все прямые ссылки на изображения из HTML через regex
            try:
                img_url_pattern = r"https?://[^\s'\"<>]+\.(?:jpg|jpeg|png|webp|gif|bmp)"
                for m in re.findall(img_url_pattern, html_content, flags=re.IGNORECASE):
                    add_url(m)
            except Exception:
                pass

            # Объединяем с картинками из сети
            for nu in network_image_urls:
                add_url(nu)

            # Удаление дубликатов и возврат
            urls = list(dict.fromkeys(urls))
            return urls
    except Exception as e:
        logging.error(f"Ошибка извлечения ссылок: {str(e)}")
        return []

# Парсинг изображений напрямую из HTML (без Playwright)
def parse_image_urls_from_html(html: str, base_url: str | None = None) -> list:
    try:
        soup = BeautifulSoup(html, 'html.parser')
        urls = []
        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src')
            if not src:
                continue
            if base_url:
                full = urljoin(base_url, src)
            else:
                # Если нет base_url, пропускаем относительные пути
                if src.startswith('//'):
                    full = 'https:' + src
                elif src.startswith(('http://', 'https://')):
                    full = src
                else:
                    continue
            urls.append(full)
        # Уникализируем и фильтруем
        urls = list(set(urls))
        return [u for u in urls if u.startswith(('http://', 'https://'))]
    except Exception as e:
        logging.error(f"Ошибка парсинга HTML: {str(e)}")
        return []

# Попытка найти медиа через Playwright
async def fetch_media_url(url: str) -> tuple:
    try:
        logging.info(f"Начинаем поиск медиа на странице: {url}")
        # Специальное правило: на easyhata.site видео не ищем вовсе
        try:
            _host = urlparse(url).netloc.lower()
            if 'easyhata.site' in _host:
                return "", ""
        except Exception:
            pass
        
        # Универсальные заголовки для всех запросов
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.google.com/',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'max-age=0'
        }
        
        # Инициализируем Playwright
        async with async_playwright() as p:
            # Запускаем браузер с настройками
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--no-first-run',
                    '--no-zygote',
                    '--single-process',
                    '--disable-gpu'
                ]
            )
            
            # Создаем контекст с настройками
            context = await browser.new_context(
                user_agent=headers['User-Agent'],
                viewport={'width': 1920, 'height': 1080},
                locale='en-US',
                timezone_id='America/New_York',
                permissions=['geolocation']
            )
            
            # Устанавливаем дополнительные заголовки
            await context.set_extra_http_headers({
                'Accept-Language': headers['Accept-Language'],
                'Referer': headers['Referer'],
                'DNT': headers['DNT']
            })
            
            # Создаем новую страницу
            page = await context.new_page()
            
            # Включаем перехват сетевых запросов
            video_urls = []
            
            async def handle_response(response):
                try:
                    url = response.url.lower()
                    content_type = (response.headers.get('content-type') or '').lower()
                    
                    # Пропускаем ненужные типы запросов
                    if any(x in url for x in ['.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg']):
                        return
                        
                    # Проверяем на видео-контент
                    is_video = ('video/' in content_type or 
                              any(ext in url for ext in ['.mp4', '.webm', '.mov', '.m3u8', 'video/']))
                    
                    if is_video and url not in video_urls:
                        # Проверяем размер контента
                        content_length = int(response.headers.get('content-length', '0'))
                        if content_length > 100000:  # Больше 100 КБ
                            video_urls.append(url)
                            logging.info(f"Найдено видео: {url} (тип: {content_type}, размер: {content_length} байт)")
                            
                except Exception as e:
                    logging.error(f"Ошибка при обработке ответа: {str(e)}")
            
            # Подписываемся на события ответов
            page.on("response", handle_response)
            
            try:
                # Загружаем страницу с настройками для обхода защиты
                await page.goto(
                    url,
                    timeout=60000,
                    wait_until="domcontentloaded",
                    referer=headers['Referer']
                )
                
                # Ждем загрузки страницы
                await page.wait_for_load_state('networkidle', timeout=30000)
                
                # Прокручиваем страницу вниз для загрузки ленивого контента
                await page.evaluate('''async () => {
                    await new Promise((resolve) => {
                        let totalHeight = 0;
                        const distance = 100;
                        const timer = setInterval(() => {
                            const scrollHeight = document.body.scrollHeight;
                            window.scrollBy(0, distance);
                            totalHeight += distance;
                            if (totalHeight >= scrollHeight || totalHeight > 2000) {
                                clearInterval(timer);
                                resolve();
                            }
                        }, 100);
                    });
                }''')
                
                # Дополнительное ожидание после прокрутки
                await page.wait_for_timeout(3000)
                
                # Ищем видео-элементы на странице
                video_elements = await page.query_selector_all('video')
                for video in video_elements:
                    try:
                        # Пробуем получить src
                        src = await video.get_attribute('src')
                        if src and src.startswith(('http://', 'https://')) and src not in video_urls:
                            video_urls.append(src)
                            logging.info(f"Найдено видео в теге video: {src}")
                        
                        # Проверяем source внутри video
                        sources = await video.query_selector_all('source')
                        for source in sources:
                            src = await source.get_attribute('src')
                            if src and src.startswith(('http://', 'https://')) and src not in video_urls:
                                video_urls.append(src)
                                logging.info(f"Найдено видео в теге source: {src}")
                                
                    except Exception as e:
                        logging.error(f"Ошибка при обработке видео-элемента: {str(e)}")
                
                # Если видео не нашли, ищем iframe с видео
                if not video_urls:
                    iframes = await page.query_selector_all('iframe')
                    for iframe in iframes:
                        try:
                            src = await iframe.get_attribute('src')
                            if src and any(x in src.lower() for x in ['youtube', 'vimeo', 'dailymotion', 'player']):
                                video_urls.append(src)
                                logging.info(f"Найдено видео в iframe: {src}")
                        except:
                            continue
                
                # Если видео нашли, возвращаем первое
                if video_urls:
                    cand = video_urls[0]
                    if not cand.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')):
                        return cand, 'video'
                
                # Если видео не нашли, ищем в JSON-LD разметке
                try:
                    json_ld = await page.evaluate('''() => {
                        const scripts = document.querySelectorAll('script[type="application/ld+json"]');
                        for (const script of scripts) {
                            try {
                                return JSON.parse(script.textContent);
                            } catch (e) {}
                        }
                        return null;
                    }''')
                    
                    if json_ld and isinstance(json_ld, dict):
                        # Проверяем различные возможные пути к видео в JSON-LD
                        for key in ['contentUrl', 'embedUrl', 'url', 'video']:
                            if key in json_ld and isinstance(json_ld[key], str) and json_ld[key].startswith(('http://', 'https://')):
                                return json_ld[key], 'video'
                except Exception as e:
                    logging.error(f"Ошибка при парсинге JSON-LD: {str(e)}")
                
                # Ищем видео в iframe
                frames = page.frames
                for frame in frames:
                    try:
                        video_elements = await frame.query_selector_all('video')
                        for video in video_elements:
                            src = await video.get_attribute('src')
                            if src and src.startswith(('http://', 'https://')) and src not in video_urls:
                                video_urls.append(src)
                    except:
                        continue
                
                # Если нашли видео, возвращаем первое (исклюаем ссылки на изображения)
                if video_urls:
                    cand2 = video_urls[0]
                    if not cand2.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')):
                        return cand2, 'video'
                
                # Если видео не нашли, ищем теги video и source
                video_elements = await page.query_selector_all('video')
                for video in video_elements:
                    # Проверяем атрибут src
                    src = await video.get_attribute('src')
                    if src and src.startswith(('http://', 'https://')):
                        if not src.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')):
                            return src, 'video'
                    
                    # Проверяем source внутри video
                    source_elements = await video.query_selector_all('source')
                    for source in source_elements:
                        src = await source.get_attribute('src')
                        if src and src.startswith(('http://', 'https://')):
                            if not src.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')):
                                return src, 'video'
                
                # Проверяем iframe с YouTube, Vimeo и другими встраиваемыми плеерами
                iframes = await page.query_selector_all('iframe')
                for iframe in iframes:
                    src = await iframe.get_attribute('src')
                    if src and any(domain in src for domain in ['youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion.com']):
                        return src, 'video'
                
                # Если видео не нашли, возвращаем первое изображение
                img_elements = await page.query_selector_all('img')
                for img in img_elements:
                    src = await img.get_attribute('src') or await img.get_attribute('data-src')
                    if src and src.startswith(('http://', 'https://')):
                        # Пропускаем маленькие изображения (иконки, аватары и т.д.)
                        width = await img.get_attribute('width')
                        height = await img.get_attribute('height')
                        if width and height and int(width) > 100 and int(height) > 100:
                            return src, 'photo'
                
                return "", ""
                
            except Exception as e:
                logging.error(f"Ошибка при загрузке страницы {url}: {str(e)}")
                return "", str(e)
                
            finally:
                # Закрываем браузер
                if 'context' in locals():
                    await context.close()
                if 'browser' in locals():
                    await browser.close()
                    
    except Exception as e:
        logging.error(f"Ошибка Playwright для {url}: {str(e)}", exc_info=True)
        if 'browser' in locals():
            try:
                await browser.close()
            except:
                pass
        return "", ""
async def download_media(url: str, session: aiohttp.ClientSession, headers: dict = None) -> tuple:
    try:
        logging.info(f"Начинаем скачивание: {url}")

        # Нейтральные заголовки по умолчанию
        if headers is None:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9'
            }

        # Если это похоже на изображение — используем image-ориентированные заголовки
        lower_url = url.lower()
        if lower_url.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')):
            headers = {
                'User-Agent': headers.get('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'),
                'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
                'Accept-Language': headers.get('Accept-Language', 'en-US,en;q=0.9')
            }
            # Авто-Referer на домен ресурса
            try:
                parsed = urlparse(url)
                if parsed.scheme and parsed.netloc:
                    headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"
            except Exception:
                pass

        async with session.get(url, headers=headers, timeout=30) as response:
            if response.status != 200:
                error_text = await response.text()
                logging.error(f"Ошибка HTTP {response.status} для URL: {url}\n{error_text[:500]}")
                
                # Проверяем, требует ли сайт авторизации
                if response.status == 401 or 'login' in error_text.lower():
                    return None, "Для загрузки этого контента требуется авторизация на сайте 🚫"
                elif response.status == 403:
                    return None, "Доступ к этому контенту запрещен (ошибка 403) 🔒"
                elif response.status == 404:
                    return None, "Контент не найден (ошибка 404) 🔍"
                else:
                    return None, f"Ошибка {response.status} при загрузке контента 🚫"
        
            # Проверка размера файла
            content_length = response.content_length or 0
            if content_length > MAX_FILE_SIZE:
                size_mb = content_length / (1024 * 1024)
                logging.error(f"Файл слишком большой: {size_mb:.2f} МБ")
                return None, f"Файл слишком большой ({size_mb:.2f} МБ). Telegram ограничивает размер до 50 МБ 🚫"
        
            # Скачивание с отображением прогресса
            content = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                content.extend(chunk)
                if len(content) > MAX_FILE_SIZE:
                    logging.error(f"Файл превысил максимальный размер при загрузке: {len(content) / (1024*1024):.2f} МБ")
                    return None, "Файл слишком большой для загрузки 🚫"
        
            logging.info(f"Успешно скачан файл размером: {len(content) / 1024:.2f} КБ")
            return BytesIO(content), None
            
    except asyncio.TimeoutError:
        logging.error(f"Таймаут при скачивании: {url}")
        return None, "Превышено время ожидания при скачивании 🚫"
    except aiohttp.ClientError as e:
        logging.error(f"Ошибка сети при скачивании {url}: {str(e)}")
        return None, f"Ошибка сети: {str(e)} 🚫"
    except Exception as e:
        logging.error(f"Ошибка скачивания {url}: {str(e)}", exc_info=True)
        return None, f"Ошибка при загрузке контента: {str(e)} 🚫"

# Определение типа медиа по URL
def get_media_type(url: str):
    url_lower = url.lower()
    
    # Проверяем популярные видеохостинги
    video_domains = [
        'youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion.com', 'twitch.tv',
        'tiktok.com', 'instagram.com/reel', 'facebook.com/watch', 'youtube.com/shorts'
    ]
    
    if any(domain in url_lower for domain in video_domains):
        return 'video'
    
    # Проверяем расширения файлов
    if any(url_lower.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return 'photo'
    elif any(url_lower.endswith(ext) for ext in VIDEO_EXTENSIONS):
        return 'video'
        
    # Проверяем MIME-типы в URL
    if 'video/' in url_lower or 'stream/' in url_lower or 'media/video' in url_lower:
        return 'video'
        
    return 'unknown'

# Быстрая проверка: является ли URL изображением по заголовку Content-Type
async def is_image_url(session: aiohttp.ClientSession, url: str) -> bool:
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9'
        }
        # Пробуем HEAD для экономии трафика
        try:
            async with session.head(url, headers=headers, allow_redirects=True, timeout=15) as resp:
                ctype = (resp.headers.get('Content-Type') or '').lower()
                return ctype.startswith('image/')
        except aiohttp.ClientResponseError as e:
            if e.status not in (400, 405):
                raise
        except Exception:
            # Фоллбэк на GET, если HEAD не поддерживается
            pass

        async with session.get(url, headers=headers, allow_redirects=True, timeout=15) as resp:
            ctype = (resp.headers.get('Content-Type') or '').lower()
            return ctype.startswith('image/')
    except Exception:
        return False

# Анимация загрузки
async def show_loading_animation(message: Message, media_type: str = 'медиа'):
    try:
        loading_msg = await message.reply(f"Загрузка {media_type}... {LOADING_EMOJIS[0]}", parse_mode='Markdown')
        if len(LOADING_EMOJIS) > 1:
            for emoji in LOADING_EMOJIS[1:]:
                try:
                    await asyncio.sleep(0.5)
                    await loading_msg.edit_text(f"Загрузка {media_type}... {emoji}", parse_mode='Markdown')
                except Exception as e:
                    logging.error(f"Ошибка при обновлении анимации: {str(e)}")
                    break
        return loading_msg
    except Exception as e:
        logging.error(f"Ошибка при создании анимации загрузки: {str(e)}")
        return None

# Главное меню
def get_main_menu():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Отправить HTML-код 📄", callback_data="send_html")],
        [InlineKeyboardButton("Поддержка 🌟", callback_data="support")]
    ])
    return keyboard

# Команда /start
async def send_welcome(message: Message):
    user_id = message.from_user.id
    update_user_activity(user_id)
    await message.reply(
        "Добро пожаловать в бот для скачивания видео! 📹\n\n"
        "Отправьте HTML-код страницы, и я попытаюсь найти и скачать видео.\n"
        "⚠️ Telegram ограничивает размер скачиваемых файлов до 50 МБ.\n"
        "Работает с тегами <video>, <iframe> или ссылками на страницы.\n\n"
        "Выберите действие:",
        reply_markup=get_main_menu()
    )

# Команда /support
async def send_support(message: Message):
    user_id = message.from_user.id
    update_user_activity(user_id)
    await message.reply(
        "🌟 *Поддержка*\n\n"
        "Если бот не работает или у вас есть вопросы, пишите: @makar2108 📩\n"
        "⚠️ Telegram ограничивает размер скачиваемых файлов до 50 МБ.\n\n"
        "Поддержите проект добровольным пожертвованием:\n"
        "BEP-20 USDT: `0xc4b648A590A61F2F1d8b99f41248066533428471` 💸",
        parse_mode='Markdown',
        reply_markup=get_main_menu()
    )

# Команда /admin
async def admin_status(message: Message):
    user_id = message.from_user.id
    update_user_activity(user_id)
    if user_id != ADMIN_ID:
        await message.reply("Доступно только админу. 🔐", reply_markup=get_main_menu())
        return
    await message.reply(
        "🔐 *Админ-панель*\n\n"
        "Выберите действие:",
        parse_mode='Markdown',
        reply_markup=get_admin_menu()
    )

# Обработка текстовых сообщений (URL или HTML-код)
async def handle_html(message: Message):
    try:
        user_id = message.from_user.id
        update_user_activity(user_id)
        content = message.text.strip()
        
        logging.info(f"Получено сообщение от пользователя {user_id}: {content[:50]}...")
        
        # Создаем сообщение о загрузке
        loading_msg = await show_loading_animation(message, "контента")
        if loading_msg is None:
            await message.reply("Произошла ошибка при создании сообщения о загрузке. Пожалуйста, попробуйте снова.")
            return
        
        # Проверяем, является ли сообщение URL
        is_url = content.startswith(('http://', 'https://'))
        
        if is_url:
            logging.info(f"Обработка URL: {content}")
            
            # Сначала проверяем, не является ли это видео
            media_type = get_media_type(content)
            if media_type == 'video':
                await process_video_url(message, content, loading_msg)
                return
                
            # Если это не видео, ищем медиа на странице
            if loading_msg is not None:
                try:
                    await loading_msg.edit_text("🔍 Анализирую страницу на наличие медиа...")
                except Exception:
                    pass
            
            # Пробуем найти медиа на странице
            media_url, media_kind = await fetch_media_url(content)
            potential_urls = []
            if media_url and media_kind == 'video':
                await process_video_url(message, media_url, loading_msg)
                return
            if media_url and media_kind == 'photo':
                potential_urls.append(media_url)
            
            # Ищем все изображения на странице и объединяем
            if loading_msg is not None:
                try:
                    await loading_msg.edit_text("🔍 Видео не найдено, ищу изображения...")
                except Exception:
                    pass
            more_urls = await extract_potential_urls(content)
            if more_urls:
                # Объединяем без дублей, сохраняя порядок: сначала найденное основное фото, затем остальные
                seen = set(potential_urls)
                for u in more_urls:
                    if u not in seen:
                        potential_urls.append(u)
                        seen.add(u)
        else:
            logging.info("Обработка HTML-кода (локальный парсинг)")
            potential_urls = parse_image_urls_from_html(content)
        
        # Специальная фильтрация для easyhata: оставляем только CDN realty для конкретного объекта
        try:
            from urllib.parse import urlparse
            import re as _re
            parsed = urlparse(content)
            host = (parsed.netloc or '').lower()
            obj_id = None
            m = _re.search(r"/flats/(\d+)/", parsed.path or '')
            if m:
                obj_id = m.group(1)

            def is_target_url(u: str) -> bool:
                lu = (u or '').lower()
                if any(x in lu for x in ['.svg', 'favicon.ico']):
                    return False
                if 'avatar' in lu:
                    return False
                if (('easybase.b-cdn.net' in lu and '/realty/' in lu) or
                    ('api.easybase.com.ua' in lu and '/media/realty/' in lu)):
                    if obj_id and f"/{obj_id}/" in lu:
                        return True
                    # если id не удалось определить, всё равно допускаем realty
                    return True
                return False

            # если это easyhata и нашли целевые ссылки — сохраняем только их
            if 'easyhata.site' in host:
                filtered = [u for u in potential_urls if is_target_url(u)]
                if len(filtered) >= 1:
                    potential_urls = filtered
        except Exception:
            pass

        logging.info(f"Найдено потенциальных URL: {len(potential_urls)}")
        
        if not potential_urls:
            if loading_msg is not None:
                try:
                    await loading_msg.delete()
                except Exception:
                    pass
            await message.reply(
                "Не удалось найти фотографии. 🚫\n"
                "Проверьте правильность ссылки или HTML-кода.",
                reply_markup=get_main_menu()
            )
            return
        
        # Фильтруем только изображения
        photo_urls = []
        async with aiohttp.ClientSession() as session:
            for url in potential_urls:
                # Для целевых CDN realty URL не делаем лишнюю проверку HEAD
                lu = url.lower()
                if ((('easybase.b-cdn.net' in lu and '/realty/' in lu) or ('api.easybase.com.ua' in lu and '/media/realty/' in lu))
                    and not any(x in lu for x in ['.svg', 'favicon.ico', '/avatar/'])):
                    photo_urls.append(url)
                    continue
                if get_media_type(url) == 'photo' or await is_image_url(session, url):
                    photo_urls.append(url)
        logging.info(f"Найдено фотографий: {len(photo_urls)}")
        
        if not photo_urls:
            await loading_msg.delete()
            await message.reply(
                "Не удалось найти фотографии. 🚫\n"
                "Убедитесь, что страница содержит изображения.",
                reply_markup=get_main_menu()
            )
            return
        
        # Скачиваем фотографии
        media = []  # только фото для альбомов
        doc_fallbacks = []  # документы для кейсов без конвертации
        success_count = 0
        error_count = 0
        
        async with aiohttp.ClientSession() as session:
            for i, url in enumerate(photo_urls, 1):
                logging.info(f"Обработка фото {i}/{len(photo_urls)}: {url}")
                photo_data, error = await download_media(url, session)
                if photo_data:
                    try:
                        # Проверяем, что фото действительно валидное
                        if photo_data.getbuffer().nbytes > 0:
                            # Сначала пробуем получить альтернативный JPEG/PNG URL
                            try:
                                alt_buf = await fetch_alt_image_format(session, url)
                                if alt_buf is not None:
                                    photo_data = alt_buf
                            except Exception:
                                pass
                            if PIL_AVAILABLE:
                                try:
                                    photo_data.seek(0)
                                    img = Image.open(photo_data)
                                    try:
                                        if getattr(img, 'is_animated', False):
                                            img.seek(0)
                                    except Exception:
                                        pass
                                    if img.mode in ('RGBA', 'P'):
                                        img = img.convert('RGB')
                                    buf = BytesIO()
                                    img.save(buf, format='JPEG', quality=90)
                                    buf.seek(0)
                                    input_file = InputFile(buf, filename=f"photo_{i}.jpg")
                                    media.append(InputMediaPhoto(media=input_file))
                                except Exception as ce:
                                    logging.error(f"Конвертация в JPEG не удалась для фото {i}: {ce}")
                                    photo_data.seek(0)
                                    copy_buf = BytesIO(photo_data.read())
                                    copy_buf.seek(0)
                                    # Фолбэк: отправим как документ с исходным расширением
                                    try:
                                        ext = '.jpg'
                                        m = re.search(r"\.([a-z0-9]{3,4})(?:\?|$)", url.lower())
                                        if m:
                                            ext = '.' + m.group(1)
                                    except Exception:
                                        ext = '.jpg'
                                    doc_fallbacks.append((copy_buf, f"photo_{i}{ext}"))
                            else:
                                photo_data.seek(0)
                                copy_buf = BytesIO(photo_data.read())
                                copy_buf.seek(0)
                                try:
                                    ext = '.jpg'
                                    m = re.search(r"\.([a-z0-9]{3,4})(?:\?|$)", url.lower())
                                    if m:
                                        ext = '.' + m.group(1)
                                except Exception:
                                    ext = '.jpg'
                                doc_fallbacks.append((copy_buf, f"photo_{i}{ext}"))
                            success_count += 1
                            logging.info(f"Успешно добавлено фото {i}")
                        else:
                            error_count += 1
                            logging.error(f"Фото {i} имеет нулевой размер")
                    except Exception as e:
                        error_count += 1
                        logging.error(f"Ошибка при проверке фото {i}: {str(e)}")
                else:
                    error_count += 1
                    logging.error(f"Ошибка при обработке фото {i}: {error}")
        
        logging.info(f"Успешно обработано фото: {success_count}, ошибок: {error_count}")
        
        if media:
            try:
                await loading_msg.delete()
            except Exception:
                pass
            loading_msg = None
            try:
                # Проверяем количество валидных фото
                if len(media) == 0:
                    raise Exception("Нет валидных фотографий для отправки")
                
                if len(media) == 1:
                    # Если найдено только одно фото, отправляем его как одиночное
                    logging.info("Отправка одиночного фото")
                    await message.reply_photo(
                        photo=media[0].media,
                        caption=f"✅ Фото скачано!\nИсточник: {content[:50]}...",
                        reply_markup=get_main_menu()
                    )
                    logging.info("Успешно отправлено одиночное фото")
                    return
                elif 2 <= len(media) <= 10:
                    # Если найдено несколько фото, отправляем как альбом
                    logging.info(f"Отправка альбома из {len(media)} фото")
                    await message.reply_media_group(media)
                    # Отправляем документы-фолбэки (если есть)
                    for idx, (buf, fname) in enumerate(doc_fallbacks, 1):
                        try:
                            buf.seek(0)
                            await message.reply_document(InputFile(buf, filename=fname))
                            await asyncio.sleep(0.2)
                        except Exception as e:
                            logging.error(f"Ошибка отправки документа {idx}: {e}")
                    await message.reply(
                        f"✅ Скачано {len(media)} фотографий!\n"
                        f"Источник: {content[:50]}...\n"
                        f"Документов (fallback): {len(doc_fallbacks)}",
                        reply_markup=get_main_menu()
                    )
                    logging.info(f"Успешно отправлен альбом из {len(media)} фото")
                    return
                else:
                    # Если фото больше 10, отправляем все по батчам по 10
                    total = len(media)
                    logging.info(f"Отправка альбома батчами по 10 (всего найдено: {total})")
                    for start in range(0, total, 10):
                        batch = media[start:start+10]
                        try:
                            await message.reply_media_group(batch)
                            await asyncio.sleep(0.5)
                        except Exception as e:
                            logging.error(f"Ошибка отправки батча {start//10+1}: {e}")
                    # Отправляем документы-фолбэки (если есть)
                    for idx, (buf, fname) in enumerate(doc_fallbacks, 1):
                        try:
                            buf.seek(0)
                            await message.reply_document(InputFile(buf, filename=fname))
                            await asyncio.sleep(0.2)
                        except Exception as e:
                            logging.error(f"Ошибка отправки документа {idx}: {e}")
                    await message.reply(
                        f"✅ Скачано и отправлено {total} фотографий!\n"
                        f"Источник: {content[:50]}...\n"
                        f"Документов (fallback): {len(doc_fallbacks)}",
                        reply_markup=get_main_menu()
                    )
                    logging.info("Успешно отправлены все батчи фото")
                    return
            except Exception as e:
                logging.error(f"Ошибка отправки фото: {str(e)}")
            
            # Если видео не нашли, ищем изображения
            if loading_msg is not None:
                try:
                    await loading_msg.edit_text(" Видео не найдено, ищу изображения...")
                except Exception:
                    pass
            # ниже устаревшая ветка для HTML-текста, пропустим для URL сценария
            
        else:
            # Создаем сообщение о загрузке
            loading_msg = await show_loading_animation(message, "контента")
            if loading_msg is None:
                await message.reply("Произошла ошибка при создании сообщения о загрузке. Пожалуйста, попробуйте снова.")
                return
            
            # Проверяем, является ли текст HTML-кодом
            is_html = '<' in content and '>' in content
            
            if is_html:
                # Проверяем тип контента по URL/HTML
                media_type = get_media_type(content)
            if media_type == 'video':
                # Пытаемся скачать видео напрямую
                await process_video_url(message, content, loading_msg)
            else:
                # Используем Playwright для анализа страницы
                if loading_msg is not None:
                    try:
                        await loading_msg.edit_text(" Анализирую страницу на наличие медиа...")
                    except Exception:
                        pass
                
                # Пробуем найти видео на странице
                video_url, _ = await fetch_media_url(content)
                if video_url:
                    await process_video_url(message, video_url, loading_msg)
                    return
                
                # Если видео не нашли, ищем изображения
                urls = await extract_potential_urls(content)
                if urls:
                    await process_media_urls(message, urls, loading_msg)
                else:
                    if loading_msg is not None:
                        try:
                            await loading_msg.edit_text("Не удалось найти медиа на странице ")
                        except Exception:
                            pass
                
    except Exception as e:
        logging.error(f"Ошибка обработки сообщения: {str(e)}")
        if 'loading_msg' in locals() and loading_msg is not None:
            try:
                await loading_msg.edit_text(f"Произошла ошибка: {str(e)} ")
            except Exception:
                pass
    finally:
        if 'loading_msg' in locals() and loading_msg is not None:
            try:
                await bot.delete_message(chat_id=loading_msg.chat.id, message_id=loading_msg.message_id)
            except Exception:
                pass

# Обработка видео по URL
async def process_video_url(message: Message, video_url: str, loading_msg: Message):
    try:
        await loading_msg.edit_text("📥 Скачиваю видео...")
        
        # Устанавливаем заголовки для обхода защиты
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://motherless.com/',
            'Accept': 'video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5',
            'Accept-Language': 'en-US,en;q=0.5',
            'Range': 'bytes=0-',
            'Origin': 'https://motherless.com',
            'DNT': '1',
        }
        
        # Создаем сессию для скачивания с настройками
        timeout = aiohttp.ClientTimeout(total=300, connect=30)
        connector = aiohttp.TCPConnector(force_close=True, enable_cleanup_closed=True)
        
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            try:
                # Скачиваем видео с нашими заголовками
                video_data, error = await download_media(video_url, session, headers=headers)
                
                if error:
                    # Если ошибка связана с аутентификацией, сообщаем пользователю
                    if 'требуется авторизация' in error.lower():
                        await message.reply(
                            "⚠️ Это видео доступно только для зарегистрированных пользователей Motherless.\n\n"
                            "Пожалуйста, войдите в аккаунт на сайте и попробуйте снова.",
                            reply_markup=get_main_menu()
                        )
                    else:
                        await message.reply(f"❌ {error}", reply_markup=get_main_menu())
                    
                    try:
                        await loading_msg.delete()
                    except:
                        pass
                    return
                
                # Определяем расширение файла
                file_ext = 'mp4'  # По умолчанию используем mp4
                if '.' in video_url:
                    ext = video_url.split('.')[-1].lower()
                    if ext in ['mp4', 'webm', 'mov', 'avi', 'mkv', 'flv']:
                        file_ext = ext
                
                # Создаем временный файл
                temp_file = f"temp_video_{int(time.time())}.{file_ext}"
                try:
                    with open(temp_file, 'wb') as f:
                        f.write(video_data.getbuffer())
                    
                    # Отправляем видео
                    await loading_msg.edit_text("📤 Отправляю видео...")
                    
                    try:
                        # Пробуем отправить как видео
                        with open(temp_file, 'rb') as video_file:
                            await message.reply_video(
                                video=video_file,
                                caption=f"🎥 Видео загружено!\nИсточник: {video_url[:100]}",
                                reply_markup=get_main_menu(),
                                supports_streaming=True
                            )
                    except Exception as e:
                        # Если не удалось отправить как видео, пробуем отправить как документ
                        logging.error(f"Ошибка отправки видео: {str(e)}, пробуем отправить как документ...")
                        with open(temp_file, 'rb') as video_file:
                            await message.reply_document(
                                document=video_file,
                                caption=f"📁 Видео загружено как документ\nИсточник: {video_url[:100]}",
                                reply_markup=get_main_menu()
                            )
                    
                    await loading_msg.delete()
                    
                except Exception as e:
                    logging.error(f"Ошибка при сохранении/отправке видео: {str(e)}", exc_info=True)
                    await message.reply("❌ Произошла ошибка при обработке видео. Пожалуйста, попробуйте позже.")
                
                # Удаляем временный файл, если он существует
                try:
                    import os
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                except Exception as e:
                    logging.error(f"Ошибка при удалении временного файла: {str(e)}")
                    
            except Exception as e:
                logging.error(f"Ошибка при обработке видео: {str(e)}", exc_info=True)
                await message.reply(f"❌ Произошла ошибка: {str(e)}")
                try:
                    await loading_msg.delete()
                except:
                    pass
                    
    except Exception as e:
        logging.error(f"Критическая ошибка в process_video_url: {str(e)}", exc_info=True)
        try:
            await message.reply("❌ Произошла критическая ошибка при обработке видео. Пожалуйста, попробуйте позже.")
            try:
                await loading_msg.delete()
            except:
                pass
        except Exception as e2:
            logging.error(f"Ошибка при отправке сообщения об ошибке: {str(e2)}")

# Обработка инлайн-кнопок
async def process_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    update_user_activity(user_id)
    action = callback.data

    if action == 'main_menu':
        await callback.message.edit_text(
            "Отправьте HTML-код страницы, содержащий видео. 📄",
            reply_markup=get_main_menu()
        )
    elif action == 'send_html':
        await callback.message.edit_text(
            "Отправьте HTML-код страницы, содержащий видео. 📄",
            reply_markup=get_main_menu()
        )
    elif action == 'support':
        await callback.message.edit_text(
            "🌟 *Поддержка*\n\n"
            "Если бот не работает или у вас есть вопросы, пишите: @makar2108 📩\n"
            "⚠️ Telegram ограничивает размер скачиваемых файлов до 50 МБ.\n\n"
            "Поддержите проект добровольным пожертвованием:\n"
            "BEP-20 USDT: `0xc4b648A590A61F2F1d8b99f41248066533428471` 💸",
            parse_mode='Markdown',
            reply_markup=get_main_menu()
        )
    elif action == 'admin_stats':
        if user_id != ADMIN_ID:
            await callback.message.edit_text("Доступно только админу. 🔐", reply_markup=get_main_menu())
            await callback.answer()
            return
        daily, weekly, total = get_user_stats()
        await callback.message.edit_text(
            f"📊 *Статистика пользователей*\n\n"
            f"Пользователи:\n"
            f"- За день: {daily}\n"
            f"- За неделю: {weekly}\n"
            f"- За всё время: {total}",
            parse_mode='Markdown',
            reply_markup=get_admin_menu()
        )
    elif action == 'admin_status':
        if user_id != ADMIN_ID:
            await callback.message.edit_text("Доступно только админу. 🔐", reply_markup=get_main_menu())
            await callback.answer()
            return
        await callback.message.edit_text(
            f"🚀 *Статус бота*\n\n"
            f"Бот работает!\n"
            f"Обработано запросов: {request_count} 📊",
            parse_mode='Markdown',
            reply_markup=get_admin_menu()
        )

    await callback.answer()

# Register handlers
dp.message.register(send_welcome, Command(commands=['start']))
dp.message.register(send_support, Command(commands=['support']))
dp.message.register(admin_status, Command(commands=['admin']))
dp.message.register(handle_html, F.content_type == ContentType.TEXT)
dp.callback_query.register(process_callback)

async def on_startup():
    logging.info('Бот запущен 🚀')

async def main():
    dp.startup.register(on_startup)
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == '__main__':
    asyncio.run(main())