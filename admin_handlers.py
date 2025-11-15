# admin_handlers.py

import logging
import html as html_module
from aiogram import F, Dispatcher, Bot, html
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder, KeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from urllib.parse import quote_plus
import re # <--- ДОДАНО

from models import Order, Product, Category, OrderStatus, Employee, Role, Settings, OrderStatusHistory
# --- ПОЧАТОК ЗМІН: Додано _generate_waiter_order_view ---
from courier_handlers import get_operator_keyboard, get_staff_login_keyboard, get_courier_keyboard, _generate_waiter_order_view
# --- КІНЕЦЬ ЗМІН ---
from notification_manager import notify_all_parties_on_status_change

# Налаштування логування
logger = logging.getLogger(__name__)

class AdminEditOrderStates(StatesGroup):
    waiting_for_new_name = State()
    waiting_for_new_phone = State()
    waiting_for_new_address = State()

class OperatorAuthStates(StatesGroup):
    waiting_for_phone = State()

def parse_products_string(products_str: str) -> dict[str, int]:
    """Розбирає рядок 'Назва x Кількість, ...' на словник."""
    if not products_str: return {}
    products_dict = {}
    for part in products_str.split(', '):
        try:
            name, quantity_str = part.rsplit(' x ', 1)
            products_dict[name] = int(quantity_str)
        except ValueError:
            logging.warning(f"Не вдалося розібрати частину рядка продукту: {part}")
    return products_dict

def build_products_string(products_dict: dict[str, int]) -> str:
    """Збирає словник назад у рядок 'Назва x Кількість, ...'."""
    return ", ".join([f"{name} x {quantity}" for name, quantity in products_dict.items()])

async def recalculate_order_total(products_dict: dict[str, int], session: AsyncSession) -> int:
    """Перераховує загальну суму замовлення на основі оновленого складу."""
    total = 0
    if not products_dict: return 0
    products_res = await session.execute(select(Product).where(Product.name.in_(list(products_dict.keys()))))
    db_products = {p.name: p for p in products_res.scalars().all()}
    for name, quantity in products_dict.items():
        if product := db_products.get(name):
            total += product.price * quantity
    return total

async def _generate_order_admin_view(order: Order, session: AsyncSession):
    """Генерує текст та клавіатуру для відображення замовлення в адмін-боті."""
    await session.refresh(order, ['status', 'courier'])
    status_name = order.status.name if order.status else 'Невідомий'
    delivery_info = f"Адреса: {html_module.escape(order.address or 'Не вказана')}" if order.is_delivery else 'Самовивіз'
    time_info = f"Час: {html_module.escape(order.delivery_time)}"
    source = f"Джерело: {'Сайт' if order.user_id is None else 'Telegram-бот'}"
    courier_info = order.courier.full_name if order.courier else 'Не призначений'
    products_formatted = "- " + html_module.escape(order.products or '').replace(", ", "\n- ")

    admin_text = (f"<b>Замовлення #{order.id}</b> ({source})\n\n"
                  f"<b>Клієнт:</b> {html_module.escape(order.customer_name)}\n<b>Телефон:</b> {html_module.escape(order.phone_number)}\n"
                  f"<b>{delivery_info}</b>\n<b>{time_info}</b>\n"
                  f"<b>Кур'єр:</b> {courier_info}\n\n"
                  f"<b>Страви:</b>\n{products_formatted}\n\n<b>Сума:</b> {order.total_price} грн\n\n"
                  f"<b>Статус:</b> {status_name}")

    kb_admin = InlineKeyboardBuilder()
    statuses_res = await session.execute(
        select(OrderStatus).where(OrderStatus.visible_to_operator == True).order_by(OrderStatus.id)
    )
    statuses = statuses_res.scalars().all()
    status_buttons = [
        InlineKeyboardButton(text=f"{'✅ ' if s.id == order.status_id else ''}{s.name}", callback_data=f"change_order_status_{order.id}_{s.id}")
        for s in statuses
    ]
    for i in range(0, len(status_buttons), 2):
        kb_admin.row(*status_buttons[i:i+2])

    courier_button_text = f"👤 Призначити кур'єра ({order.courier.full_name if order.courier else 'Виберіть'})"
    kb_admin.row(InlineKeyboardButton(text=courier_button_text, callback_data=f"select_courier_{order.id}"))
    kb_admin.row(InlineKeyboardButton(text="✏️ Редагувати замовлення", callback_data=f"edit_order_{order.id}"))
    return admin_text, kb_admin.as_markup()

