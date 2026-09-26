import asyncio
import logging
import os
import json
import io
import urllib.parse
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, 
    BotCommand, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
)
import openai
import replicate
from weasyprint import HTML

# --- КЛЮЧИ (из переменных окружения Render) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN")

RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://story-bot-34cj.onrender.com")

if REPLICATE_API_TOKEN:
    os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Семафор для предотвращения лимитов Replicate
replicate_semaphore = asyncio.Semaphore(2)

# Хранилище сгенерированных книг (для команды /my_books и регенерации страниц)
USER_BOOKS = {}

# FSM Состояния опроса
class StoryForm(StatesGroup):
    waiting_for_name = State()
    waiting_for_style = State()
    waiting_for_theme = State()
    waiting_for_gender = State()
    waiting_for_age = State()
    waiting_for_hair = State()
    waiting_for_eyes = State()
    waiting_for_clothes = State()
    waiting_for_companion = State()
    waiting_for_photo = State()

# Клавиатуры
def get_restart_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Создать новую сказку")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_style_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎨 3D Pixar / Disney"), KeyboardButton(text="🖌 Акварельная сказка")],
            [KeyboardButton(text="✏️ Карандашная графика"), KeyboardButton(text="👾 2D Мультфильм")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_gender_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Мальчик"), KeyboardButton(text="Девочка")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_eyes_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Карие"), KeyboardButton(text="Голубые"), KeyboardButton(text="Зеленые")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_reframe_inline_keyboard(page_idx: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=f"🔄 Перерисовать страницу {page_idx}", callback_data=f"redraw_{page_idx}")
        ]]
    )

@dp.message(CommandStart())
@dp.message(F.text == "Создать новую сказку")
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я создаю волшебные иллюстрированные книги для детей.\n\n"
        "Как зовут главного героя книги? **Напишите имя в винительном падеже** (отвечая на вопрос «Про кого?» — например: *Фёдора*, *Алису*, *Мишу*):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(StoryForm.waiting_for_name)

@dp.message(Command("my_books"))
async def cmd_my_books(message: types.Message):
    user_id = message.from_user.id
    if user_id in USER_BOOKS and USER_BOOKS[user_id]:
        book_info = USER_BOOKS[user_id]
        pdf_bytes = book_info.get('pdf_bytes')
        child_name = book_info.get('child_name', 'книга')
        if pdf_bytes:
            document = BufferedInputFile(pdf_bytes, filename=f"Сказка_{child_name}.pdf")
            await message.answer_document(document=document, caption=f"Ваша последняя книга про {child_name}!")
            return
    await message.answer("У вас пока нет сохраненных книг. Нажмите 'Создать новую сказку'!", reply_markup=get_restart_keyboard())

