import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import openai
import replicate

# "8665857884:AAHi6b9NZWqZhM_gUmiRJwcCxzak3TewEls"
BOT_TOKEN = "8665857884:AAHi6b9NZWqZhM_gUmiRJwcCxzak3TewEls"
OPENAI_API_KEY = "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"
REPLICATE_API_TOKEN = "r8_cDVzEaXQy7tlWRvS7RJIZZr7edkHMMG457NkK"

os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class StoryForm(StatesGroup):
    waiting_for_name = State()
    waiting_for_photo = State()

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await message.answer("Привет! Я создаю персональные сказки. Как зовут ребенка?")
    await state.set_state(StoryForm.waiting_for_name)

@dp.message(StoryForm.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(child_name=message.text)
    await message.answer(f"Приятно познакомиться, {message.text}! Отправь мне фото ребенка (желательно, чтобы лицо было четко видно).")
    await state.set_state(StoryForm.waiting_for_photo)

@dp.message(StoryForm.waiting_for_photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    child_name = data['child_name']
    
    await message.answer("Придумываю сказку и рисую иллюстрацию... Занимает около 30–40 секунд.")

    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    photo_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

    story_text, image_prompt = await generate_story(child_name)
    image_result_url = await generate_image(photo_url, image_prompt)

    if image_result_url:
        await message.answer_photo(photo=image_result_url, caption=story_text)
    else:
        await message.answer(story_text)
        await message.answer("Не удалось сгенерировать картинку, но вот ваша сказка!")

    await state.clear()

async def generate_story(name: str):
    client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
    prompt = f"""
    Напиши короткую добрую сказку (до 700 символов) про ребенка по имени {name}.
    В конце отдельной строкой напиши: PROMPT: <описание главного героя и сцены на английском языка для картинки в стиле Pixar, например: a cute child {name} in a wizard hat in a fairytale forest, pixar 3d style>
    """
    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    full_text = response.choices[0].message.content
    
    if "PROMPT:" in full_text:
        story, img_prompt = full_text.split("PROMPT:")
        return story.strip(), img_prompt.strip()
    return full_text, f"A cute child named {name} in a magical forest, pixar style"

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
        print(f"Ошибка картинки: {e}")
        return None

# Фейковый веб-сервер для Render Web Service
async def handle_health_check(request):
    return web.Response(text="Bot is running!")

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

