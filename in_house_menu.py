# in_house_menu.py

import html as html_module
import json
import logging
import os  # <-- --- ЗМІНА 1: Додано імпорт 'os' ---
from fastapi import APIRouter, Depends, HTTPException, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
# ЗМІНЕНО: Додано selectinload
from sqlalchemy.orm import joinedload, selectinload
from aiogram import Bot, html as aiogram_html
# NEW: Import keyboard builder
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton
from urllib.parse import quote_plus as url_quote_plus # <-- NEW

# ЗМІНЕНО: Додано OrderStatusHistory
from models import Table, Product, Category, Order, Settings, Employee, OrderStatusHistory # <-- Settings is imported
from dependencies import get_db_session
# Змінено: імпортуємо новий шаблон з templates.py
from templates import IN_HOUSE_MENU_HTML_TEMPLATE

router = APIRouter()
logger = logging.getLogger(__name__)


# --- ЗМІНА 2: Функція get_admin_bot оновлена ---
async def get_admin_bot(session: AsyncSession) -> Bot | None:
    """Допоміжна функція для отримання екземпляра адмін-бота."""
    # settings = await session.get(Settings, 1) # <-- ВИДАЛЕНО
    admin_bot_token = os.environ.get('ADMIN_BOT_TOKEN') # <-- ДОДАНО
    
    if admin_bot_token: # <-- ЗМІНЕНО
        from aiogram.enums import ParseMode
        from aiogram.client.default import DefaultBotProperties
        return Bot(token=admin_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) # <-- ЗМІНЕНО
    return None
# --- КІНЕЦЬ ЗМІНИ 2 ---

# --- ПОЧАТОК ЗМІНИ: Ендпоінт приймає access_token ---
@router.get("/menu/table/{access_token}", response_class=HTMLResponse)
async def get_in_house_menu(access_token: str, request: Request, session: AsyncSession = Depends(get_db_session)):
    """Відображає сторінку меню для конкретного столика."""

    # Змінено: Шукаємо столик за access_token, а не за ID
    table_res = await session.execute(
        select(Table).where(Table.access_token == access_token)
    )
    table = table_res.scalar_one_or_none()
    # --- КІНЕЦЬ ЗМІНИ ---

    if not table:
        raise HTTPException(status_code=404, detail="Столик не знайдено.")

    settings = await session.get(Settings, 1) or Settings() # <-- MODIFIED: Fetch settings
    logo_html = f'<img src="/{settings.logo_url}" alt="Логотип" class="header-logo">' if settings and settings.logo_url else ''

    # Отримуємо меню, яке показується в ресторані
    categories_res = await session.execute(
        select(Category)
        .where(Category.show_in_restaurant == True)
        .order_by(Category.sort_order, Category.name)
    )
    products_res = await session.execute(
        select(Product)
        .join(Category)
        .where(Product.is_active == True, Category.show_in_restaurant == True)
    )

    categories = [{"id": c.id, "name": c.name} for c in categories_res.scalars().all()]
    products = [{"id": p.id, "name": p.name, "description": p.description, "price": p.price, "image_url": p.image_url, "category_id": p.category_id} for p in products_res.scalars().all()]

    # Передаємо дані меню в шаблон через JSON
    menu_data = json.dumps({"categories": categories, "products": products})

    # --- NEW: Prepare design/SEO variables ---
    site_title = settings.site_title or "Назва"
    primary_color_val = settings.primary_color or "#5a5a5a"
    font_family_sans_val = settings.font_family_sans or "Golos Text"
    font_family_serif_val = settings.font_family_serif or "Playfair Display"
    # ---------------------------------------

    # ВАЖЛИВО: Ми передаємо table.id в шаблон, а не access_token.
    # Це безпечно, оскільки table.id використовується для внутрішніх API-запитів,
    # а не для URL, який можна вгадати.
    return HTMLResponse(content=IN_HOUSE_MENU_HTML_TEMPLATE.format(
        table_name=html_module.escape(table.name),
        table_id=table.id,
        logo_html=logo_html,
        menu_data=menu_data,
        site_title=html_module.escape(site_title),
        seo_description=html_module.escape(settings.seo_description or ""),
        seo_keywords=html_module.escape(settings.seo_keywords or ""),
        primary_color_val=primary_color_val,
        font_family_sans_val=font_family_sans_val,
        font_family_serif_val=font_family_serif_val,
        font_family_sans_encoded=url_quote_plus(font_family_sans_val),
        font_family_serif_encoded=url_quote_plus(font_family_serif_val)
    ))

