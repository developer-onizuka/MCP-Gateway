FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=5001
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway-mcp.py .
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 5001
CMD ["python", "gateway-mcp.py"]
