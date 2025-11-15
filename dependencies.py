# dependencies.py
import secrets
import os  # <-- --- ИЗМЕНЕНИЕ 1: Добавлен импорт 'os' ---
import logging  # <-- --- ИЗМЕНЕНИЕ 2: Добавлен импорт 'logging' ---
from typing import Generator
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from models import async_session_maker

security = HTTPBasic()

# --- ИЗМЕНЕНИЕ 3: Функция check_credentials полностью обновлена ---
def check_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    """Проверяет учетные данные администратора из переменных окружения."""
    
    # Получаем эталонные логин/пароль из .env
    # 'admin' будет логином по умолчанию, если в .env не задан ADMIN_USER
    env_user = os.environ.get("ADMIN_USER", "admin")
    env_pass = os.environ.get("ADMIN_PASS") # Пароля по умолчанию НЕТ!
    
    if not env_pass:
        # Если пароль не задан в .env, никто не сможет войти.
        # Это критическая ошибка конфигурации.
        logging.error("КРИТИЧЕСКАЯ ОШИБКА: ADMIN_PASS не установлен в переменных окружения!")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Система не настроена (отсутствует пароль администратора)",
        )

    is_user_ok = secrets.compare_digest(credentials.username, env_user)
    is_pass_ok = secrets.compare_digest(credentials.password, env_pass)
    
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неправильный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
# --- КОНЕЦ ИЗМЕНЕНИЯ 3 ---


async def get_db_session() -> Generator[AsyncSession, None, None]:
    """Создает и предоставляет сессию базы данных для эндпоинта."""
    async with async_session_maker() as session:
        yield session