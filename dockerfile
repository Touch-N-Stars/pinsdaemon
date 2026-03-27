# docker build -t pinsdaemon .  
# docker run -p 8000:8000 pinsdaemon     

FROM python:3.11-slim

# System tools (lsblk kommt aus util-linux)
RUN apt-get update && apt-get install -y \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

# Workdir
WORKDIR /app

# Requirements (für caching)
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# App kopieren (WICHTIG)
COPY app ./app

# Port
EXPOSE 8000

# Start
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]