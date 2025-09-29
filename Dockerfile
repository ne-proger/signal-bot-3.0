FROM python:3.11-slim


ENV PYTHONDONTWRITEBYTECODE=1 \
PYTHONUNBUFFERED=1 \
TZ=${TZ:-Europe/Madrid}


RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*


WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt


COPY . .


# создадим каталоги для данных и секретов (могут монтироваться томами)
RUN mkdir -p /app/data /app/secrets


CMD ["python", "-m", "src.main"]