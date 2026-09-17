FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/seeding
COPY requirements-seeding.lock pyproject.toml README.md ./
COPY app ./app
COPY seeding_entry.py ./
RUN pip install --no-cache-dir -r requirements-seeding.lock \
    && pip install --no-cache-dir . --no-deps

RUN useradd --create-home --uid 10001 seeding \
    && mkdir -p /srv/seeding/runtime \
    && chown -R seeding:seeding /srv/seeding
USER seeding

EXPOSE 8031
CMD ["uvicorn", "seeding_entry:app", "--host", "0.0.0.0", "--port", "8031"]
