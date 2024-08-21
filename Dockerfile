# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set up the working directory
WORKDIR /app

# Install dependencies required for extracting zip files and building .NET applications
RUN apt-get update && apt-get install -y \
    unzip \
    wget \
    libicu-dev \
    zip

# Download slsk-batchdl
RUN wget https://github.com/fiso64/slsk-batchdl/releases/download/v2.2.7/slsk-batchdl_linux-x64.zip \
    && unzip slsk-batchdl_linux-x64.zip \
    && rm slsk-batchdl_linux-x64.zip
# Move the compiled binary to /usr/local/bin
RUN find /app/ -type f -name 'sldl' -exec cp {} /usr/local/bin/sldl \; \
    && chmod +x /usr/local/bin/sldl

# Set up the working directory for the main application
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app/

# Expose the port the app runs on (if applicable)
EXPOSE 5000

# Run the application
CMD ["python", "app.py"]