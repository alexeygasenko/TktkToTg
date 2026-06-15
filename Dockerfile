FROM denoland/deno:bin-2.5.6 AS deno

FROM python:3.13-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home app \
    && mkdir -p /data \
    && chown -R app:app /app /data

USER app

CMD ["python", "-m", "app.main"]
