FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data DATABASE_PATH=/data/onbuilding.sqlite3 PORT=8000
WORKDIR /app
COPY app.py /app/app.py
COPY connectors/ /app/connectors/
COPY public/ /app/public/
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && mkdir -p /data
EXPOSE 8000
CMD ["sh","-c","gunicorn app:application --bind 0.0.0.0:$PORT --workers 2 --threads 2 --timeout 60"]
