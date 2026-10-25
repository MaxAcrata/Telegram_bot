# Деплой Telegram-бота на Hetzner

## Дата: 2026-10-11

## Обзор

Hetzner - немецкий хостинг-провайдер с качественными серверами по доступным ценам. Идеально подходит для хостинга Telegram-ботов.

---

## 1. Выбор тарифа

### Shared Cloud (VPS)

| Тариф | vCPU | RAM | Диск | Цена/мес | Рекомендация |
|---|---|---|---|---|---|
| CX11 | 1 | 2 GB | 20 GB | €3.29 | ✅ Для бота |
| CX21 | 2 | 4 GB | 40 GB | €5.39 | Для бота + БД |
| CX31 | 2 | 8 GB | 80 GB | €10.59 | Для нескольких ботов |
| CX41 | 4 | 16 GB | 160 GB | €20.59 | Для продакшена |

### Dedicated Cloud (выделенные серверы)

| Тариф | vCPU | RAM | Диск | Цена/мес |
|---|---|---|---|---|
| CCX11 | 2 | 8 GB | 80 GB | €19.49 |
| CCX21 | 4 | 16 GB | 160 GB | €33.49 |
| CCX31 | 8 | 32 GB | 320 GB | €61.49 |

### Рекомендация для Telegram-бота

**CX11 (€3.29/мес)** - оптимальный выбор:
- 1 vCPU - достаточно для бота
- 2 GB RAM - хватит для Python + SQLite
- 20 GB диск - много места для БД и фото
- Самая низкая цена

**CX21 (€5.39/мес)** - если нужен запас:
- 2 vCPU - для параллельных задач
- 4 GB RAM - для будущего масштабирования
- 40 GB диск - для большого количества фото

---

## 2. Регистрация на Hetzner

