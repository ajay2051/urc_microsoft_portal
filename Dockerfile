FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -r -u 10001 app && chown -R app /app
USER app
EXPOSE 8080
CMD ["python", "manage.py", "runbolt", "--host", "0.0.0.0", "--port", "8080"]