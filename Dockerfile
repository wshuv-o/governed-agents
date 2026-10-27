FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY governed_agents governed_agents
COPY evals evals
RUN useradd --uid 10001 --no-create-home app
USER 10001
# Same image, two roles. API (default) or worker: `python -m governed_agents.worker`.
CMD ["sh", "-c", "exec uvicorn governed_agents.api:app --host 0.0.0.0 --port ${PORT:-8080}"]