async def _display_order_view(bot: Bot, chat_id: int, message_id: int, order_id: int, session: AsyncSession):
    """Оновлює повідомлення з деталями замовлення."""
    order = await session.get(Order, order_id)
    if not order: return
    admin_text, kb_admin = await _generate_order_admin_view(order, session)
    try:
        await bot.edit_message_text(text=admin_text, chat_id=chat_id, message_id=message_id, reply_markup=kb_admin)
    except TelegramBadRequest as e:
        logger.error(f"Не вдалося відредагувати повідомлення в _display_order_view: {e}")

async def _display_edit_items_menu(bot: Bot, chat_id: int, message_id: int, order_id: int, session: AsyncSession):
    """Показує меню редагування складу замовлення."""
    order = await session.get(Order, order_id)
    if not order: return
    products_dict = parse_products_string(order.products)
    text = f"<b>Склад замовлення #{order.id}</b> (Сума: {order.total_price} грн)\n\n"
    kb = InlineKeyboardBuilder()
    if not products_dict:
        text += "<i>Замовлення порожнє</i>"
    else:
        product_names = list(products_dict.keys())
        products_res = await session.execute(select(Product).where(Product.name.in_(product_names)))
        db_products = {p.name: p for p in products_res.scalars().all()}
        for name, quantity in products_dict.items():
            if product := db_products.get(name):
                kb.row(
                    InlineKeyboardButton(text="➖", callback_data=f"admin_change_qnt_{order.id}_{product.id}_-1"),
                    InlineKeyboardButton(text=f"{html_module.escape(name)}: {quantity}", callback_data="noop"),
                    InlineKeyboardButton(text="➕", callback_data=f"admin_change_qnt_{order.id}_{product.id}_1"),
                    InlineKeyboardButton(text="❌", callback_data=f"admin_delete_item_{order.id}_{product.id}")
                )
    kb.row(InlineKeyboardButton(text="➕ Додати страву", callback_data=f"admin_add_item_start_{order_id}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"edit_order_{order_id}"))
    await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=kb.as_markup())

async def _display_edit_customer_menu(bot: Bot, chat_id: int, message_id: int, order_id: int, session: AsyncSession):
    """Показує меню редагування даних клієнта."""
    order = await session.get(Order, order_id)
    if not order: return

    text = (f"<b>Редагування клієнта (Замовлення #{order.id})</b>\n\n"
            f"<b>Поточне ім'я:</b> {html_module.escape(order.customer_name)}\n"
            f"<b>Поточний телефон:</b> {html_module.escape(order.phone_number)}")

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Змінити ім'я", callback_data=f"change_name_start_{order_id}"),
           InlineKeyboardButton(text="Змінити телефон", callback_data=f"change_phone_start_{order_id}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"edit_order_{order_id}"))

    await bot.edit_message_text(
        text=text,
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=kb.as_markup()
    )

async def _display_edit_delivery_menu(bot: Bot, chat_id: int, message_id: int, order_id: int, session: AsyncSession):
    """Показує меню редагування доставки/самовивозу."""
    order = await session.get(Order, order_id)
    if not order: return

    delivery_type_str = "🚚 Доставка" if order.is_delivery else "🏠 Самовивіз"
    text = (f"<b>Редагування доставки (Замовлення #{order.id})</b>\n\n"
            f"<b>Тип:</b> {delivery_type_str}\n"
            f"<b>Адреса:</b> {html_module.escape(order.address or 'Не вказана')}\n"
            f"<b>Час:</b> {html_module.escape(order.delivery_time or 'Якнайшвидше')}")

    kb = InlineKeyboardBuilder()
    toggle_text = "Зробити Самовивозом" if order.is_delivery else "Зробити Доставкою"
    kb.row(InlineKeyboardButton(text=toggle_text, callback_data=f"toggle_delivery_type_{order.id}"))
    if order.is_delivery:
        kb.row(InlineKeyboardButton(text="Змінити адресу", callback_data=f"change_address_start_{order_id}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"edit_order_{order_id}"))

    await bot.edit_message_text(
        text=text,
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=kb.as_markup()
    )


def register_admin_handlers(dp: Dispatcher):
    @dp.message(F.text == "🔐 Вхід оператора")
    async def operator_login_start(message: Message, state: FSMContext, session: AsyncSession):
        employee = await session.scalar(select(Employee).where(Employee.telegram_user_id == message.from_user.id).options(joinedload(Employee.role)))
        if employee:
            if employee.role.can_manage_orders:
                return await message.answer(f"✅ Ви вже авторизовані як оператор.", reply_markup=get_operator_keyboard(employee))
            elif employee.role.can_be_assigned:
                return await message.answer("❌ Ви авторизовані як кур'єр. Для входу як оператор, спочатку вийдіть із системи.", reply_markup=get_courier_keyboard(employee))
        await state.set_state(OperatorAuthStates.waiting_for_phone)
        kb = InlineKeyboardBuilder().add(InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_auth")).as_markup()
        await message.answer("Будь ласка, введіть номер телефону для ролі **оператора**:", reply_markup=kb)

    @dp.message(OperatorAuthStates.waiting_for_phone)
    async def process_operator_phone(message: Message, state: FSMContext, session: AsyncSession):
        phone = message.text.strip()
        employee = await session.scalar(select(Employee).options(joinedload(Employee.role)).where(Employee.phone_number == phone))
        if employee and employee.role.can_manage_orders:
            employee.telegram_user_id = message.from_user.id
            await session.commit()
            await state.clear()
            await message.answer(f"🎉 Доброго дня, {employee.full_name}! Ви успішно авторизовані як {employee.role.name}.", reply_markup=get_operator_keyboard(employee))
        else:
            await message.answer("❌ Співробітника з таким номером не знайдено або він не має прав Оператора.")
    
    @dp.callback_query(F.data.startswith("change_order_status_"))
    async def change_order_status_admin(callback: CallbackQuery, session: AsyncSession):
        client_bot = dp.get("client_bot")
        employee = await session.scalar(select(Employee).where(Employee.telegram_user_id == callback.from_user.id))
        actor_info = f"Оператор: {employee.full_name}" if employee else f"Оператор (ID: {callback.from_user.id})"
        
        parts = callback.data.split("_")
        order_id, new_status_id = int(parts[3]), int(parts[4])

        order = await session.get(Order, order_id)
        if not order: return await callback.answer("Замовлення не знайдено!", show_alert=True)
        if order.status_id == new_status_id: return await callback.answer("Статус вже встановлено.")

        old_status = await session.get(OrderStatus, order.status_id)
        old_status_name = old_status.name if old_status else 'Невідомий'

        order.status_id = new_status_id
        
        history_entry = OrderStatusHistory(
            order_id=order.id,
            status_id=new_status_id,
            actor_info=actor_info
        )
        session.add(history_entry)
        
        await session.commit()
        
        await notify_all_parties_on_status_change(
            order=order,
            old_status_name=old_status_name,
            actor_info=actor_info,
            admin_bot=callback.bot,
            client_bot=client_bot,
            session=session
        )
        
        await _display_order_view(callback.bot, callback.message.chat.id, callback.message.message_id, order_id, session)
        await callback.answer(f"Статус замовлення #{order.id} змінено.")

    @dp.callback_query(F.data.startswith("edit_order_"))
    async def show_edit_order_menu(callback: CallbackQuery):
        order_id = int(callback.data.split("_")[2])
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="👤 Клієнт", callback_data=f"edit_customer_{order_id}"),
               InlineKeyboardButton(text="🍔 Склад замовлення", callback_data=f"edit_items_{order_id}"))
        kb.row(InlineKeyboardButton(text="🚚 Доставка", callback_data=f"edit_delivery_{order_id}"))
        kb.row(InlineKeyboardButton(text="⬅️ Повернутися до замовлення", callback_data=f"view_order_{order_id}"))
        await callback.message.edit_text(f"📝 <b>Редагування замовлення #{order_id}</b>\nВиберіть, що хочете змінити:", reply_markup=kb.as_markup())
        await callback.answer()

    # --- ПОЧАТОК ЗМІН: Оновлений обробник back_to_order_view ---
    @dp.callback_query(F.data.startswith("view_order_"))
    async def back_to_order_view(callback: CallbackQuery, session: AsyncSession):
        order_id = int(callback.data.split("_")[2])
        
        # Завантажуємо замовлення, щоб перевірити його тип
        order = await session.get(Order, order_id, options=[joinedload(Order.table)])
        if not order:
            return await callback.answer("Помилка: Замовлення не знайдено.", show_alert=True)

        if order.order_type == "in_house":
            # Це замовлення офіціанта, викликаємо представлення офіціанта
            text, keyboard = await _generate_waiter_order_view(order, session)
            try:
                await callback.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest as e:
                logger.warning(f"Error in back_to_order_view (waiter): {e}. Sending new message.")
                # Якщо повідомлення не можна відредагувати (наприклад, воно без тексту), видаляємо старе і надсилаємо нове
                try:
                    await callback.message.delete()
                    await callback.message.answer(text, reply_markup=keyboard)
                except Exception as del_e:
                     logger.error(f"Failed to send replacement message in back_to_order_view: {del_e}")
            await callback.answer()
        else:
            # Це замовлення на доставку/самовивіз, викликаємо звичайне адмін-представлення
            await _display_order_view(callback.bot, callback.message.chat.id, callback.message.message_id, order_id, session)
            await callback.answer()
    # --- КІНЕЦЬ ЗМІН ---

    @dp.callback_query(F.data.startswith("edit_customer_"))
    async def edit_customer_menu_handler(callback: CallbackQuery, session: AsyncSession):
        order_id = int(callback.data.split("_")[2])
        await _display_edit_customer_menu(callback.bot, callback.message.chat.id, callback.message.message_id, order_id, session)
        await callback.answer()

    @dp.callback_query(F.data.startswith("edit_items_"))
    async def edit_items_menu_handler(callback: CallbackQuery, session: AsyncSession):
        order_id = int(callback.data.split("_")[2])
        await _display_edit_items_menu(callback.bot, callback.message.chat.id, callback.message.message_id, order_id, session)
        await callback.answer()

    @dp.callback_query(F.data.startswith("edit_delivery_"))
    async def edit_delivery_menu_handler(callback: CallbackQuery, session: AsyncSession):
        order_id = int(callback.data.split("_")[2])
        await _display_edit_delivery_menu(callback.bot, callback.message.chat.id, callback.message.message_id, order_id, session)
        await callback.answer()

    async def start_fsm_for_edit(callback: CallbackQuery, state: FSMContext, new_state: State, prompt_text: str):
        order_id = int(callback.data.split("_")[-1])
        await state.set_state(new_state)
        await state.update_data(order_id=order_id, message_id=callback.message.message_id)
        await callback.message.edit_text(f"<b>Замовлення #{order_id}</b>: {prompt_text}")
        await callback.answer()

    @dp.callback_query(F.data.startswith("change_name_start_"))
    async def change_name_start(callback: CallbackQuery, state: FSMContext):
        await start_fsm_for_edit(callback, state, AdminEditOrderStates.waiting_for_new_name, "Введіть нове ім'я клієнта.")

    @dp.callback_query(F.data.startswith("change_phone_start_"))
    async def change_phone_start(callback: CallbackQuery, state: FSMContext):
        await start_fsm_for_edit(callback, state, AdminEditOrderStates.waiting_for_new_phone, "Введіть новий номер телефону.")

    @dp.callback_query(F.data.startswith("change_address_start_"))
    async def change_address_start(callback: CallbackQuery, state: FSMContext):
        await start_fsm_for_edit(callback, state, AdminEditOrderStates.waiting_for_new_address, "Введіть нову адресу доставки.")

    async def process_fsm_for_edit(message: Message, state: FSMContext, session: AsyncSession, field_to_update: str, menu_to_return_func):
        data = await state.get_data()
        order_id, message_id = data['order_id'], data['message_id']
        order = await session.get(Order, order_id)
        if order:
            setattr(order, field_to_update, message.text)
            await session.commit()
        await state.clear()
        try: await message.delete()
        except TelegramBadRequest: pass
        await menu_to_return_func(message.bot, message.chat.id, message_id, order_id, session)

    @dp.message(AdminEditOrderStates.waiting_for_new_name)
    async def process_new_name(message: Message, state: FSMContext, session: AsyncSession):
        await process_fsm_for_edit(message, state, session, 'customer_name', _display_edit_customer_menu)

    @dp.message(AdminEditOrderStates.waiting_for_new_phone)
    async def process_new_phone(message: Message, state: FSMContext, session: AsyncSession):
        await process_fsm_for_edit(message, state, session, 'phone_number', _display_edit_customer_menu)

    @dp.message(AdminEditOrderStates.waiting_for_new_address)
    async def process_new_address(message: Message, state: FSMContext, session: AsyncSession):
        await process_fsm_for_edit(message, state, session, 'address', _display_edit_delivery_menu)

    @dp.callback_query(F.data.startswith("admin_change_qnt_") | F.data.startswith("admin_delete_item_"))
    async def admin_modify_item(callback: CallbackQuery, session: AsyncSession):
        parts = callback.data.split("_")
        order_id, product_id = int(parts[3]), int(parts[4])
        order = await session.get(Order, order_id)
        product = await session.get(Product, product_id)
        if not order or not product: return await callback.answer("Помилка!", show_alert=True)

        products_dict = parse_products_string(order.products)
        if "change_qnt" in callback.data:
            new_quantity = products_dict.get(product.name, 0) + int(parts[5])
            if new_quantity > 0: products_dict[product.name] = new_quantity
            else: del products_dict[product.name]
        elif "delete_item" in callback.data and product.name in products_dict:
            del products_dict[product.name]

        order.products = build_products_string(products_dict)
        order.total_price = await recalculate_order_total(products_dict, session)
        await session.commit()
        await _display_edit_items_menu(callback.bot, callback.message.chat.id, callback.message.message_id, order_id, session)
        await callback.answer()

    @dp.callback_query(F.data.startswith("toggle_delivery_type_"))
    async def toggle_delivery_type(callback: CallbackQuery, session: AsyncSession):
        order_id = int(callback.data.split("_")[-1])
        order = await session.get(Order, order_id)
        if not order: return
        order.is_delivery = not order.is_delivery
        if not order.is_delivery: order.address = None
        await session.commit()
        await _display_edit_delivery_menu(callback.bot, callback.message.chat.id, callback.message.message_id, order_id, session)
        await callback.answer()

    @dp.callback_query(F.data.startswith("admin_add_item_start_"))
    async def admin_add_item_start(callback: CallbackQuery, session: AsyncSession):
        order_id = int(callback.data.split("_")[-1])
        categories = (await session.execute(select(Category).order_by(Category.sort_order, Category.name))).scalars().all()
        kb = InlineKeyboardBuilder()
        for cat in categories:
            kb.add(InlineKeyboardButton(text=cat.name, callback_data=f"admin_show_cat_{order_id}_{cat.id}_1"))
        kb.adjust(2)
        kb.row(InlineKeyboardButton(text="⬅️ Назад до складу замовлення", callback_data=f"edit_items_{order_id}"))
        await callback.message.edit_text("Виберіть категорію:", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("admin_show_cat_"))
    async def admin_show_category(callback: CallbackQuery, session: AsyncSession):
        order_id, category_id = map(int, callback.data.split("_")[3:5])
        products = (await session.execute(select(Product).where(Product.category_id == category_id, Product.is_active == True))).scalars().all()
        kb = InlineKeyboardBuilder()
        for prod in products:
            kb.add(InlineKeyboardButton(text=f"{prod.name} ({prod.price} грн)", callback_data=f"admin_add_prod_{order_id}_{prod.id}"))
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="⬅️ Назад до категорій", callback_data=f"admin_add_item_start_{order_id}"))
        await callback.message.edit_text("Виберіть страву:", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("admin_add_prod_"))
    async def admin_add_to_order(callback: CallbackQuery, session: AsyncSession):
        order_id, product_id = map(int, callback.data.split("_")[3:])
        order = await session.get(Order, order_id)
        product = await session.get(Product, product_id)
        if not order or not product: return await callback.answer("Помилка!", show_alert=True)
        products_dict = parse_products_string(order.products)
        products_dict[product.name] = products_dict.get(product.name, 0) + 1
        order.products = build_products_string(products_dict)
        order.total_price = await recalculate_order_total(products_dict, session)
        await session.commit()
        await _display_edit_items_menu(callback.bot, callback.message.chat.id, callback.message.message_id, order_id, session)
        await callback.answer(f"✅ {product.name} додано!")

    @dp.callback_query(F.data.startswith("select_courier_"))
    async def select_courier_start(callback: CallbackQuery, session: AsyncSession):
        order_id = int(callback.data.split("_")[2])
        # ВИПРАВЛЕНО: Збираємо ID усіх ролей, які можуть бути кур'єрами
        courier_roles_res = await session.execute(select(Role.id).where(Role.can_be_assigned == True))
        courier_role_ids = courier_roles_res.scalars().all()
        
        if not courier_role_ids:
            return await callback.answer("Помилка: Роль 'Кур'єр' не знайдена в системі.", show_alert=True)
        
        # Фільтруємо співробітників за усіма знайденими ролями та статусом "на зміні"
        couriers = (await session.execute(select(Employee).where(Employee.role_id.in_(courier_role_ids), Employee.is_on_shift == True).order_by(Employee.full_name))).scalars().all()
        
        kb = InlineKeyboardBuilder()
        text = f"<b>Замовлення #{order_id}</b>\nВиберіть кур'єра (🟢 На зміні):"
        if not couriers:
            text = "❌ На даний момент немає жодного кур'єра на зміні."
        else:
            for courier in couriers:
                kb.add(InlineKeyboardButton(text=courier.full_name, callback_data=f"assign_courier_{order_id}_{courier.id}"))
            kb.adjust(2)
        
        kb.row(InlineKeyboardButton(text="❌ Скасувати призначення", callback_data=f"assign_courier_{order_id}_0"))
        kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"view_order_{order_id}"))
        
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("assign_courier_"))
    async def assign_courier(callback: CallbackQuery, session: AsyncSession):
        settings = await session.get(Settings, 1)
        order_id, courier_id = map(int, callback.data.split("_")[2:])
        order = await session.get(Order, order_id)
        if not order: return await callback.answer("Замовлення не знайдено!", show_alert=True)

        old_courier_id = order.courier_id
        new_courier_name = "Не призначений"

        if old_courier_id and old_courier_id != courier_id:
            old_courier = await session.get(Employee, old_courier_id)
            if old_courier and old_courier.telegram_user_id:
                try:
                    await callback.bot.send_message(old_courier.telegram_user_id, f"❗️ Замовлення #{order.id} було знято з вас оператором.")
                except Exception as e:
                    logging.error(f"Не вдалося повідомити колишнього кур'єра {old_courier.id}: {e}")

        if courier_id == 0:
            order.courier_id = None
        else:
            new_courier = await session.get(Employee, courier_id)
            if not new_courier: return await callback.answer("Кур'єра не знайдено!", show_alert=True)
            order.courier_id = courier_id
            new_courier_name = new_courier.full_name
            
            if new_courier.telegram_user_id:
                try:
                    kb_courier = InlineKeyboardBuilder()
                    statuses_res = await session.execute(select(OrderStatus).where(OrderStatus.visible_to_courier == True).order_by(OrderStatus.id))
                    statuses = statuses_res.scalars().all()
                    kb_courier.row(*[InlineKeyboardButton(text=s.name, callback_data=f"courier_set_status_{order.id}_{s.id}") for s in statuses])
                    
                    if order.is_delivery and order.address:
                        encoded_address = quote_plus(order.address)
                        # ВИПРАВЛЕНО: Правильне посилання на карту
                        map_query = f"https://maps.google.com/?q={encoded_address}"
                        kb_courier.row(InlineKeyboardButton(text="🗺️ На карті", url=map_query))
                    
                    # ВИДАЛЕНО: Кнопка "Зателефонувати клієнту" за запитом користувача.
                        
                    await callback.bot.send_message(
                        new_courier.telegram_user_id,
                        # ОНОВЛЕНО: Додано номер телефону в текст повідомлення
                        f"🔔 Вам призначено нове замовлення!\n\n<b>Замовлення #{order.id}</b>\nАдреса: {html_module.escape(order.address or 'Самовивіз')}\nТелефон: {html_module.escape(order.phone_number)}\nСума: {order.total_price} грн.",
                        reply_markup=kb_courier.as_markup()
                    )
                except Exception as e:
                    logging.error(f"Не вдалося повідомити нового кур'єра {new_courier.telegram_user_id}: {e}")
        
        await session.commit()
        
        if settings and settings.admin_chat_id:
            await callback.bot.send_message(settings.admin_chat_id, f"👤 Замовленню #{order.id} призначено кур'єра: <b>{html_module.escape(new_courier_name)}</b>")
        
        await _display_order_view(callback.bot, callback.message.chat.id, callback.message.message_id, order_id, session)
        await callback.answer(f"Кур'єра призначено: {new_courier_name}")