### Шаг 1: Создание аккаунта
1. Зайдите на [hetzner.com](https://www.hetzner.com)
2. Нажмите «Register»
3. Заполните форму:
   - Имя, фамилия
   - Email
   - Пароль
   - Страна
   - Телефон
4. Подтвердите email
5. Подтвердите телефон (SMS)

### Шаг 2: Верификация
1. Зайдите в аккаунт
2. Перейдите в «Account» → «Verification»
3. Загрузите документы:
   - Паспорт или ID карта
   - Подтверждение адреса (счет за коммунальные)
4. Дождитесь подтверждения (обычно1-2 дня)

### Шаг 3: Добавление платежного метода
1. Перейдите в «Account» → «Payment»
2. Добавьте кредитную карту
3. Или PayPal

---

## 3. Создание сервера

### Шаг 1: Создание проекта
1. Зайдите в Cloud Console
2. Нажмите «New Project»
3. Введите название: `telegram-bot`
4. Нажмите «Create»

### Шаг 2: Создание сервера
1. В проекте нажмите «Add Server»
2. Выберите локацию:
   - **Falkenstein** (Германия) - рекомендуется
   - **Helsinki** (Финляндия)
   - **Nuremberg** (Германия)
3. Выберите образ: **Ubuntu 22.04**
4. Выберите тип сервера: **CX11** (€3.29/мес)
5. Добавьте SSH ключ (или создайте новый)
6. Введите имя сервера: `telegram-bot`
7. Нажмите «Create & Buy Now»

### Шаг 3: Получение данных
После создания сервера вы получите:
- IP адрес (например, `157.90.xxx.xxx`)
- Root пароль (отправится на email)

---

## 4. Настройка сервера

### Шаг 1: Подключение к серверу

**Windows (PowerShell):**
```bash
ssh root@ваш_ip_адрес
```

**Или используйте PuTTY:**
1. Скачайте PuTTY
2. Введите IP адрес
3. Порт: 22
4. Нажмите «Open»
5. Введите `root` и пароль

### Шаг 2: Обновление системы
```bash
apt update && apt upgrade -y
```

### Шаг 3: Установка Python
```bash
apt install python3 python3-pip python3-venv -y
```

### Шаг 4: Создание пользователя
```bash
adduser botuser
usermod -aG sudo botuser
su - botuser
```

---

## 5. Деплой бота

### Шаг 1: Клонирование репозитория
```bash
cd /home/botuser
git clone https://github.com/MaxAcrata/Telegram_bot.git
cd Telegram_bot
git checkout ver2
```

### Шаг 2: Создание виртуального окружения
```bash
python3 -m venv venv
source venv/bin/activate
```

### Шаг 3: Установка зависимостей
```bash
pip install -r requirements.txt
```

### Шаг 4: Создание .env файла
```bash
cp .env.example .env
nano .env
```

Заполните переменные:
```env
TELEGRAM_BOT_TOKEN=ваш_токен
TELEGRAM_CHAT_ID=ваш_chat_id
ADMIN_IDS=ваш_id
DB_PATH=/home/botuser/data/bot.db
OVERDUE_DAYS=3
REPORT_HOUR=9
REPORT_MINUTE=0
REPORT_TIMEZONE=Europe/Moscow
```

### Шаг 5: Создание директории для данных
```bash
mkdir -p /home/botuser/data
```

### Шаг 6: Тестовый запуск
```bash
python bot.py
```

Если бот запустился без ошибок, нажмите `Ctrl+C` для остановки.

---

## 6. Настройка автозапуска

### Шаг 1: Создание systemd сервиса
```bash
sudo nano /etc/systemd/system/telegram-bot.service
```

Вставьте следующий текст:
```ini
[Unit]
Description=Telegram Bot for Material Requests
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/home/botuser/Telegram_bot
Environment=PATH=/home/botuser/Telegram_bot/venv/bin:/usr/bin
ExecStart=/home/botuser/Telegram_bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Шаг 2: Запуск сервиса
```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot
```

### Шаг 3: Проверка статуса
```bash
sudo systemctl status telegram-bot
```

### Шаг 4: Просмотр логов
```bash
sudo journalctl -u telegram-bot -f
```

---

## 7. Настройка файрвола

### Шаг 1: Настройка UFW
```bash
sudo ufw allow ssh
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable
```

### Шаг 2: Проверка
```bash
sudo ufw status
```

---

## 8. Настройка автоматических обновлений

### Шаг 1: Создание скрипта обновления
```bash
nano /home/botuser/update.sh
```

Вставьте:
```bash
#!/bin/bash
cd /home/botuser/Telegram_bot
git pull origin ver2
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart telegram-bot
```

### Шаг 2: Сделайте скрипт исполняемым
```bash
chmod +x /home/botuser/update.sh
```

### Шаг 3: Настройка cron для автоматических обновлений
```bash
crontab -e
```

Добавьте строку:
```
0 3 * * * /home/botuser/update.sh >> /home/botuser/update.log 2>&1
```

---

## 9. Настройка мониторинга

### Шаг 1: Установка мониторинга
```bash
sudo apt install htop iftop -y
```

### Шаг 2: Проверка ресурсов
```bash
htop
```

### Шаг 3: Проверка использования диска
```bash
df -h
```

### Шаг 4: Проверка логов
```bash
tail -f /var/log/syslog
```

---

## 10. Резервное копирование

### Шаг 1: Создание скрипта бэкапа
```bash
nano /home/botuser/backup.sh
```

Вставьте:
```bash
#!/bin/bash
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/home/botuser/backups"
mkdir -p $BACKUP_DIR
cp /home/botuser/data/bot.db $BACKUP_DIR/bot_$DATE.db
# Хранить последние7 дней
find $BACKUP_DIR -name "*.db" -mtime +7 -delete
```

### Шаг 2: Сделайте скрипт исполняемым
```bash
chmod +x /home/botuser/backup.sh
```

### Шаг 3: Настройка cron для бэкапов
```bash
crontab -e
```

Добавьте строку:
```
0 2 * * * /home/botuser/backup.sh >> /home/botuser/backup.log 2>&1
```

---

## 11. Безопасность

### Шаг 1: Смена порта SSH
```bash
sudo nano /etc/ssh/sshd_config
```

Измените:
```
Port 2222
```

### Шаг 2: Настройка SSH ключей
```bash
ssh-keygen -t ed25519
```

### Шаг 3: Отключение входа по паролю
```bash
sudo nano /etc/ssh/sshd_config
```

Измените:
```
PasswordAuthentication no
```

### Шаг 4: Перезапуск SSH
```bash
sudo systemctl restart sshd
```

---

## 12. Оптимизация производительности

### Шаг 1: Настройка swap (если RAM <4GB)
```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Шаг 2: Настройка sysctl
```bash
sudo nano /etc/sysctl.conf
```

Добавьте:
```
vm.swappiness=10
```

### Шаг 3: Применение
```bash
sudo sysctl -p
```

---

## 13. Мониторинг и алерты

### Шаг 1: Установка мониторинга
```bash
sudo apt install monit -y
```

### Шаг 2: Настройка мониторинга бота
```bash
sudo nano /etc/monit/conf.d/telegram-bot
```

Вставьте:
```
check process telegram-bot with pidfile /var/run/telegram-bot.pid
    start program = "/bin/systemctl start telegram-bot"
    stop program = "/bin/systemctl stop telegram-bot"
    if 5 restarts within 5 cycles then alert
```

### Шаг 3: Запуск мониторинга
```bash
sudo systemctl enable monit
sudo systemctl start monit
```

---

## 14. Стоимость

### Минимальная конфигурация:
- **Сервер CX11:** €3.29/мес
- **Домен:** €10-15/год (опционально)
- **Итого:** ~€3.29/мес (~$3.50/мес)

### Рекомендуемая конфигурация:
- **Сервер CX21:** €5.39/мес
- **Домен:** €10-15/год
- **Итого:** ~€5.39/мес (~$5.80/мес)

---

## 15. Часто задаваемые вопросы

### В: Как подключиться к серверу?
**О:** Используйте SSH: `ssh root@ваш_ip_адрес`

### В: Как перезапустить бота?
**О:** `sudo systemctl restart telegram-bot`

### В: Как посмотреть логи?
**О:** `sudo journalctl -u telegram-bot -f`

### В: Как обновить бота?
**О:** `/home/botuser/update.sh`

### В: Как сделать бэкап?
**О:** `/home/botuser/backup.sh`

### В: Как изменить тариф?
**О:** В Cloud Console → Server → Resize

---

## 16. Контакт

Для вопросов по деплою обращайтесь к команде разработки или в поддержку Hetzner.