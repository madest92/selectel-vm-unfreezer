FROM python:3.12-alpine

WORKDIR /app

RUN adduser -D -H selectel

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

USER selectel

ENV HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

ENTRYPOINT ["python", "main.py"]
