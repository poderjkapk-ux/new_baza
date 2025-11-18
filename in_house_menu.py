# in_house_menu.py

import html as html_module
import json
import logging
import os
from fastapi import APIRouter, Depends, HTTPException, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload
from aiogram import Bot, html as aiogram_html
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton
from urllib.parse import quote_plus as url_quote_plus

from models import Table, Product, Category, Order, Settings, Employee, OrderStatusHistory
from dependencies import get_db_session
from templates import IN_HOUSE_MENU_HTML_TEMPLATE
# --- НОВИЙ ІМПОРТ: Для розподілу на кухню/бар ---
from notification_manager import distribute_order_to_production

router = APIRouter()
logger = logging.getLogger(__name__)


async def get_admin_bot(session: AsyncSession) -> Bot | None:
    """Допоміжна функція для отримання екземпляра адмін-бота."""
    admin_bot_token = os.environ.get('ADMIN_BOT_TOKEN')
    
    if admin_bot_token:
        from aiogram.enums import ParseMode
        from aiogram.client.default import DefaultBotProperties
        return Bot(token=admin_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return None

@router.get("/menu/table/{access_token}", response_class=HTMLResponse)
async def get_in_house_menu(access_token: str, request: Request, session: AsyncSession = Depends(get_db_session)):
    """Відображає сторінку меню для конкретного столика."""

    table_res = await session.execute(
        select(Table).where(Table.access_token == access_token)
    )
    table = table_res.scalar_one_or_none()

    if not table:
        raise HTTPException(status_code=404, detail="Столик не знайдено.")

    settings = await session.get(Settings, 1) or Settings()
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

    # --- Design variables ---
    site_title = settings.site_title or "Назва"
    primary_color_val = settings.primary_color or "#5a5a5a"
    secondary_color_val = settings.secondary_color or "#eeeeee"
    background_color_val = settings.background_color or "#f4f4f4"
    font_family_sans_val = settings.font_family_sans or "Golos Text"
    font_family_serif_val = settings.font_family_serif or "Playfair Display"
    # ---------------------------------------

    return HTMLResponse(content=IN_HOUSE_MENU_HTML_TEMPLATE.format(
        table_name=html_module.escape(table.name),
        table_id=table.id,
        logo_html=logo_html,
        menu_data=menu_data,
        site_title=html_module.escape(site_title),
        seo_description=html_module.escape(settings.seo_description or ""),
        seo_keywords=html_module.escape(settings.seo_keywords or ""),
        primary_color_val=primary_color_val,
        secondary_color_val=secondary_color_val,
        background_color_val=background_color_val,
        font_family_sans_val=font_family_sans_val,
        font_family_serif_val=font_family_serif_val,
        font_family_sans_encoded=url_quote_plus(font_family_sans_val),
        font_family_serif_encoded=url_quote_plus(font_family_serif_val)
    ))

@router.post("/api/menu/table/{table_id}/call_waiter", response_class=JSONResponse)
async def call_waiter(table_id: int, session: AsyncSession = Depends(get_db_session)):
    """Обробляє виклик офіціанта зі столика."""
    table = await session.get(Table, table_id, options=[selectinload(Table.assigned_waiters)])
    if not table: raise HTTPException(status_code=404, detail="Столик не знайдено.")

    waiters = table.assigned_waiters
    message_text = f"❗️ <b>Виклик зі столика: {html_module.escape(table.name)}</b>"
    
    admin_chat_id_str = os.environ.get('ADMIN_CHAT_ID')

    admin_bot = await get_admin_bot(session)
    if not admin_bot:
        raise HTTPException(status_code=500, detail="Сервіс сповіщень недоступний.")

    try:
        target_chat_ids = set()
        for w in waiters:
            if w.telegram_user_id and w.is_on_shift:
                target_chat_ids.add(w.telegram_user_id)

        if not target_chat_ids:
            if admin_chat_id_str:
                try:
                    target_chat_ids.add(int(admin_chat_id_str))
                    message_text += "\n<i>Офіціанта не призначено або він не на зміні.</i>"
                except ValueError:
                     logger.warning(f"Некоректний admin_chat_id: {admin_chat_id_str}")

        if target_chat_ids:
            for chat_id in target_chat_ids:
                try:
                    await admin_bot.send_message(chat_id, message_text)
                except Exception as e:
                    logger.error(f"Не вдалося надіслати виклик офіціанта в чат {chat_id}: {e}")
            return JSONResponse(content={"message": "Офіціанта сповіщено. Будь ласка, зачекайте."})
        else:
            logger.error(f"Не вдалося знайти отримувача для виклику офіціанта зі столика {table_id}")
            raise HTTPException(status_code=503, detail="Не вдалося знайти отримувача для сповіщення.")
    finally:
        await admin_bot.session.close()

@router.post("/api/menu/table/{table_id}/request_bill", response_class=JSONResponse)
async def request_bill(table_id: int, session: AsyncSession = Depends(get_db_session)):
    """Обробляє запит на рахунок зі столика."""
    table = await session.get(Table, table_id, options=[selectinload(Table.assigned_waiters)])
    if not table: raise HTTPException(status_code=404, detail="Столик не знайдено.")

    waiters = table.assigned_waiters
    message_text = f"💰 <b>Запит на розрахунок зі столика: {html_module.escape(table.name)}</b>"

    admin_chat_id_str = os.environ.get('ADMIN_CHAT_ID')

    admin_bot = await get_admin_bot(session)
    if not admin_bot:
        raise HTTPException(status_code=500, detail="Сервіс сповіщень недоступний.")

    try:
        target_chat_ids = set()
        for w in waiters:
            if w.telegram_user_id and w.is_on_shift:
                target_chat_ids.add(w.telegram_user_id)

        if not target_chat_ids:
            if admin_chat_id_str:
                try:
                    target_chat_ids.add(int(admin_chat_id_str))
                    message_text += "\n<i>Офіціанта не призначено або він не на зміні.</i>"
                except ValueError:
                     logger.warning(f"Некоректний admin_chat_id: {admin_chat_id_str}")

        if target_chat_ids:
            for chat_id in target_chat_ids:
                try:
                    await admin_bot.send_message(chat_id, message_text)
                except Exception as e:
                    logger.error(f"Не вдалося надіслати запит на рахунок в чат {chat_id}: {e}")
            return JSONResponse(content={"message": "Запит надіслано. Офіціант незабаром підійде з рахунком."})
        else:
            logger.error(f"Не вдалося знайти отримувача для запиту на рахунок зі столика {table_id}")
            raise HTTPException(status_code=503, detail="Не вдалося знайти отримувача для сповіщення.")
    finally:
        await admin_bot.session.close()

@router.post("/api/menu/table/{table_id}/place_order", response_class=JSONResponse)
async def place_in_house_order(table_id: int, items: list = Body(...), session: AsyncSession = Depends(get_db_session)):
    """Обробляє нове замовлення зі столика."""
    table = await session.get(Table, table_id, options=[selectinload(Table.assigned_waiters)])
    if not table: raise HTTPException(status_code=404, detail="Столик не знайдено.")
    if not items: raise HTTPException(status_code=400, detail="Замовлення порожнє.")

    total_price = sum(item.get('price', 0) * item.get('quantity', 0) for item in items)
    products_str = ", ".join([f"{item['name']} x {item['quantity']}" for item in items])

    order = Order(
        customer_name=f"Стіл: {table.name}", phone_number=f"table_{table.id}",
        address=None, products=products_str, total_price=total_price,
        is_delivery=False, delivery_time="In House", order_type="in_house",
        table_id=table.id, status_id=1 # Статус "Новий"
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
        logger.error(f"Замовлення #{order.id} створено, але адмін-бот недоступний для сповіщення.")
        return JSONResponse(content={"message": "Замовлення прийнято! Очікуйте.", "order_id": order.id})

    kb_waiter = InlineKeyboardBuilder()
    kb_waiter.row(InlineKeyboardButton(text="✅ Прийняти замовлення", callback_data=f"waiter_accept_order_{order.id}"))

    kb_admin = InlineKeyboardBuilder()
    kb_admin.row(InlineKeyboardButton(text="⚙️ Керувати (Адмін)", callback_data=f"waiter_manage_order_{order.id}"))


    try:
        # 1. Розсилка офіціантам (персонально для цього столика)
        waiters = table.assigned_waiters
        admin_chat_id_str = os.environ.get('ADMIN_CHAT_ID')
        admin_chat_id = None
        if admin_chat_id_str:
            try: admin_chat_id = int(admin_chat_id_str)
            except ValueError: pass

        waiter_chat_ids = set()
        for w in waiters:
            if w.telegram_user_id and w.is_on_shift:
                waiter_chat_ids.add(w.telegram_user_id)

        if waiter_chat_ids:
            for chat_id in waiter_chat_ids:
                try:
                    await admin_bot.send_message(chat_id, order_details_text, reply_markup=kb_waiter.as_markup())
                except Exception as e:
                    logger.error(f"Не вдалося надіслати нове замовлення офіціанту {chat_id}: {e}")

            if admin_chat_id and admin_chat_id not in waiter_chat_ids:
                try:
                    await admin_bot.send_message(admin_chat_id, "✅ " + order_details_text, reply_markup=kb_admin.as_markup())
                except Exception as e: pass
        else:
            if admin_chat_id:
                await admin_bot.send_message(
                    admin_chat_id,
                    f"❗️ <b>Замовлення з вільного столика {aiogram_html.bold(table.name)} (ID: #{order.id})!</b>\n\n" + order_details_text,
                    reply_markup=kb_admin.as_markup()
                )

        # 2. --- НОВЕ: Розподіл на Кухню та Бар ---
        # Викликаємо функцію розподілу, яка надішле чеки поварам та барменам
        try:
            await distribute_order_to_production(admin_bot, order, session)
        except Exception as e:
            logger.error(f"Помилка при розподілі замовлення #{order.id} на кухню/бар: {e}")
            
        return JSONResponse(content={"message": "Замовлення прийнято! Офіціант незабаром його підтвердить.", "order_id": order.id})

    finally:
        await admin_bot.session.close()