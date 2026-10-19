# HostBot — صورة جاهزة لأي مكان يدعم Docker (ومنها Hugging Face Spaces)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# مستخدم غير جذري (مطلوب في Hugging Face)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH="/home/user/.local/bin:$PATH"

WORKDIR /app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip -r requirements.txt

COPY --chown=user . .

EXPOSE 7860
CMD ["gunicorn", "-b", "0.0.0.0:7860", "-w", "1", "--timeout", "600", "--graceful-timeout", "30", "app:app"]
