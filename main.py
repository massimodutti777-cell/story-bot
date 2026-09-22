import asyncio
import logging
import os
import json
import io
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
            await message.answer(f"Ошибка Replicate:\n`{img_err}`", parse_mode="Markdown")

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

async def generate_image(face_image_url: str, prompt: str):
    try:
        # Используем актуальную и быстро работающую модель FLUX.1
        output = replicate.run(
            "black-forest-labs/flux-schnell",
            input={
                "prompt": f"A 3D Pixar style children's book illustration of a cute child named protagonist, {prompt}, magical bright colors, high quality",
                "num_inference_steps": 4,
                "aspect_ratio": "1:1"
            }
        )
        res_url = output[0] if isinstance(output, list) else output
        return res_url, None
    except Exception as e:
        print(f"Ошибка Replicate: {e}")
        # Резервный вызов SDXL Lightning
        try:
            output = replicate.run(
                "bytedance/sdxl-lightning-4step:558fe9d4c646c732771168c8d388614c56e30681b613017575218d6138d62681",
                input={
                    "prompt": f"Pixar style 3D illustration, {prompt}, fairytale, vibrant colors",
                    "width": 1024,
                    "height": 1024
                }
            )
            res_url = output[0] if isinstance(output, list) else output
            return res_url, None
        except Exception as fallback_err:
            return None, str(e)

async def build_pdf_book_html(name: str, theme: str, book_data: list):
    try:
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                @page {{
                    size: A4 portrait;
                    margin: 0;
                    background-color: #1a102f;
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
                    display: block;
                    position: relative;
                    background: linear-gradient(135deg, #2b1055 0%, #7597de 100%);
                    text-align: center;
                    page-break-after: always;
                }}
                .cover-title {{
                    position: absolute;
                    top: 80mm;
                    left: 20mm;
                    right: 20mm;
                    font-size: 32pt;
                    font-weight: bold;
                    color: #ffe600;
                    text-shadow: 2px 2px 8px rgba(0,0,0,0.6);
                }}
                .cover-subtitle {{
                    position: absolute;
                    top: 130mm;
                    left: 20mm;
                    right: 20mm;
                    font-size: 18pt;
                    color: #ffffff;
                }}
                .story-page {{
                    width: 210mm;
                    height: 297mm;
                    position: relative;
                    page-break-after: always;
                    background-color: #0f0c1b;
                }}
                .page-image {{
                    position: absolute;
                    top: 15mm;
                    left: 15mm;
                    width: 180mm;
                    height: 180mm;
                    border-radius: 12px;
                    object-fit: cover;
                    box-shadow: 0 8px 20px rgba(0,0,0,0.5);
                }}
                .text-box {{
                    position: absolute;
                    top: 205mm;
                    left: 15mm;
                    width: 180mm;
                    height: 75mm;
                    background: rgba(255, 255, 255, 0.95);
                    border-radius: 12px;
                    padding: 8mm 10mm;
                    color: #1a102f;
                }}
                .page-number {{
                    font-size: 11pt;
                    font-weight: bold;
                    color: #6b21a8;
                    margin-bottom: 3mm;
                }}
                .page-text {{
                    font-size: 13pt;
                    line-height: 1.5;
                    color: #241442;
                }}
            </style>
        </head>
        <body>
            <div class="cover-page">
                <div class="cover-title">Сказка про {name}</div>
                <div class="cover-subtitle">{theme}</div>
            </div>
        """

        for item in book_data:
            img_src = item['image_url'] if item['image_url'] else ""
            html_content += f"""
            <div class="story-page">
                {"<img class='page-image' src='" + img_src + "'/>" if img_src else ""}
                <div class="text-box">
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
    await start_web_server()
    asyncio.create_task(keep_alive())
    
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать сначала / Новая сказка")
    ])

    bot.delete_webhook()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
