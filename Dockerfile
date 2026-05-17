FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py claude_core.py telegram_bot.py whatsapp_bot.py slack_bot.py bot.py ./

CMD ["python", "-u", "main.py"]