@dp.message(StoryForm.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(child_name=message.text.strip())
    await message.answer(
        "Выберите визуальный стиль иллюстраций для вашей книги:",
        reply_markup=get_style_keyboard()
    )
    await state.set_state(StoryForm.waiting_for_style)

@dp.message(StoryForm.waiting_for_style)
async def process_style(message: types.Message, state: FSMContext):
    await state.update_data(art_style=message.text.strip())
    data = await state.get_data()
    child_name = data['child_name']
    await message.answer(
        f"Отлично! О чем будет сказка про {child_name}?\n\n"
        "Напишите сюжет (например: 'Путешествие в космос на динозавре', 'Спасение подводного города', 'Школа магии').",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(StoryForm.waiting_for_theme)

@dp.message(StoryForm.waiting_for_theme)
async def process_theme(message: types.Message, state: FSMContext):
    await state.update_data(story_theme=message.text)
    await message.answer("Выберите пол ребенка:", reply_markup=get_gender_keyboard())
    await state.set_state(StoryForm.waiting_for_gender)

@dp.message(StoryForm.waiting_for_gender)
async def process_gender(message: types.Message, state: FSMContext):
    await state.update_data(gender=message.text)
    await message.answer(
        "Укажите возраст ребенка (например: '4 года', '6 лет'):",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(StoryForm.waiting_for_age)

@dp.message(StoryForm.waiting_for_age)
async def process_age(message: types.Message, state: FSMContext):
    await state.update_data(age=message.text)
    await message.answer("Опишите цвет и тип волос ребенка (например: 'короткие медные рыжие', 'темные кудрявые'):")
    await state.set_state(StoryForm.waiting_for_hair)

@dp.message(StoryForm.waiting_for_hair)
async def process_hair(message: types.Message, state: FSMContext):
    await state.update_data(hair=message.text)
    await message.answer("Выберите цвет глаз:", reply_markup=get_eyes_keyboard())
    await state.set_state(StoryForm.waiting_for_eyes)

@dp.message(StoryForm.waiting_for_eyes)
async def process_eyes(message: types.Message, state: FSMContext):
    await state.update_data(eyes=message.text)
    await message.answer(
        "Опишите во что одет персонаж (например: 'красная футболка, синие штаны'):",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(StoryForm.waiting_for_clothes)

@dp.message(StoryForm.waiting_for_clothes)
async def process_clothes(message: types.Message, state: FSMContext):
    await state.update_data(clothes=message.text)
    await message.answer(
        "Есть ли у героя спутник или питомец? (Напишите, например: 'собака корги', 'маленький зеленый дракон' или напишите 'нет'):"
    )
    await state.set_state(StoryForm.waiting_for_companion)

@dp.message(StoryForm.waiting_for_companion)
async def process_companion(message: types.Message, state: FSMContext):
    companion_text = message.text.strip()
    if companion_text.lower() in ["нет", "no", "none", "-"]:
        companion_text = ""
    await state.update_data(companion=companion_text)
    await message.answer("Отлично! Теперь отправьте фото ребенка (или отправьте любой текст, чтобы пропустить).")
    await state.set_state(StoryForm.waiting_for_photo)

@dp.message(StoryForm.waiting_for_photo)
async def process_photo_and_generate(message: types.Message, state: FSMContext):
    data = await state.get_data()
    child_name = data['child_name']
    story_theme = data['story_theme']
    gender = data.get('gender', 'Мальчик')
    age = data.get('age', '5 лет')
    hair_ru = data.get('hair', 'короткие волосы')
    eyes_ru = data.get('eyes', 'карие')
    clothes_ru = data.get('clothes', 'детская одежда')
    companion_ru = data.get('companion', '')
    art_style_ru = data.get('art_style', '3D Pixar')

    await message.answer("Перевожу параметры, создаю иллюстрации и сочиняю сказку... Это займет около 2–3 минут.")

    appearance_en, style_prompt_en = await translate_appearance_and_style(
        gender, age, hair_ru, eyes_ru, clothes_ru, companion_ru, art_style_ru, child_name
    )

    pages, error_msg = await generate_full_book(child_name, story_theme)
    if error_msg:
        await message.answer(f"Ошибка OpenAI:\n`{error_msg}`", parse_mode="Markdown")

    generated_book_data = []
    full_story_text_for_audio = ""

    for idx, page in enumerate(pages, 1):
        story_text = page.get("text", "")
        scene_prompt = page.get("prompt", "")
        has_character = page.get("has_character", True)

        full_story_text_for_audio += f"Страница {idx}. {story_text}\n\n"

        if has_character:
            final_prompt = f"{style_prompt_en}, {appearance_en}, {scene_prompt}, cheerful fairytale atmosphere, bright sunny magical lighting, highly detailed 8k"
        else:
            final_prompt = f"{style_prompt_en} landscape cinematic scene illustration without human character, {scene_prompt}, bright fairytale lighting, highly detailed 8k"

        image_url = await generate_image_flux_guaranteed(final_prompt)

        header = f"Страница {idx}/10\n\n{story_text}"
        
        sent_msg = None
        try:
            if image_url:
                sent_msg = await message.answer_photo(
                    photo=image_url, 
                    caption=header, 
                    reply_markup=get_reframe_inline_keyboard(idx)
                )
            else:
                sent_msg = await message.answer(header)
        except Exception as send_err:
            print(f"Error sending photo on page {idx}: {send_err}")
            sent_msg = await message.answer(header)
            
        generated_book_data.append({
            "page": idx,
            "text": story_text,
            "prompt": final_prompt,
            "image_url": image_url,
            "message_id": sent_msg.message_id if sent_msg else None
        })
        
        await asyncio.sleep(1.2)

    # Сохраняем историю пользователя в глобальное хранилище
    USER_BOOKS[message.from_user.id] = {
        "child_name": child_name,
        "story_theme": story_theme,
        "appearance_en": appearance_en,
        "style_prompt_en": style_prompt_en,
        "book_data": generated_book_data,
        "pdf_bytes": None
    }

    await message.answer("Озвучиваем аудиоверсию сказки...")
    audio_bytes = await generate_audio_story(full_story_text_for_audio)
    if audio_bytes:
        audio_file = BufferedInputFile(audio_bytes, filename=f"Аудиосказка_{child_name}.mp3")
        await message.answer_audio(audio=audio_file, caption=f"🎧 Аудиокнига про {child_name}")

    await message.answer("Верстаем вашу красочную PDF-книгу...")
    pdf_bytes = await build_pdf_book_html(child_name, story_theme, generated_book_data, appearance_en, style_prompt_en)
    
    if pdf_bytes:
        USER_BOOKS[message.from_user.id]["pdf_bytes"] = pdf_bytes
        document = BufferedInputFile(pdf_bytes, filename=f"Сказка_{child_name}.pdf")
        await message.answer_document(
            document=document, 
            caption=f"Ваша иллюстрированная книга про {child_name} готова!",
            reply_markup=get_restart_keyboard()
        )
    else:
        await message.answer("Не удалось собрать PDF, но вы можете прочитать сказку выше!", reply_markup=get_restart_keyboard())

    await state.clear()

# Обработка нажатия кнопки "Перерисовать страницу"
@dp.callback_query(F.data.startswith("redraw_"))
async def callback_redraw_page(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in USER_BOOKS or not USER_BOOKS[user_id]:
        await callback.answer("Данные книги не найдены, создайте новую сказку.")
        return

    page_idx = int(callback.data.split("_")[1])
    book_info = USER_BOOKS[user_id]
    book_data = book_info["book_data"]

    page_item = next((item for item in book_data if item["page"] == page_idx), None)
    if not page_item:
        await callback.answer("Страница не найдена.")
        return

    await callback.answer(f"Перерисовываем страницу {page_idx}...")
    new_prompt = f"{page_item['prompt']}, alternative variation, unique camera angle"
    new_image_url = await generate_image_flux_guaranteed(new_prompt)

    if new_image_url:
        page_item["image_url"] = new_image_url
        header = f"Страница {page_idx}/10 (Обновлено)\n\n{page_item['text']}"
        try:
            await callback.message.edit_media(
                media=types.InputMediaPhoto(media=new_image_url, caption=header),
                reply_markup=get_reframe_inline_keyboard(page_idx)
            )
        except Exception as e:
            print(f"Edit media failed: {e}")
            
        # Обновляем сохраненный PDF
        pdf_bytes = await build_pdf_book_html(
            book_info["child_name"], book_info["story_theme"], book_data, 
            book_info["appearance_en"], book_info["style_prompt_en"]
        )
        if pdf_bytes:
            USER_BOOKS[user_id]["pdf_bytes"] = pdf_bytes

async def generate_audio_story(text: str) -> bytes:
    """Генерация аудиофайла через OpenAI TTS"""
    try:
        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        response = await client.audio.speech.create(
            model="tts-1",
            voice="alloy",
            input=text
        )
        return response.content
    except Exception as e:
        print(f"TTS Error: {e}")
        return None

async def translate_appearance_and_style(gender: str, age: str, hair: str, eyes: str, clothes: str, companion: str, art_style: str, name: str):
    try:
        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": f"Translate these details to English prompts. Gender: {gender}, Age: {age}, Hair: {hair}, Eyes: {eyes}, Clothes: {clothes}, Companion: {companion}, Style: {art_style}.\nFormat output strictly as JSON with keys 'appearance' and 'style_prompt'."
            }],
            response_format={"type": "json_object"}
        )
        data = json.loads(response.choices[0].message.content)
        return data.get("appearance", f"a cute young {name}"), data.get("style_prompt", "3D Pixar style")
    except Exception as e:
        print(f"Translation error: {e}")
        return f"a cute young {name}, {hair} hair, {eyes} eyes, {clothes}", "3D Pixar style animation"

async def generate_full_book(name: str, theme: str):
    try:
        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        prompt = f"""
        Создай детскую сказку из 10 страниц про ребенка по имени {name}.
        Сюжет сказки: {theme}.
        
        ТРЕБОВАНИЯ К ИЛЛЮСТРАЦИЯМ:
        - На некоторых страницах должен присутствовать герой.
        - На некоторых страницах изображай только окружение/предметы по сюжету.
        
        Ответь СТРОГО в формате JSON с ключом "pages", содержащим массив из 10 объектов без лишнего текста.
        Каждый объект должен содержать:
        - "text": текст страницы (2-4 предложения).
        - "prompt": описание сцены на английском языке.
        - "has_character": boolean.
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
                     "prompt": "fairytale world", "has_character": (i % 2 != 0)} for i in range(1, 11)]
        return fallback, str(e)

def _run_flux(full_prompt: str, num_steps: int = 4):
    output = replicate.run(
        "black-forest-labs/flux-schnell",
        input={
            "prompt": full_prompt,
            "num_inference_steps": num_steps,
            "aspect_ratio": "1:1"
        }
    )
    res = output[0] if isinstance(output, list) else output
    return str(res)

async def generate_image_flux_guaranteed(full_prompt: str) -> str:
    async with replicate_semaphore:
        for attempt in range(1, 4):
            try:
                res_url = await asyncio.wait_for(
                    asyncio.to_thread(_run_flux, full_prompt, 4),
                    timeout=50.0
                )
                if res_url:
                    return res_url
            except Exception as e:
                print(f"Flux attempt {attempt} error: {e}")
                await asyncio.sleep(2.0 * attempt)

        try:
            res_url = await asyncio.wait_for(
                asyncio.to_thread(_run_flux, full_prompt, 2),
                timeout=30.0
            )
            if res_url:
                return res_url
        except Exception as err:
            print(f"Ultra-fallback error: {err}")

        return None

async def build_pdf_book_html(name: str, theme: str, book_data: list, appearance: str, style_prompt: str):
    try:
        cover_prompt = f"{style_prompt} magical cover art for children book, {appearance}, exploring {theme}, bright fairytale colors"
        cover_bg = await generate_image_flux_guaranteed(cover_prompt)
        if not cover_bg and len(book_data) > 0:
            for item in book_data:
                if item.get("image_url"):
                    cover_bg = item.get("image_url")
                    break

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
    
    await start_web_server()
    asyncio.create_task(keep_alive())

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Webhook successfully deleted")
    except Exception as e:
        print(f"Error deleting webhook: {e}")

    await bot.set_my_commands([
        BotCommand(command="start", description="Начать сначала / Новая сказка"),
        BotCommand(command="my_books", description="Мои книги / Скачать повторно")
    ])

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