@router.post("/api/menu/table/{table_id}/call_waiter", response_class=JSONResponse)
async def call_waiter(table_id: int, session: AsyncSession = Depends(get_db_session)):
    """Обробляє виклик офіціанта зі столика."""
    # (Цей ендпоінт залишається без змін, він приймає table_id з JavaScript)
    # ЗМІНЕНО: Використовуємо selectinload для M2M
    table = await session.get(Table, table_id, options=[selectinload(Table.assigned_waiters)])
    if not table: raise HTTPException(status_code=404, detail="Столик не знайдено.")

    # ЗМІНЕНО: Отримуємо список офіціантів
    waiters = table.assigned_waiters
    message_text = f"❗️ <b>Виклик зі столика: {html_module.escape(table.name)}</b>"
    
    # --- ЗМІНА 3: Отримуємо ADMIN_CHAT_ID з os.environ ---
    admin_chat_id_str = os.environ.get('ADMIN_CHAT_ID')
    # --- КІНЕЦЬ ЗМІНИ 3 ---

    admin_bot = await get_admin_bot(session)
    if not admin_bot:
        raise HTTPException(status_code=500, detail="Сервіс сповіщень недоступний.")

    try:
        # ЗМІНЕНО: Логіка пошуку отримувачів (M2M)
        target_chat_ids = set()
        for w in waiters:
            if w.telegram_user_id and w.is_on_shift:
                target_chat_ids.add(w.telegram_user_id)

        # --- ЗМІНА 4: Використовуємо admin_chat_id_str ---
        if not target_chat_ids:
            # settings = await session.get(Settings, 1) # <-- ВИДАЛЕНО
            if admin_chat_id_str: # <-- ЗМІНЕНО
                try:
                    target_chat_ids.add(int(admin_chat_id_str)) # <-- ЗМІНЕНО
                    message_text += "\n<i>Офіціанта не призначено або він не на зміні.</i>"
                except ValueError:
                     logger.warning(f"Некоректний admin_chat_id: {admin_chat_id_str}")
        # --- КІНЕЦЬ ЗМІНИ 4 ---

        if target_chat_ids:
            for chat_id in target_chat_ids:
                try:
                    await admin_bot.send_message(chat_id, message_text)
                except Exception as e:
                    logger.error(f"Не вдалося надіслати виклик офіціанта в чат {chat_id}: {e}")
            return JSONResponse(content={"message": "Офіціанта сповіщено. Будь ласка, зачекайте."})
        else:
            raise HTTPException(status_code=503, detail="Не вдалося знайти отримувача для сповіщення.")
    finally:
        await admin_bot.session.close()

@router.post("/api/menu/table/{table_id}/request_bill", response_class=JSONResponse)
async def request_bill(table_id: int, session: AsyncSession = Depends(get_db_session)):
    """Обробляє запит на рахунок зі столика."""
    # (Цей ендпоіінт залишається без змін)
    # ЗМІНЕНО: Використовуємо selectinload для M2M
    table = await session.get(Table, table_id, options=[selectinload(Table.assigned_waiters)])
    if not table: raise HTTPException(status_code=404, detail="Столик не знайдено.")

    # ЗМІНЕНО: Отримуємо список офіціантів
    waiters = table.assigned_waiters
    message_text = f"💰 <b>Запит на розрахунок зі столика: {html_module.escape(table.name)}</b>"

    # --- ЗМІНА 5: Отримуємо ADMIN_CHAT_ID з os.environ ---
    admin_chat_id_str = os.environ.get('ADMIN_CHAT_ID')
    # --- КІНЕЦЬ ЗМІНИ 5 ---

    admin_bot = await get_admin_bot(session)
    if not admin_bot:
        raise HTTPException(status_code=500, detail="Сервіс сповіщень недоступний.")

    try:
        # ЗМІНЕНО: Логіка пошуку отримувачів (M2M)
        target_chat_ids = set()
        for w in waiters:
            if w.telegram_user_id and w.is_on_shift:
                target_chat_ids.add(w.telegram_user_id)

        # --- ЗМІНА 6: Використовуємо admin_chat_id_str ---
        if not target_chat_ids:
            # settings = await session.get(Settings, 1) # <-- ВИДАЛЕНО
            if admin_chat_id_str: # <-- ЗМІНЕНО
                try:
                    target_chat_ids.add(int(admin_chat_id_str)) # <-- ЗМІНЕНО
                    message_text += "\n<i>Офіціанта не призначено або він не на зміні.</i>"
                except ValueError:
                     logger.warning(f"Некоректний admin_chat_id: {admin_chat_id_str}")
        # --- КІНЕЦЬ ЗМІНИ 6 ---

        if target_chat_ids:
            for chat_id in target_chat_ids:
                try:
                    await admin_bot.send_message(chat_id, message_text)
                except Exception as e:
                    logger.error(f"Не вдалося надіслати запит на рахунок в чат {chat_id}: {e}")
            return JSONResponse(content={"message": "Запит надіслано. Офіціант незабаром підійде з рахунком."})
        else:
            raise HTTPException(status_code=503, detail="Не вдалося знайти отримувача для сповіщення.")
    finally:
        await admin_bot.session.close()

