FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

EXPOSE 8000

CMD ["gunicorn", "wsgi:application", "-w", "4", "-b", "0.0.0.0:8000", \
     "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-"]
