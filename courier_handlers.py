# courier_handlers.py

import logging
import html as html_module
from aiogram import Dispatcher, F, html, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, KeyboardButton, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from sqlalchemy.orm import joinedload
from typing import Dict, Any, Optional 
from urllib.parse import quote_plus
import re 
import os # <--- ДОБАВЛЕНО: для доступа к os.environ

# ЗМІНЕНО: Додано OrderStatusHistory, Table, Category, Product
from models import Employee, Order, OrderStatus, Settings, OrderStatusHistory, Table, Category, Product
from notification_manager import notify_all_parties_on_status_change

# НОВІ ІМПОРТИ
from aiogram import html as aiogram_html
from aiogram.utils.keyboard import InlineKeyboardButton

logger = logging.getLogger(__name__)

class StaffAuthStates(StatesGroup):
    waiting_for_phone = State()

# НОВА МАШИНА СТАНІВ (FSM) ДЛЯ СТВОРЕННЯ ЗАМОВЛЕННЯ
class WaiterCreateOrderStates(StatesGroup):
    managing_cart = State()
    choosing_category = State()
    choosing_product = State()


def get_staff_login_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="🔐 Вхід оператора"))
    builder.row(KeyboardButton(text="🚚 Вхід кур'єра"))
    builder.row(KeyboardButton(text="🤵 Вхід офіціанта"))
    # --- НОВА КНОПКА ---
    builder.row(KeyboardButton(text="🧑‍🍳 Вхід повара"))
    return builder.as_markup(resize_keyboard=True)

# НОВА ФУНКЦІЯ: Об'єднує кнопки всіх ролей (КУР'ЄР, ОФІЦІАНТ, ПОВАР)
def get_staff_keyboard(employee: Employee):
    builder = ReplyKeyboardBuilder()
    role = employee.role
    
    # 1. Кнопка управління зміною
    if employee.is_on_shift:
        builder.row(KeyboardButton(text="🔴 Завершити зміну"))
    else:
        builder.row(KeyboardButton(text="🟢 Почати зміну"))

    # 2. Рольові кнопки (тільки якщо на зміні)
    role_buttons = []
    if employee.is_on_shift:
        # Курьерські кнопки
        if role.can_be_assigned:
            role_buttons.append(KeyboardButton(text="📦 Мої замовлення"))
        # Офіціантські кнопки
        if role.can_serve_tables:
            role_buttons.append(KeyboardButton(text="🍽 Мої столики"))
        # Поварські кнопки
        if role.can_receive_kitchen_orders:
             role_buttons.append(KeyboardButton(text="🔪 Кухня"))
            
    if role_buttons:
        # Розміщуємо всі необхідні кнопки
        builder.row(*role_buttons)

    # 3. Кнопка виходу
    builder.row(KeyboardButton(text="🚪 Вийти"))
    
    return builder.as_markup(resize_keyboard=True)

# ОНОВЛЕНІ ОБГОРТКИ: Тепер приймають Employee і викликають нову функцію
def get_courier_keyboard(employee: Employee):
    return get_staff_keyboard(employee)

def get_operator_keyboard(employee: Employee):
    return get_staff_keyboard(employee)

def get_waiter_keyboard(employee: Employee):
    return get_staff_keyboard(employee)

# НОВА ФУНКЦІЯ: Показує замовлення для повара
async def show_chef_orders(message_or_callback: Message | CallbackQuery, session: AsyncSession, **kwargs: Dict[str, Any]):
    user_id = message_or_callback.from_user.id
    message = message_or_callback.message if isinstance(message_or_callback, CallbackQuery) else message_or_callback

    employee = await session.scalar(select(Employee).where(Employee.telegram_user_id == user_id).options(joinedload(Employee.role)))
    
    if not employee or not employee.role.can_receive_kitchen_orders:
         return await message.answer("❌ У вас немає прав повара.")

    if not employee.is_on_shift:
         return await message.answer("🔴 Ви не на зміні. Натисніть '🟢 Почати зміну', щоб бачити нові замовлення.")

    # --- Фільтруємо за статусами, видимими поварам ---
    kitchen_statuses_res = await session.execute(
        select(OrderStatus.id).where(OrderStatus.visible_to_chef == True)
    )
    kitchen_status_ids = kitchen_statuses_res.scalars().all()

    orders_res = await session.execute(
        select(Order).options(joinedload(Order.status), joinedload(Order.table)).where(
            Order.status_id.in_(kitchen_status_ids)
        ).order_by(Order.id.asc()) # Сортуємо від старіших до новіших
    )
    orders = orders_res.scalars().all()

    text = "🔪 <b>Замовлення на кухні:</b>\n\n"
    
    if not orders:
        text += "Наразі активних замовлень для кухні немає."
    
    kb = InlineKeyboardBuilder()
    if orders:
        for order in orders:
            status_name = order.status.name if order.status else "Невідомий"
            # Виводимо тільки склад замовлення
            products_formatted = html_module.escape(order.products or '').replace(", ", "\n")
            
            table_info = order.table.name if order.table else 'Доставка' if order.is_delivery else 'Самовивіз'

            text += (f"═════════════════\n"
                     f"<b>№{order.id}</b> ({status_name})\n"
                     f"Час: {order.created_at.strftime('%H:%M')}\n"
                     f"Тип: {table_info}\n"
                     f"<b>Склад:</b>\n{products_formatted}\n\n")
            
            # Кнопка для видачі
            kb.row(InlineKeyboardButton(text=f"✅ Видача замовлення #{order.id}", callback_data=f"chef_ready_{order.id}"))
        kb.adjust(1)
    
    try:
        if isinstance(message_or_callback, CallbackQuery):
            await message.edit_text(text, reply_markup=kb.as_markup())
            await message_or_callback.answer()
        else:
            await message.answer(text, reply_markup=kb.as_markup())
    except TelegramBadRequest as e:
         if "message is not modified" in str(e):
             await message_or_callback.answer("Дані не змінилися.")
         else:
             logger.error(f"Error in show_chef_orders: {e}")
             await message.answer(text, reply_markup=kb.as_markup())


