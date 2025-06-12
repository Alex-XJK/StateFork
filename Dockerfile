# Use a lightweight base image
FROM python:3.11-slim
LABEL authors="alex"

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Copy only requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy full project
COPY . .

# Expose port
EXPOSE 8000

# Command to run the API server
CMD ["uvicorn", "app.api_server:app", "--host", "0.0.0.0", "--port", "8000"]
