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

# --- ReportLab для создания PDF ---
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Используем стандартный встроенный шрифт с поддержкой unicode/кириллицы в ReportLab
try:
    pdfmetrics.registerFont(TTFont('DejaVu', 'DejaVuSans.ttf'))
    PDF_FONT = 'DejaVu'
except Exception:
    PDF_FONT = 'Helvetica'

# Чтение ключей из переменных окружения Render:
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN")


RENDER_URL = "https://story-bot-34cj.onrender.com"  # Ваш URL на Render

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
        keyboard=[[KeyboardButton(text=" Создать новую сказку")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

@dp.message(CommandStart())
@dp.message(F.text == " Создать новую сказку")
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я создаю персональные иллюстрированные книги для детей.\n\nКак зовут главного героя книги?",
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
    await message.answer("Отлично! Теперь отправьте четкое фото ребенка — я использую его для создания иллюстраций.")
    await state.set_state(StoryForm.waiting_for_photo)

@dp.message(StoryForm.waiting_for_photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    child_name = data['child_name']
    story_theme = data['story_theme']
    
    await message.answer(" Пишу большую книгу на 10 страниц и генерирую иллюстрации... Это займет около 2–3 минут.")

    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    photo_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

    pages, error_msg = await generate_full_book(child_name, story_theme)
    if error_msg:
        await message.answer(f" Ошибка OpenAI:\n`{error_msg}`", parse_mode="Markdown")

    generated_book_data = []

    for idx, page in enumerate(pages, 1):
        story_text = page.get("text", "")
        img_prompt = page.get("prompt", "")

        image_url, img_err = await generate_image(photo_url, img_prompt)
        if img_err and idx == 1:
            await message.answer(f" Ошибка Replicate:\n`{img_err}`", parse_mode="Markdown")

        header = f" Страница {idx}/10\n\n{story_text}"
        
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

    await message.answer(" Собираем вашу сказку в красивый PDF-файл...")

    pdf_bytes = await build_pdf_book(child_name, story_theme, generated_book_data)
    
    if pdf_bytes:
        document = BufferedInputFile(pdf_bytes, filename=f"Сказка_{child_name}.pdf")
        await message.answer_document(
            document=document, 
            caption=f" Ваша иллюстрированная книга про {child_name} готова!",
            reply_markup=get_restart_keyboard()
        )
    else:
        await message.answer(" Не удалось собрать PDF, но вы можете прочитать сказку выше!", reply_markup=get_restart_keyboard())

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
        - "prompt": описание сцены на английском языке в стиле 3D Pixar.
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
        output = replicate.run(
            "instant-id/instant-id:41ecf0db217e20112001c203f14213773ed240578e063b218d6411d9f82d1c67",
            input={
                "image": face_image_url,
                "prompt": prompt,
                "negative_prompt": "ugly, blurry, distorted face, bad anatomy",
                "identity_weight": 0.8,
                "adapter_strength_ratio": 0.8
            }
        )
        res_url = output[0] if isinstance(output, list) else output
        return res_url, None
    except Exception as e:
        print(f"Ошибка Replicate: {e}")
        return None, str(e)

async def build_pdf_book(name: str, theme: str, book_data: list):
    try:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer, 
            pagesize=A4, 
            rightMargin=2*cm, 
            leftMargin=2*cm, 
            topMargin=2*cm, 
            bottomMargin=2*cm
        )
        
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontName=PDF_FONT, fontSize=18, alignment=1, spaceAfter=20)
        body_style = ParagraphStyle('BodyStyle', parent=styles['Normal'], fontName=PDF_FONT, fontSize=12, leading=16, spaceAfter=15)
        
        story = []
        
        story.append(Paragraph(f"Сказка про {name}", title_style))
        story.append(Paragraph(f"Тема: {theme}", body_style))
        story.append(Spacer(1, 2*cm))
        story.append(PageBreak())

        async with aiohttp.ClientSession() as session:
            for item in book_data:
                story.append(Paragraph(f"Страница {item['page']}", title_style))
                
                if item['image_url']:
                    try:
                        async with session.get(item['image_url']) as resp:
                            if resp.status == 200:
                                img_data = await resp.read()
                                img_buffer = io.BytesIO(img_data)
                                story.append(RLImage(img_buffer, width=13*cm, height=13*cm))
                                story.append(Spacer(1, 0.5*cm))
                    except Exception as img_err:
                        print(f"Ошибка картинки в PDF: {img_err}")
                
                story.append(Paragraph(item['text'], body_style))
                story.append(PageBreak())

        doc.build(story)
        buffer.seek(0)
        return buffer.getvalue()
    except Exception as e:
        print(f"Ошибка сборки PDF: {e}")
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
        BotCommand(command="start", description=" Начать сначала / Новая сказка")
    ])
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