async def show_courier_orders(message_or_callback: Message | CallbackQuery, session: AsyncSession, **kwargs: Dict[str, Any]):
    user_id = message_or_callback.from_user.id
    message = message_or_callback.message if isinstance(message_or_callback, CallbackQuery) else message_or_callback

    employee = await session.scalar(select(Employee).where(Employee.telegram_user_id == user_id).options(joinedload(Employee.role)))
    
    if not employee or not employee.role.can_be_assigned:
         return await message.answer("❌ У вас немає прав кур'єра.")

    final_statuses_res = await session.execute(
        select(OrderStatus.id).where(or_(OrderStatus.is_completed_status == True, OrderStatus.is_cancelled_status == True))
    )
    final_status_ids = final_statuses_res.scalars().all()

    orders_res = await session.execute(
        select(Order).options(joinedload(Order.status)).where(
            Order.courier_id == employee.id,
            Order.status_id.not_in(final_status_ids)
        ).order_by(Order.id.desc())
    )
    orders = orders_res.scalars().all()

    text = "🚚 <b>Ваші активні замовлення:</b>\n\n"
    if not employee.is_on_shift:
         text += "🔴 Ви не на зміні. Натисніть '🟢 Почати зміну', щоб отримувати нові замовлення.\n\n"
    if not orders:
        text += "На даний момент немає активних замовлень, призначених вам."
    
    kb = InlineKeyboardBuilder()
    if orders:
        for order in orders:
            status_name = order.status.name if order.status else "Невідомий"
            address_info = order.address if order.is_delivery else 'Самовивіз'
            text += (f"<b>Замовлення #{order.id}</b> ({status_name})\n"
                     f"📍 Адреса: {html_module.escape(address_info)}\n"
                     f"💰 Сума: {order.total_price} грн\n\n")
            kb.row(InlineKeyboardButton(text=f"Дії по замовленню #{order.id}", callback_data=f"courier_view_order_{order.id}"))
        kb.adjust(1)
    
    try:
        if isinstance(message_or_callback, CallbackQuery):
            await message.edit_text(text, reply_markup=kb.as_markup())
            await message_or_callback.answer()
        else:
            await message.answer(text, reply_markup=kb.as_markup())
    except TelegramBadRequest as e:
         if "message is not modified" in str(e):
             await message_or_callback.answer("Дані не змінилися.")
         else:
             logger.error(f"Error in show_courier_orders: {e}")
             await message.answer(text, reply_markup=kb.as_markup())

async def show_waiter_tables(message_or_callback: Message | CallbackQuery, session: AsyncSession, state: FSMContext):
    # ДОДАНО: state: FSMContext
    is_callback = isinstance(message_or_callback, CallbackQuery)
    message = message_or_callback.message if is_callback else message_or_callback
    user_id = message_or_callback.from_user.id
    
    # ДОДАНО: Очищуємо стан FSM (на випадок скасування створення замовлення)
    await state.clear()
    
    employee = await session.scalar(
        select(Employee).where(Employee.telegram_user_id == user_id).options(joinedload(Employee.role))
    )
    
    if not employee or not employee.role.can_serve_tables:
        if is_callback:
            await message_or_callback.answer("❌ У вас немає прав офіціанта.", show_alert=True)
            return
        else:
            return await message.answer("❌ У вас немає прав офіціанта.")

    if not employee.is_on_shift:
        text_off_shift = "🔴 Ви не на зміні. Почніть зміну, щоб побачити свої столики."
        if is_callback:
            await message_or_callback.answer(text_off_shift, show_alert=True)
            return
        else:
            return await message.answer(text_off_shift)

    tables_res = await session.execute(
        select(Table).where(Table.assigned_waiters.any(Employee.id == employee.id)).order_by(Table.name)
    )
    tables = tables_res.scalars().all()

    text = "🍽 <b>Закріплені за вами столики:</b>\n\n"
    kb = InlineKeyboardBuilder()
    if not tables:
        text += "За вами не закріплено жодного столика."
    else:
        for table in tables:
            kb.add(InlineKeyboardButton(text=f"Столик: {html_module.escape(table.name)}", callback_data=f"waiter_view_table_{table.id}"))
    kb.adjust(1)
    
    if is_callback:
        try:
            await message.edit_text(text, reply_markup=kb.as_markup())
        except TelegramBadRequest: 
            await message.delete()
            await message.answer(text, reply_markup=kb.as_markup())
        await message_or_callback.answer()
    else:
        await message.answer(text, reply_markup=kb.as_markup())


