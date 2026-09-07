import asyncio
import logging
import os
import json
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import openai
import replicate

# --- КЛЮЧИ ---
BOT_TOKEN = "8665857884:AAHi6b9NZWqZhM_gUmiRJwcCxzak3TewEls"
OPENAI_API_KEY = "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"
REPLICATE_API_TOKEN = "r8_cDVzEaXQy7tlWRvS7RJIZZr7edkHMMG457NkK"

os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Состояния диалога
class StoryForm(StatesGroup):
    waiting_for_name = State()
    waiting_for_theme = State()
    waiting_for_photo = State()

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await message.answer("Привет! Я создаю персональные иллюстрированные книги для детей.\n\nКак зовут главного героя книги?")
    await state.set_state(StoryForm.waiting_for_name)

@dp.message(StoryForm.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(child_name=message.text)
    await message.answer(f"Замечательно! О чем будет сказка про {message.text}?\n\nНапишите сюжет (например: 'Путешествие в космос на динозавре', 'Спасение подводного города', 'Школа магии').")
    await state.set_state(StoryForm.waiting_for_theme)

@dp.message(StoryForm.waiting_for_theme)
async def process_theme(message: types.Message, state: FSMContext):
    await state.update_data(story_theme=message.text)
    await message.answer("Отлично! Теперь отправьте четкое фото ребенка — я использую его для создания иллюстраций к каждой странице.")
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

    # Генерация книги (10 страниц)
    pages = await generate_full_book(child_name, story_theme)

    for idx, page in enumerate(pages, 1):
        story_text = page.get("text", "")
        img_prompt = page.get("prompt", "")

        # Генерация уникальной картинки для каждой страницы
        image_url = await generate_image(photo_url, img_prompt)

        header = f" Страница {idx}/10\n\n{story_text}"
        
        if image_url:
            await message.answer_photo(photo=image_url, caption=header)
        else:
            await message.answer(header)
        
        await asyncio.sleep(1)  # Пауза между отправкой страниц

    await message.answer(" Конец сказки! Надеюсь, малышу понравилась книга.")
    await state.clear()

async def generate_full_book(name: str, theme: str):
    try:
        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        prompt = f"""
        Создай детскую сказку из 10 страниц про ребенка по имени {name}.
        Сюжет сказки: {theme}.
        
        Ответь СТРОГО в формате JSON-массива из 10 объектов без лишнего текста.
        Каждый объект должен содержать:
        - "text": текст страницы (2-4 предложения, добрые, интересные).
        - "prompt": подробное описание сцены на английском языке в стиле 3D Pixar для создания картинки, например: "A 3D Pixar style illustration of a cute child {name} space suit, flying on a blue dinosaur in outer space, high detail".
        
        Пример структуры:
        [
          {{"text": "Страница 1...", "prompt": "a cute child..."}},
          ...
        ]
        """
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        return data.get("pages", data.get("book", list(data.values())[0]))
    except Exception as e:
        print(f"Ошибка книги OpenAI: {e}")
        # Резервные 10 страниц, если упал OpenAI API
        return [{"text": f"Страница {i}: {name} продолжал свое удивительное приключение по сюжету '{theme}'!", 
                 "prompt": f"a cute child named {name} in a fairytale adventure, page {i}, pixar style"} for i in range(1, 11)]

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
        return output[0] if isinstance(output, list) else output
    except Exception as e:
        print(f"Ошибка Replicate: {e}")
        return None

# Фейковый веб-сервер для Render
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

async def main():
    logging.basicConfig(level=logging.INFO)
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
