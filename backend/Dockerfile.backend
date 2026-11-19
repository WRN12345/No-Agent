FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src
RUN pip install --no-cache-dir "/app[server]"

ENV NO_AGENT_HOME=/data
EXPOSE 8000

CMD ["uvicorn", "no_agent.server:app", "--host", "0.0.0.0", "--port", "8000"]