# ОНОВЛЕНО: Використання get_staff_keyboard в start_handler
async def start_handler(message: Message, state: FSMContext, session: AsyncSession, **kwargs: Dict[str, Any]):
    await state.clear()
    employee = await session.scalar(
        select(Employee).where(Employee.telegram_user_id == message.from_user.id).options(joinedload(Employee.role))
    )
    if employee:
        keyboard = get_staff_keyboard(employee) # Використовуємо уніфіковану клавіатуру
        await message.answer(f"🎉 Доброго дня, {employee.full_name}! Ви увійшли в режим {employee.role.name}.",
                             reply_markup=keyboard)
    else:
        await message.answer("👋 Ласкаво просимо! Використовуйте цей бот для управління замовленнями.",
                             reply_markup=get_staff_login_keyboard())

# --- ПОЧАТОК ЗМІН: Функція _generate_waiter_order_view переміщена сюди ---
# (Раніше вона була всередині register_courier_handlers)
async def _generate_waiter_order_view(order: Order, session: AsyncSession):
    await session.refresh(order, ['status', 'accepted_by_waiter', 'table']) # Додано 'table'
    status_name = order.status.name if order.status else 'Невідомий'
    products_formatted = "- " + html_module.escape(order.products or '').replace(", ", "\n- ")
    
    if order.accepted_by_waiter:
        accepted_by_text = f"<b>Прийнято:</b> {html_module.escape(order.accepted_by_waiter.full_name)}\n\n"
    else:
        accepted_by_text = "<b>Прийнято:</b> <i>Очікує...</i>\n\n"
    
    table_name = order.table.name if order.table else "N/A" # Отримуємо ім'я столика

    text = (f"<b>Керування замовленням #{order.id}</b> (Стіл: {table_name})\n\n"
            f"<b>Склад:</b>\n{products_formatted}\n\n<b>Сума:</b> {order.total_price} грн\n\n"
            f"{accepted_by_text}"
            f"<b>Поточний статус:</b> {status_name}")

    kb = InlineKeyboardBuilder()
    
    if not order.accepted_by_waiter_id:
        kb.row(InlineKeyboardButton(text="✅ Прийняти це замовлення", callback_data=f"waiter_accept_order_{order.id}"))

    statuses_res = await session.execute(
        select(OrderStatus).where(OrderStatus.visible_to_waiter == True).order_by(OrderStatus.id)
    )
    statuses = statuses_res.scalars().all()
    status_buttons = [
        InlineKeyboardButton(text=f"{'✅ ' if s.id == order.status_id else ''}{s.name}", callback_data=f"staff_set_status_{order.id}_{s.id}")
        for s in statuses
    ]
    for i in range(0, len(status_buttons), 2):
        kb.row(*status_buttons[i:i+2])

    kb.row(InlineKeyboardButton(text="✏️ Редагувати замовлення", callback_data=f"edit_order_{order.id}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад до столика", callback_data=f"waiter_view_table_{order.table_id}"))
    
    return text, kb.as_markup()
# --- КІНЕЦЬ ЗМІН ---

