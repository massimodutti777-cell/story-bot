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

# --- КЛЮЧИ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN")

RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://story-bot-34cj.onrender.com")

if REPLICATE_API_TOKEN:
    os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

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
    
    await message.answer("Создаю сказку и обрабатываю лицо персонажа... Это займет около 2–3 минут.")

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

        image_url = None
        for attempt in range(2):
            try:
                image_url, img_err = await generate_image_with_faceswap(photo_url, img_prompt, child_name)
                if image_url:
                    break
            except Exception as e:
                print(f"Error generating image page {idx}, attempt {attempt}: {e}")

        header = f"Страница {idx}/10\n\n{story_text}"
        
        try:
            if image_url:
                await message.answer_photo(photo=image_url, caption=header)
            else:
                await message.answer(header)
        except Exception as send_err:
            print(f"Error sending photo on page {idx}: {send_err}")
            await message.answer(header)
            
        generated_book_data.append({
            "page": idx,
            "text": story_text,
            "image_url": image_url
        })
        
        await asyncio.sleep(0.5)

    await message.answer("Верстаем вашу красочную PDF-книгу...")

    pdf_bytes = await build_pdf_book_html(child_name, story_theme, generated_book_data, photo_url)
    
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
        
        ОБЯЗАТЕЛЬНОЕ УСЛОВИЕ: Ребенок {name} должен быть главным действующим лицом КАЖДОЙ страницы!
        
        Ответь СТРОГО в формате JSON-массива из 10 объектов без лишнего текста.
        Каждый объект должен содержать:
        - "text": текст страницы (2-4 предложения).
        - "prompt": описание сцены на английском языке (например: "human child standing near a cute magical creature").
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
                     "prompt": "a human child in a fairytale adventure, 3d pixar style"} for i in range(1, 11)]
        return fallback, str(e)

def _run_replicate_flux_and_swap(face_url: str, prompt: str, name: str):
    # 1. Генерация качественной сцены без анатомических мутаций (без хвостов у детей)
    full_prompt = f"3D Pixar style animation screenshot, a cute human child boy named {name}, human body with normal clothes, no tail on human, {prompt}, bright sunny magical lighting, sharp clear focus, 8k render"
    
    base_image = replicate.run(
        "black-forest-labs/flux-schnell",
        input={
            "prompt": full_prompt,
            "num_inference_steps": 4,
            "aspect_ratio": "1:1"
        }
    )
    base_url = str(base_image[0] if isinstance(base_image, list) else base_image)

    # 2. Наложение оригинального лица ребенка с фото
    try:
        swapped_image = replicate.run(
            "lucataco/faceswap:9a429854842207b8f5c16b22f082e0e4178a5712f86237dd1d51a6be12cf73d2",
            input={
                "target_image": base_url,
                "source_image": face_url
            }
        )
        res_url = str(swapped_image[0] if isinstance(swapped_image, list) else swapped_image)
        return res_url
    except Exception as swap_err:
        print(f"FaceSwap failed, using base image: {swap_err}")
        return base_url

async def generate_image_with_faceswap(face_url: str, prompt: str, name: str):
    try:
        res_url = await asyncio.wait_for(
            asyncio.to_thread(_run_replicate_flux_and_swap, face_url, prompt, name),
            timeout=50.0
        )
        if res_url:
            return res_url, None
    except Exception as e:
        print(f"Replicate generation error: {e}")
        return None, str(e)

async def build_pdf_book_html(name: str, theme: str, book_data: list, face_url: str):
    try:
        cover_bg, _ = await generate_image_with_faceswap(face_url, f"magical title cover art for children book about {theme}", name)
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
                    background: linear-gradient(to bottom, rgba(0,0,0,0.1) 0%, rgba(15,12,27,0.85) 100%);
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

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Webhook successfully deleted")
    except Exception as e:
        print(f"Error deleting webhook: {e}")

    await bot.set_my_commands([
        BotCommand(command="start", description="Начать сначала / Новая сказка")
    ])

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
