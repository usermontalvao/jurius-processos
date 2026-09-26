FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src TZ=America/Cuiaba
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir httpx fastapi uvicorn
COPY src ./src
VOLUME /app/dados
EXPOSE 8080
CMD ["uvicorn", "jurius_processos.api:app", "--host", "0.0.0.0", "--port", "8080"]
