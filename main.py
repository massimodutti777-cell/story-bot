import asyncio
import logging
import os
import json
import io
import urllib.parse
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, BotCommand, BufferedInputFile
import openai
import replicate
from weasyprint import HTML

# --- КЛЮЧИ (безопасно считываются из переменных окружения Render) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN")

RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://story-bot-34cj.onrender.com")

if REPLICATE_API_TOKEN:
    os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Состояния диалога
class StoryForm(StatesGroup):
    waiting_for_name = State()
    waiting_for_theme = State()
    waiting_for_photo = State()

def get_restart_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Создать новую сказку")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

@dp.message(CommandStart())
@dp.message(F.text == "Создать новую сказку")
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я создаю волшебные иллюстрированные книги для детей.\n\nКак зовут главного героя книги?",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(StoryForm.waiting_for_name)

@dp.message(StoryForm.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(child_name=message.text)
    await message.answer(
        f"Замечательно! О чем будет сказка про {message.text}?\n\n"
        "Напишите сюжет (например: 'Путешествие в космос на динозавре', 'Спасение подводного города', 'Школа магии')."
    )
    await state.set_state(StoryForm.waiting_for_theme)

@dp.message(StoryForm.waiting_for_theme)
async def process_theme(message: types.Message, state: FSMContext):
    await state.update_data(story_theme=message.text)
    await message.answer("Отлично! Теперь отправьте четкое фото ребенка — я использую его для создания персонажа.")
    await state.set_state(StoryForm.waiting_for_photo)

@dp.message(StoryForm.waiting_for_photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    child_name = data['child_name']
    story_theme = data['story_theme']
    
    await message.answer("Пишу волшебную книгу и генерирую персональные иллюстрации... Это займет около 2–3 минут.")

    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    photo_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

    pages, error_msg = await generate_full_book(child_name, story_theme)
    if error_msg:
        await message.answer(f"Ошибка OpenAI:\n`{error_msg}`", parse_mode="Markdown")

    generated_book_data = []

    for idx, page in enumerate(pages, 1):
        story_text = page.get("text", "")
        img_prompt = page.get("prompt", "")

        image_url, img_err = await generate_image(photo_url, img_prompt)
        if img_err and idx == 1:
            await message.answer(f"Предупреждение Replicate (используется резервная генерация):\n`{img_err}`", parse_mode="Markdown")

        header = f"Страница {idx}/10\n\n{story_text}"
        
        if image_url:
            await message.answer_photo(photo=image_url, caption=header)
        else:
            await message.answer(header)
            
        generated_book_data.append({
            "page": idx,
            "text": story_text,
            "image_url": image_url
        })
        
        await asyncio.sleep(1)

    await message.answer("Верстаем вашу красочную PDF-книгу...")

    pdf_bytes = await build_pdf_book_html(child_name, story_theme, generated_book_data)
    
    if pdf_bytes:
        document = BufferedInputFile(pdf_bytes, filename=f"Сказка_{child_name}.pdf")
        await message.answer_document(
            document=document, 
            caption=f"Ваша иллюстрированная книга про {child_name} готова!",
            reply_markup=get_restart_keyboard()
        )
    else:
        await message.answer("Не удалось собрать PDF, но вы можете прочитать сказку выше!", reply_markup=get_restart_keyboard())

    await state.clear()

async def generate_full_book(name: str, theme: str):
    try:
        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        prompt = f"""
        Создай детскую сказку из 10 страниц про ребенка по имени {name}.
        Сюжет сказки: {theme}.
        
        Ответь СТРОГО в формате JSON-массива из 10 объектов без лишнего текста.
        Каждый объект должен содержать:
        - "text": текст страницы (2-4 предложения).
        - "prompt": описание сцены на английском языке в стиле 3D Pixar img.
        """
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        pages = data.get("pages", data.get("book", list(data.values())[0]))
        return pages, None
    except Exception as e:
        print(f"Ошибка книги OpenAI: {e}")
        fallback = [{"text": f"Страница {i}: {name} продолжал свое приключение по сюжету '{theme}'!", 
                     "prompt": f"a cute child named {name} in a fairytale adventure, pixar style"} for i in range(1, 11)]
        return fallback, str(e)

def _run_replicate_flux(prompt: str):
    output = replicate.run(
        "black-forest-labs/flux-schnell",
        input={
            "prompt": f"A 3D Pixar style children's book illustration, {prompt}, magical bright colors, high quality, 8k",
            "num_inference_steps": 4,
            "aspect_ratio": "1:1"
        }
    )
    res = output[0] if isinstance(output, list) else output
    return str(res)

async def generate_image(face_image_url: str, prompt: str):
    # Попытка 1: Replicate
    try:
        res_url = await asyncio.to_thread(_run_replicate_flux, prompt)
        if res_url:
            return res_url, None
    except Exception as e:
        print(f"Ошибка Replicate: {e}")

    # Попытка 2: Гарантированная резервная генерация через Pollinations AI
    try:
        styled_prompt = f"3D Pixar style children book illustration, {prompt}, vibrant fairytale colors"
        encoded = urllib.parse.quote(styled_prompt)
        fallback_url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true"
        return fallback_url, None
    except Exception as fallback_err:
        return None, str(fallback_err)

async def build_pdf_book_html(name: str, theme: str, book_data: list):
    try:
        # Генерируем красивую фоновую обложку в тему сказки
        cover_bg, _ = await generate_image("", f"magical cover art for children book about {theme}, vibrant colors, pixar style 3d")
        cover_bg_style = f"background-image: url('{cover_bg}'); background-size: cover; background-position: center;" if cover_bg else "background: linear-gradient(135deg, #2b1055 0%, #7597de 100%);"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                @page {{
                    size: A4 portrait;
                    margin: 0;
                }}
                *, *::before, *::after {{
                    box-sizing: border-box;
                }}
                body {{
                    margin: 0;
                    padding: 0;
                    font-family: 'DejaVu Sans', sans-serif;
                    color: #ffffff;
                }}
                .cover-page {{
                    width: 210mm;
                    height: 297mm;
                    position: relative;
                    {cover_bg_style}
                    page-break-after: always;
                }}
                .cover-overlay {{
                    position: absolute;
                    inset: 0;
                    background: linear-gradient(to bottom, rgba(0,0,0,0.2) 0%, rgba(15,12,27,0.85) 100%);
                    display: flex;
                    flex-direction: column;
                    justify-content: flex-end;
                    padding: 25mm 20mm;
                    text-align: center;
                }}
                .cover-title {{
                    font-size: 34pt;
                    font-weight: bold;
                    color: #ffe600;
                    text-shadow: 2px 4px 10px rgba(0,0,0,0.9);
                    margin-bottom: 5mm;
                }}
                .cover-subtitle {{
                    font-size: 20pt;
                    color: #ffffff;
                    text-shadow: 1px 2px 6px rgba(0,0,0,0.9);
                }}
                .story-page {{
                    width: 210mm;
                    height: 297mm;
                    position: relative;
                    page-break-after: always;
                    background-color: #0f0c1b;
                    overflow: hidden;
                }}
                .full-bg-image {{
                    position: absolute;
                    top: 0;
                    left: 0;
                    width: 210mm;
                    height: 297mm;
                    object-fit: cover;
                }}
                .text-overlay {{
                    position: absolute;
                    bottom: 0;
                    left: 0;
                    right: 0;
                    padding: 25mm 18mm 18mm 18mm;
                    background: linear-gradient(to top, rgba(15, 12, 27, 0.95) 75%, rgba(15, 12, 27, 0) 100%);
                    color: #ffffff;
                }}
                .page-number {{
                    font-size: 11pt;
                    font-weight: bold;
                    color: #ffe600;
                    margin-bottom: 3mm;
                    text-transform: uppercase;
                    letter-spacing: 1px;
                }}
                .page-text {{
                    font-size: 14pt;
                    line-height: 1.6;
                    color: #ffffff;
                    text-shadow: 1px 1px 4px rgba(0,0,0,0.9);
                }}
            </style>
        </head>
        <body>
            <div class="cover-page">
                <div class="cover-overlay">
                    <div class="cover-title">Сказка про {name}</div>
                    <div class="cover-subtitle">{theme}</div>
                </div>
            </div>
        """

        for item in book_data:
            img_src = item['image_url'] if item['image_url'] else ""
            html_content += f"""
            <div class="story-page">
                {"<img class='full-bg-image' src='" + img_src + "'/>" if img_src else ""}
                <div class="text-overlay">
                    <div class="page-number">Страница {item['page']}</div>
                    <div class="page-text">{item['text']}</div>
                </div>
            </div>
            """

        html_content += """
        </body>
        </html>
        """

        pdf_bytes = HTML(string=html_content).write_pdf()
        return pdf_bytes

    except Exception as e:
        print(f"Ошибка генерации HTML-PDF: {e}")
        return None

# --- Настройки HTTP-сервера и пинга для Render ---
async def handle_health_check(request):
    return web.Response(text="Book Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(RENDER_URL) as resp:
                    print(f"Self-ping status: {resp.status}")
        except Exception as e:
            print(f"Ping failed: {e}")

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # 1. Запуск фейкового веб-сервера и фонового пинга
    await start_web_server()
    asyncio.create_task(keep_alive())

    # 2. Очищаем вебхук перед стартом поллинга
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Webhook successfully deleted")
    except Exception as e:
        print(f"Error deleting webhook: {e}")

    # 3. Устанавливаем меню команд
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать сначала / Новая сказка")
    ])

    # 4. Запускаем поллинг
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
