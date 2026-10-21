FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/

# Postfix/LMTP should be able to call this, or we expose port if using inet
EXPOSE 8024

CMD ["python", "src/app/main.py"]
