FROM python:3.12-slim

# ENVIRONMENT
ENV DATA_DIR="/data"
ENV TZ="America/Moncton"
ENV PORT="8090"

# SETUP APP
WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/main.py .

# VOLUMES
VOLUME ["/data"]

# PORTS
EXPOSE 8090

CMD ["python", "main.py"]