@router.post("/api/menu/table/{table_id}/place_order", response_class=JSONResponse)
async def place_in_house_order(table_id: int, items: list = Body(...), session: AsyncSession = Depends(get_db_session)):
    """Обробляє нове замовлення зі столика."""
    # (Цей ендпоіінт залишається без змін)
    # ЗМІНЕНО: Використовуємо selectinload для M2M
    table = await session.get(Table, table_id, options=[selectinload(Table.assigned_waiters)])
    if not table: raise HTTPException(status_code=404, detail="Столик не знайдено.")
    if not items: raise HTTPException(status_code=400, detail="Замовлення порожнє.")

    total_price = sum(item.get('price', 0) * item.get('quantity', 0) for item in items)
    products_str = ", ".join([f"{item['name']} x {item['quantity']}" for item in items])

    order = Order(
        customer_name=f"Стіл: {table.name}", phone_number=f"table_{table.id}",
        address=None, products=products_str, total_price=total_price,
        is_delivery=False, delivery_time="In House", order_type="in_house",
        table_id=table.id, status_id=1 # Статус "Новый"
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)

    history_entry = OrderStatusHistory(
        order_id=order.id, status_id=order.status_id,
        actor_info=f"Гість за столиком {table.name}"
    )
    session.add(history_entry)
    await session.commit()

    order_details_text = (f"📝 <b>Нове замовлення зі столика: {aiogram_html.bold(table.name)} (ID: #{order.id})</b>\n\n"
                          f"<b>Склад:</b>\n- " + aiogram_html.quote(products_str.replace(", ", "\n- ")) +
                          f"\n\n<b>Сума:</b> {total_price} грн")

    admin_bot = await get_admin_bot(session)
    if not admin_bot:
        raise HTTPException(status_code=500, detail="Сервіс сповіщень недоступний.")

    # ЗМІНЕНО: Клавіатура для офіціантів (Прийняти)
    kb_waiter = InlineKeyboardBuilder()
    kb_waiter.row(InlineKeyboardButton(text="✅ Прийняти замовлення", callback_data=f"waiter_accept_order_{order.id}"))

    # Клавіатура для адмін-чату (Керувати)
    kb_admin = InlineKeyboardBuilder()
    kb_admin.row(InlineKeyboardButton(text="⚙️ Керувати (Адмін)", callback_data=f"waiter_manage_order_{order.id}"))


    try:
        waiters = table.assigned_waiters
        
        # --- ЗМІНА 7: Отримуємо ADMIN_CHAT_ID з os.environ ---
        admin_chat_id_str = os.environ.get('ADMIN_CHAT_ID')
        # settings = await session.get(Settings, 1) # <-- ВИДАЛЕНО
        admin_chat_id = None
        if admin_chat_id_str: # <-- ЗМІНЕНО
            try:
                admin_chat_id = int(admin_chat_id_str) # <-- ЗМІНЕНО
            except ValueError:
                logger.warning(f"Некоректний admin_chat_id: {admin_chat_id_str}")
        # --- КІНЕЦЬ ЗМІНИ 7 ---


        waiter_chat_ids = set()
        for w in waiters:
            if w.telegram_user_id and w.is_on_shift:
                waiter_chat_ids.add(w.telegram_user_id)

        if waiter_chat_ids:
            # Надсилаємо сповіщення з кнопкою "Прийняти" усім офіціантам столика
            for chat_id in waiter_chat_ids:
                try:
                    await admin_bot.send_message(chat_id, order_details_text, reply_markup=kb_waiter.as_markup())
                except Exception as e:
                    logger.error(f"Не вдалося надіслати нове замовлення офіціанту {chat_id}: {e}")

            # Надсилаємо сповіщення в адмін-чат (якщо він є і це не один з офіціантів)
            if admin_chat_id and admin_chat_id not in waiter_chat_ids:
                try:
                    await admin_bot.send_message(admin_chat_id, "✅ " + order_details_text, reply_markup=kb_admin.as_markup())
                except Exception as e:
                    logger.error(f"Не вдалося надіслати копію замовлення в адмін-чат {admin_chat_id}: {e}")

            return JSONResponse(content={"message": "Замовлення прийнято! Офіціант незабаром його підтвердить.", "order_id": order.id})

        else:
            # Немає офіціантів - надсилаємо лише в адмін-чат
            if admin_chat_id:
                await admin_bot.send_message(
                    admin_chat_id,
                    f"❗️ <b>Замовлення з вільного столика {aiogram_html.bold(table.name)} (ID: #{order.id})!</b>\n\n" + order_details_text +
                    "\n\n<i>(Жоден офіціант не був на зміні або не призначений на цей столик)</i>",
                    reply_markup=kb_admin.as_markup()
                )
                return JSONResponse(content={"message": "Замовлення прийнято! Очікуйте.", "order_id": order.id})
            else:
                # Критична помилка: нікому відправити
                logger.error(f"Критична помилка: Немає ані офіціантів, ані адмін-чату для замовлення #{order.id}")
                raise HTTPException(status_code=503, detail="Не вдалося знайти отримувача для сповіщення.")
    finally:
        await admin_bot.session.close()