def register_courier_handlers(dp_admin: Dispatcher):
    dp_admin.message.register(start_handler, CommandStart())

    @dp_admin.message(F.text.in_({"🚚 Вхід кур'єра", "🔐 Вхід оператора", "🤵 Вхід офіціанта", "🧑‍🍳 Вхід повара"}))
    async def staff_login_start(message: Message, state: FSMContext, session: AsyncSession):
        user_id = message.from_user.id
        employee = await session.scalar(
            select(Employee).where(Employee.telegram_user_id == user_id).options(joinedload(Employee.role))
        )
        if employee:
            return await message.answer(f"✅ Ви вже авторизовані як {employee.role.name}. Спочатку вийдіть із системи.", 
                                        reply_markup=get_staff_login_keyboard())
        
        role_type = "unknown"
        if "кур'єра" in message.text: role_type = "courier"
        elif "оператора" in message.text: role_type = "operator"
        elif "офіціанта" in message.text: role_type = "waiter"
        elif "повара" in message.text: role_type = "chef"
            
        await state.set_state(StaffAuthStates.waiting_for_phone)
        await state.update_data(role_type=role_type)
        kb = InlineKeyboardBuilder().add(InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_auth")).as_markup()
        await message.answer(f"Будь ласка, введіть номер телефону для ролі **{role_type}**:", reply_markup=kb)

    @dp_admin.message(StaffAuthStates.waiting_for_phone)
    async def process_staff_phone(message: Message, state: FSMContext, session: AsyncSession):
        phone = message.text.strip()
        data = await state.get_data()
        role_type = data.get("role_type")
        
        employee = await session.scalar(select(Employee).options(joinedload(Employee.role)).where(Employee.phone_number == phone))
        
        role_checks = {
            "courier": lambda e: e and e.role.can_be_assigned,
            "operator": lambda e: e and e.role.can_manage_orders,
            "waiter": lambda e: e and e.role.can_serve_tables,
            "chef": lambda e: e and e.role.can_receive_kitchen_orders,
        }
        
        if role_checks.get(role_type, lambda e: False)(employee):
            employee.telegram_user_id = message.from_user.id
            await session.commit()
            await state.clear()
            
            keyboard = get_staff_keyboard(employee)
            
            await message.answer(f"🎉 Доброго дня, {employee.full_name}! Ви успішно авторизовані як {employee.role.name}.", reply_markup=keyboard)
        else:
            await message.answer(f"❌ Співробітника з таким номером не знайдено або він не має прав для ролі '{role_type}'. Спробуйте ще раз.")

    @dp_admin.callback_query(F.data == "cancel_auth")
    async def cancel_auth(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        try:
             await callback.message.edit_text("Авторизацію скасовано.")
        except Exception:
             await callback.message.delete()
             await callback.message.answer("Авторизацію скасовано.", reply_markup=get_staff_login_keyboard())
    
    @dp_admin.message(F.text.in_({"🟢 Почати зміну", "🔴 Завершити зміну"}))
    async def toggle_shift(message: Message, session: AsyncSession):
        employee = await session.scalar(
            select(Employee).where(Employee.telegram_user_id == message.from_user.id).options(joinedload(Employee.role))
        )
        if not employee: return
        is_start = message.text.startswith("🟢")
        if employee.is_on_shift == is_start:
            await message.answer(f"Ваш статус вже {'на зміні' if is_start else 'не на зміні'}.")
            return

        employee.is_on_shift = is_start
        await session.commit()
        
        action = "почали" if is_start else "завершили"
        
        keyboard = get_staff_keyboard(employee)
        
        await message.answer(f"✅ Ви успішно {action} зміну.", reply_markup=keyboard)


    @dp_admin.message(F.text == "🚪 Вийти")
    async def logout_handler(message: Message, session: AsyncSession):
        employee = await session.scalar(
            select(Employee).where(Employee.telegram_user_id == message.from_user.id)
            .options(joinedload(Employee.role), joinedload(Employee.assigned_tables))
        )
        if employee:
            employee.telegram_user_id = None
            employee.is_on_shift = False
            if employee.role.can_be_assigned:
                 employee.current_order_id = None

            await session.commit()
            await message.answer("👋 Ви вийшли з системи.", reply_markup=get_staff_login_keyboard())
        else:
            await message.answer("❌ Ви не авторизовані.")

    @dp_admin.message(F.text.in_({"📦 Мої замовлення", "🍽 Мої столики", "🔪 Кухня"}))
    async def handle_show_items_by_role(message: Message, session: AsyncSession, state: FSMContext, **kwargs: Dict[str, Any]):
        employee = await session.scalar(
            select(Employee).where(Employee.telegram_user_id == message.from_user.id).options(joinedload(Employee.role))
        )
        if not employee:
            return await message.answer("❌ Ви не авторизовані.")

        if message.text == "📦 Мої замовлення" and employee.role.can_be_assigned:
            await show_courier_orders(message, session)
        elif message.text == "🍽 Мої столики" and employee.role.can_serve_tables:
            await show_waiter_tables(message, session, state)
        # --- НОВИЙ ОБРОБНИК ---
        elif message.text == "🔪 Кухня" and employee.role.can_receive_kitchen_orders:
            await show_chef_orders(message, session)
        else:
            await message.answer("❌ Ваша роль не дозволяє переглядати ці дані.")

    @dp_admin.callback_query(F.data.startswith("courier_view_order_"))
    async def courier_view_order_details(callback: CallbackQuery, session: AsyncSession, **kwargs: Dict[str, Any]):
        order_id = int(callback.data.split("_")[3])
        order = await session.get(Order, order_id)
        if not order: return await callback.answer("Замовлення не знайдено.")

        status_name = order.status.name if order.status else 'Невідомий'
        address_info = order.address if order.is_delivery else 'Самовивіз'
        text = (f"<b>Деталі замовлення #{order.id}</b>\n\n"
                f"Статус: {status_name}\n"
                f"Адреса: {html_module.escape(address_info)}\n"
                f"Клієнт: {html_module.escape(order.customer_name)}\n"
                f"Телефон: {html_module.escape(order.phone_number)}\n" 
                f"Склад: {html_module.escape(order.products)}\n"
                f"Сума: {order.total_price} грн\n\n")
        
        kb = InlineKeyboardBuilder()
        statuses_res = await session.execute(
            select(OrderStatus).where(OrderStatus.visible_to_courier == True).order_by(OrderStatus.id)
        )
        courier_statuses = statuses_res.scalars().all()
        
        status_buttons = [
            InlineKeyboardButton(text=status.name, callback_data=f"staff_set_status_{order.id}_{status.id}")
            for status in courier_statuses
        ]
        kb.row(*status_buttons)
        
        if order.is_delivery and order.address:
            encoded_address = quote_plus(order.address)
            map_query = f"https://maps.google.com/?q={encoded_address}"
            kb.row(InlineKeyboardButton(text="🗺️ Показати на карті", url=map_query))

        kb.row(InlineKeyboardButton(text="⬅️ До моїх замовлень", callback_data="show_courier_orders_list"))
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
        await callback.answer()

    @dp_admin.callback_query(F.data == "show_courier_orders_list")
    async def back_to_list(callback: CallbackQuery, session: AsyncSession, **kwargs: Dict[str, Any]):
        await show_courier_orders(callback, session)

    # --- НОВА ФУНКЦІЯ: Повар натиснув "Видача" ---
    @dp_admin.callback_query(F.data.startswith("chef_ready_"))
    async def chef_ready_for_issuance(callback: CallbackQuery, session: AsyncSession):
        client_bot = dp_admin.get("client_bot")
        employee = await session.scalar(select(Employee).where(Employee.telegram_user_id == callback.from_user.id).options(joinedload(Employee.role)))
        order_id = int(callback.data.split("_")[-1])
        
        order = await session.get(Order, order_id, options=[joinedload(Order.status), joinedload(Order.table), joinedload(Order.courier), joinedload(Order.accepted_by_waiter)])
        if not order: return await callback.answer("Замовлення не знайдено.")

        ready_status = await session.scalar(select(OrderStatus).where(OrderStatus.name == "Готовий до видачі").limit(1))
        if not ready_status: return await callback.answer("Помилка: Статус 'Готовий до видачі' не знайдено.", show_alert=True)
        
        if order.status_id == ready_status.id: 
            return await callback.answer("Замовлення вже готове.")

        old_status_name = order.status.name
        actor_info = f"{employee.role.name}: {employee.full_name}" if employee else f"Повар (ID: {callback.from_user.id})"
        
        # 1. Зміна статусу та історії
        order.status_id = ready_status.id
        session.add(OrderStatusHistory(order_id=order.id, status_id=ready_status.id, actor_info=actor_info))
        await session.commit()
        
        # 2. Сповіщення всіх сторін (Викличе логіку notification_manager.py)
        await notify_all_parties_on_status_change(
            order=order, old_status_name=old_status_name, actor_info=actor_info,
            admin_bot=callback.bot, client_bot=client_bot, session=session
        )

        # 3. Оновлення повідомлення для повара (залишити тільки текст замовлення)
        products_formatted = html_module.escape(order.products or '').replace(", ", "\n")
        table_info = order.table.name if order.table else 'Доставка' if order.is_delivery else 'Самовивіз'
        
        done_text = (f"✅ <b>ВИДАНО (Готово): Замовлення #{order.id}</b>\n"
                     f"Тип: {table_info}\n"
                     f"Склад:\n{products_formatted}")
        
        try:
             await callback.message.edit_text(done_text, reply_markup=None)
        except TelegramBadRequest:
             pass
        
        await callback.answer(f"Замовлення #{order.id} передано на видачу.")
    # --- КІНЕЦЬ НОВОЇ ФУНКЦІЇ ---

    @dp_admin.callback_query(F.data.startswith("staff_set_status_"))
    async def staff_set_status(callback: CallbackQuery, session: AsyncSession, **kwargs: Dict[str, Any]):
        client_bot = dp_admin.get("client_bot")
        employee = await session.scalar(select(Employee).where(Employee.telegram_user_id == callback.from_user.id).options(joinedload(Employee.role)))
        actor_info = f"{employee.role.name}: {employee.full_name}" if employee else f"Співробітник (ID: {callback.from_user.id})"
        
        order_id, new_status_id = map(int, callback.data.split("_")[3:])
        
        order = await session.get(Order, order_id, options=[joinedload(Order.table)])
        if not order: return await callback.answer("Замовлення не знайдено.")
        
        new_status = await session.get(OrderStatus, new_status_id)
        if not new_status: return await callback.answer(f"Помилка: Статус не знайдено.")

        old_status_name = order.status.name if order.status else 'Невідомий'
        order.status_id = new_status.id
        alert_text = f"Статус змінено: {new_status.name}"

        if new_status.is_completed_status or new_status.is_cancelled_status:
            if employee and employee.current_order_id == order_id:
                employee.current_order_id = None
            if new_status.is_completed_status and order.courier_id:
                order.completed_by_courier_id = order.courier_id

        session.add(OrderStatusHistory(order_id=order.id, status_id=new_status.id, actor_info=actor_info))
        await session.commit()
        
        await notify_all_parties_on_status_change(
            order=order, old_status_name=old_status_name, actor_info=actor_info,
            admin_bot=callback.bot, client_bot=client_bot, session=session
        )
        await callback.answer(alert_text)
        
        if order.order_type == "in_house":
            await manage_in_house_order_handler(callback, session, order_id=order.id)
        else:
            await show_courier_orders(callback, session)
            
    # --- НОВІ ОБРОБНИКИ ДЛЯ ОФІЦІАНТА ---
    
    @dp_admin.callback_query(F.data.startswith("waiter_view_table_"))
    async def show_waiter_table_orders(callback: CallbackQuery, session: AsyncSession, state: FSMContext, table_id: int = None):
        await state.clear() # Очищуємо стан на випадок скасування
        
        if table_id is None:
            try:
                table_id = int(callback.data.split("_")[-1])
            except ValueError:
                logger.error(f"Could not extract table_id from callback: {callback.data}")
                return await callback.answer("Помилка: некоректні дані.", show_alert=True)
        
        table = await session.get(Table, table_id)
        if not table:
            return await callback.answer("Столик не знайдено!", show_alert=True)

        final_statuses_res = await session.execute(select(OrderStatus.id).where(or_(OrderStatus.is_completed_status == True, OrderStatus.is_cancelled_status == True)))
        final_statuses = final_statuses_res.scalars().all()
        
        active_orders_res = await session.execute(select(Order).where(Order.table_id == table_id, Order.status_id.not_in(final_statuses)).options(joinedload(Order.status)))
        active_orders = active_orders_res.scalars().all()

        text = f"<b>Столик: {html_module.escape(table.name)}</b>\n\nАктивні замовлення:\n"
        kb = InlineKeyboardBuilder()
        if not active_orders:
            text += "\n<i>Немає активних замовлень.</i>"
        else:
            for order in active_orders:
                kb.row(InlineKeyboardButton(
                    text=f"Замовлення #{order.id} ({order.status.name}) - {order.total_price} грн",
                    callback_data=f"waiter_manage_order_{order.id}"
                ))
        
        # НОВА КНОПКА: Створити замовлення
        kb.row(InlineKeyboardButton(text="➕ Створити замовлення", callback_data=f"waiter_create_order_{table.id}"))
        
        kb.row(InlineKeyboardButton(text="⬅️ До списку столиків", callback_data="back_to_tables_list"))
        
        try:
            await callback.message.edit_text(text, reply_markup=kb.as_markup())
        except TelegramBadRequest:
            await callback.message.delete()
            await callback.message.answer(text, reply_markup=kb.as_markup())
        await callback.answer()

    @dp_admin.callback_query(F.data == "back_to_tables_list")
    async def back_to_waiter_tables(callback: CallbackQuery, session: AsyncSession, state: FSMContext): 
        await show_waiter_tables(callback, session, state) 

    @dp_admin.callback_query(F.data.startswith("waiter_view_order_"))
    async def waiter_view_order_details(callback: CallbackQuery, session: AsyncSession):
        order_id = int(callback.data.split("_")[-1])
        await manage_in_house_order_handler(callback, session, order_id=order_id)
        

    @dp_admin.callback_query(F.data.startswith("waiter_manage_order_"))
    async def manage_in_house_order_handler(callback: CallbackQuery, session: AsyncSession, order_id: int = None):
        if not order_id:
            order_id = int(callback.data.split("_")[-1])

        order = await session.get(Order, order_id, options=[joinedload(Order.table), joinedload(Order.status), joinedload(Order.accepted_by_waiter)])
        if not order:
            return await callback.answer("Замовлення не знайдено", show_alert=True)

        # ВИКОРИСТАННЯ ГЛОБАЛЬНОЇ ФУНКЦІЇ
        text, keyboard = await _generate_waiter_order_view(order, session) 
        
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                pass
            else:
                logger.warning(f"Could not edit message in manage_in_house_order_handler: {e}. Sending new one.")
                await callback.message.delete()
                await callback.message.answer(text, reply_markup=keyboard)

        await callback.answer()

    @dp_admin.callback_query(F.data.startswith("waiter_accept_order_"))
    async def waiter_accept_order(callback: CallbackQuery, session: AsyncSession):
        order_id = int(callback.data.split("_")[-1])
        
        employee = await session.scalar(
            select(Employee).where(Employee.telegram_user_id == callback.from_user.id)
        )
        if not employee:
            return await callback.answer("Вас не знайдено в системі.", show_alert=True)

        order = await session.get(
            Order, 
            order_id, 
            options=[
                joinedload(Order.table).joinedload(Table.assigned_waiters),
                joinedload(Order.status)
            ]
        )

        if not order:
            await callback.answer("Замовлення не знайдено.", show_alert=True)
            try: await callback.message.delete()
            except TelegramBadRequest: pass
            return

        if order.accepted_by_waiter_id:
            await callback.answer("Це замовлення вже прийнято іншим офіціантом.", show_alert=True)
            await manage_in_house_order_handler(callback, session, order_id=order.id)
            return

        order.accepted_by_waiter_id = employee.id
        old_status_name = order.status.name
        
        processing_status = None
        try:
            processing_status = await session.scalar(select(OrderStatus).where(OrderStatus.name == "В обробці").limit(1))
            if processing_status:
                order.status_id = processing_status.id
                session.add(OrderStatusHistory(
                    order_id=order.id, 
                    status_id=processing_status.id, 
                    actor_info=f"Офіціант (прийняв): {employee.full_name}"
                ))
        except Exception as e:
            logger.error(f"Не вдалося автоматично змінити статус для замовлення #{order_id}: {e}")

        await session.commit()
        await callback.answer(f"Ви прийняли замовлення #{order_id}!", show_alert=False)

        await manage_in_house_order_handler(callback, session, order_id=order.id)

        notification_text = (
            f"ℹ️ Замовлення #{order.id} (Стіл: {html.escape(order.table.name)}) "
            f"було прийнято офіціантом: <b>{html.escape(employee.full_name)}</b>"
        )
        
        target_chat_ids = set()
        for w in order.table.assigned_waiters:
            if w.telegram_user_id and w.is_on_shift and w.id != employee.id:
                target_chat_ids.add(w.telegram_user_id)
        
        settings = await session.get(Settings, 1)
        admin_chat_id_str = os.environ.get('ADMIN_CHAT_ID') 
        
        if admin_chat_id_str:
            try:
                target_chat_ids.add(int(admin_chat_id_str))
            except ValueError:
                logger.warning(f"Некоректний admin_chat_id: {admin_chat_id_str}")
            
        for chat_id in target_chat_ids:
            try:
                await callback.bot.send_message(chat_id, notification_text)
            except Exception as e:
                logger.error(f"Не вдалося сповістити {chat_id} про прийняття замовлення #{order_id}: {e}")

        if processing_status and processing_status.notify_customer and order.user_id:
            client_bot = dp_admin.get("client_bot")
            if client_bot:
                try:
                    await client_bot.send_message(order.user_id, f"Ваше замовлення #{order.id} прийнято в обробку.")
                except Exception as e:
                    logger.error(f"Не вдалося сповістити клієнта {order.user_id} про прийняття замовлення: {e}")

    # --- НОВІ ОБРОБНИКИ ДЛЯ FSM СТВОРЕННЯ ЗАМОВЛЕННЯ ---

    async def _display_waiter_cart(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
        """Допоміжна функція для відображення кошика під час створення замовлення."""
        data = await state.get_data()
        cart = data.get("cart", {})
        table_name = data.get("table_name", "N/A")
        table_id = data.get("table_id")

        text = f"📝 <b>Створення замовлення для: {html_module.escape(table_name)}</b>\n\n<b>Склад:</b>\n"
        kb = InlineKeyboardBuilder()
        total_price = 0

        if not cart:
            text += "<i>Кошик порожній</i>"
        else:
            for prod_id, item in cart.items():
                item_total = item['price'] * item['quantity']
                total_price += item_total
                text += f"- {html_module.escape(item['name'])} ({item['quantity']} шт.) = {item_total} грн\n"
                kb.row(
                    InlineKeyboardButton(text="➖", callback_data=f"waiter_cart_qnt_{prod_id}_-1"),
                    InlineKeyboardButton(text=f"{item['quantity']}x {html_module.escape(item['name'])}", callback_data="noop"),
                    InlineKeyboardButton(text="➕", callback_data=f"waiter_cart_qnt_{prod_id}_1")
                )
        
        text += f"\n\n<b>Загальна сума: {total_price} грн</b>"
    
        kb.row(InlineKeyboardButton(text="➕ Додати страву", callback_data="waiter_cart_add_item"))
        if cart:
            kb.row(InlineKeyboardButton(text="✅ Оформити замовлення", callback_data="waiter_cart_finalize"))
        # Кнопка "Скасувати" повертає до перегляду столика
        kb.row(InlineKeyboardButton(text="⬅️ Скасувати (повернутись до столика)", callback_data=f"waiter_view_table_{table_id}")) 
    
        try:
            await callback.message.edit_text(text, reply_markup=kb.as_markup())
        except TelegramBadRequest as e:
            logger.warning(f"Error editing message in _display_waiter_cart: {e}")
            pass
        await callback.answer()

    @dp_admin.callback_query(F.data.startswith("waiter_create_order_"))
    async def waiter_create_order_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
        """Початок FSM: Створення нового замовлення для столика."""
        table_id = int(callback.data.split("_")[-1])
        table = await session.get(Table, table_id)
        if not table:
            return await callback.answer("Столик не знайдено!", show_alert=True)
        
        await state.set_state(WaiterCreateOrderStates.managing_cart)
        await state.update_data(cart={}, table_id=table_id, table_name=table.name)
        
        await _display_waiter_cart(callback, state, session)

    @dp_admin.callback_query(WaiterCreateOrderStates.managing_cart, F.data == "waiter_cart_add_item")
    async def waiter_cart_add_item(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
        """Перехід до вибору категорії."""
        await state.set_state(WaiterCreateOrderStates.choosing_category)
        
        categories_res = await session.execute(
            select(Category).where(Category.show_in_restaurant == True).order_by(Category.sort_order, Category.name)
        )
        categories = categories_res.scalars().all()
        
        kb = InlineKeyboardBuilder()
        for cat in categories:
            kb.add(InlineKeyboardButton(text=cat.name, callback_data=f"waiter_cart_cat_{cat.id}"))
        kb.adjust(2)
        kb.row(InlineKeyboardButton(text="⬅️ Назад до кошика", callback_data="waiter_cart_back_to_cart"))
        
        await callback.message.edit_text("Виберіть категорію:", reply_markup=kb.as_markup())
        await callback.answer()

    @dp_admin.callback_query(F.data == "waiter_cart_back_to_cart", WaiterCreateOrderStates.choosing_category)
    @dp_admin.callback_query(F.data == "waiter_cart_back_to_cart", WaiterCreateOrderStates.choosing_product)
    async def waiter_cart_back_to_cart(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
        """Повернення до кошика зі станів вибору."""
        await state.set_state(WaiterCreateOrderStates.managing_cart)
        await _display_waiter_cart(callback, state, session)

    @dp_admin.callback_query(WaiterCreateOrderStates.choosing_category, F.data.startswith("waiter_cart_cat_"))
    async def waiter_cart_show_category(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
        """Показ страв у вибраній категорії."""
        category_id = int(callback.data.split("_")[-1])
        await state.set_state(WaiterCreateOrderStates.choosing_product)
        
        products_res = await session.execute(
            select(Product).where(
                Product.category_id == category_id,
                Product.is_active == True
            ).order_by(Product.name)
        )
        products = products_res.scalars().all()
        
        kb = InlineKeyboardBuilder()
        for prod in products:
            kb.add(InlineKeyboardButton(text=f"{prod.name} - {prod.price} грн", callback_data=f"waiter_cart_prod_{prod.id}"))
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="⬅️ Назад до категорій", callback_data="waiter_cart_back_to_categories"))
        
        await callback.message.edit_text("Виберіть страву:", reply_markup=kb.as_markup())
        await callback.answer()

    @dp_admin.callback_query(F.data == "waiter_cart_back_to_categories", WaiterCreateOrderStates.choosing_product)
    async def waiter_cart_back_to_categories(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
        """Повернення до списку категорій."""
        await waiter_cart_add_item(callback, state, session)

    @dp_admin.callback_query(WaiterCreateOrderStates.choosing_product, F.data.startswith("waiter_cart_prod_"))
    async def waiter_cart_add_product(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
        """Додавання страви до кошика в FSM."""
        product_id = int(callback.data.split("_")[-1])
        product = await session.get(Product, product_id)
        if not product:
            return await callback.answer("Страву не знайдено!", show_alert=True)
        
        data = await state.get_data()
        cart = data.get("cart", {})
        
        prod_id_str = str(product_id)
        if prod_id_str in cart:
            cart[prod_id_str]["quantity"] += 1
        else:
            cart[prod_id_str] = {"name": product.name, "price": product.price, "quantity": 1}
        
        await state.update_data(cart=cart)
        await state.set_state(WaiterCreateOrderStates.managing_cart)
        await _display_waiter_cart(callback, state, session)
        await callback.answer(f"{product.name} додано до кошика.")

    @dp_admin.callback_query(WaiterCreateOrderStates.managing_cart, F.data.startswith("waiter_cart_qnt_"))
    async def waiter_cart_change_quantity(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
        """Зміна кількості страви в кошику FSM."""
        prod_id, change = callback.data.split("_")[3:]
        change = int(change)
        
        data = await state.get_data()
        cart = data.get("cart", {})
        
        if prod_id not in cart:
            return await callback.answer("Помилка: страви немає в кошику.")
        
        cart[prod_id]["quantity"] += change
        
        if cart[prod_id]["quantity"] <= 0:
            del cart[prod_id]
            await callback.answer("Страву видалено.")
        else:
            await callback.answer()
        
        await state.update_data(cart=cart)
        await _display_waiter_cart(callback, state, session)

    @dp_admin.callback_query(WaiterCreateOrderStates.managing_cart, F.data == "waiter_cart_finalize")
    async def waiter_cart_finalize(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
        """Створення замовлення та збереження в БД."""
        data = await state.get_data()
        cart = data.get("cart", {})
        table_id = data.get("table_id")
        table_name = data.get("table_name")
        
        if not cart:
            return await callback.answer("Кошик порожній!", show_alert=True)
        
        employee = await session.scalar(
            select(Employee).where(Employee.telegram_user_id == callback.from_user.id)
        )
        if not employee:
            return await callback.answer("Вас не знайдено в системі.", show_alert=True)

        total_price = 0
        product_strings = []
        for item in cart.values():
            total_price += item['price'] * item['quantity']
            product_strings.append(f"{item['name']} x {item['quantity']}")
        products_str = ", ".join(product_strings)

        # Знаходимо статус "Новий" (який вимагає сповіщення кухні)
        new_status = await session.scalar(select(OrderStatus).where(OrderStatus.name == "Новий").limit(1))
        status_id_to_set = new_status.id if new_status else 1 
        actor_info = f"Офіціант (створив): {employee.full_name}"

        order = Order(
            customer_name=f"Стіл: {table_name}",
            phone_number=f"table_{table_id}",
            address=None,
            products=products_str,
            total_price=total_price,
            is_delivery=False,
            delivery_time="In House",
            order_type="in_house",
            table_id=table_id,
            status_id=status_id_to_set, 
            accepted_by_waiter_id=employee.id # Приймається автоматично
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)
        
        # Додаємо запис в історію
        history_entry = OrderStatusHistory(
            order_id=order.id,
            status_id=order.status_id,
            actor_info=actor_info
        )
        session.add(history_entry)
        await session.commit()
        
        await callback.answer(f"Замовлення #{order.id} створено!", show_alert=True)
        
        # Сповіщення в адмін-чат та поварам
        admin_bot = dp_admin.get("bot_instance")
        if admin_bot:
            await notify_new_order_to_staff(admin_bot, order, session)

        await state.clear()
        await show_waiter_table_orders(callback, session, state, table_id=table_id)