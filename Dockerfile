FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY biomars ./biomars
COPY studio ./studio
RUN pip install --no-cache-dir . && useradd --create-home studio
COPY config ./config
COPY web ./web
COPY launch_protocol.py ./
RUN mkdir -p /app/var && chown -R studio:studio /app
USER studio
EXPOSE 8000
CMD ["uvicorn", "studio.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
