# admin_design_settings.py

import html
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models import Settings
from templates import ADMIN_HTML_TEMPLATE, ADMIN_DESIGN_SETTINGS_BODY
from dependencies import get_db_session, check_credentials

router = APIRouter()

@router.get("/admin/design_settings", response_class=HTMLResponse)
async def get_design_settings_page(
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    """Відображає сторінку налаштувань дизайну, SEO та текстів."""
    settings = await session.get(Settings, 1)
    if not settings:
        settings = Settings() # Provide default values if no settings exist

    body = ADMIN_DESIGN_SETTINGS_BODY.format(
        site_title=settings.site_title or "Назва",
        seo_description=settings.seo_description or "",
        seo_keywords=settings.seo_keywords or "",
        primary_color=settings.primary_color or "#5a5a5a",
        font_family_sans_val=settings.font_family_sans or "Golos Text",
        font_family_serif_val=settings.font_family_serif or "Playfair Display",
        telegram_welcome_message=settings.telegram_welcome_message or "Шановний {user_name}, ласкаво просимо! 👋\n\nМи раді вас бачити. Оберіть опцію:",
        font_select_sans_golos="selected" if (settings.font_family_sans or "Golos Text") == "Golos Text" else "",
        font_select_sans_inter="selected" if settings.font_family_sans == "Inter" else "",
        font_select_sans_roboto="selected" if settings.font_family_sans == "Roboto" else "",
        font_select_serif_playfair="selected" if (settings.font_family_serif or "Playfair Display") == "Playfair Display" else "",
        font_select_serif_lora="selected" if settings.font_family_serif == "Lora" else "",
        font_select_serif_merriweather="selected" if settings.font_family_serif == "Merriweather" else "",
    )

    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active"]}
    active_classes["design_active"] = "active"
    
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Дизайн та SEO", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))

@router.post("/admin/design_settings")
async def save_design_settings(
    site_title: str = Form(...),
    seo_description: str = Form(""),
    seo_keywords: str = Form(""),
    primary_color: str = Form(...),
    font_family_sans: str = Form(...),
    font_family_serif: str = Form(...),
    telegram_welcome_message: str = Form(...),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    """Зберігає налаштування дизайну, SEO та текстів."""
    settings = await session.get(Settings, 1)
    if not settings:
        settings = Settings(id=1)
        session.add(settings)

    settings.site_title = site_title
    settings.seo_description = seo_description
    settings.seo_keywords = seo_keywords
    settings.primary_color = primary_color
    settings.font_family_sans = font_family_sans
    settings.font_family_serif = font_family_serif
    settings.telegram_welcome_message = telegram_welcome_message

    await session.commit()
    
    return RedirectResponse(url="/admin/design_settings?saved=true", status_code=303)