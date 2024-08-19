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
    curl \
    git \
    sudo \
    libicu-dev \
    zip

# Install .NET SDK
RUN wget https://dot.net/v1/dotnet-install.sh -O dotnet-install.sh \
    && chmod +x dotnet-install.sh \
    && ./dotnet-install.sh --channel 6.0 --install-dir /usr/share/dotnet \
    && ln -s /usr/share/dotnet/dotnet /usr/bin/dotnet

# Clone the slsk-batchdl repository
RUN git clone https://github.com/fiso64/slsk-batchdl.git /app/slsk-batchdl

# Build the slsk-batchdl binary for macOS
WORKDIR /app/slsk-batchdl
RUN chmod +x publish.sh \
    && sed -i 's/arm64/x64/g' publish.sh \
    && sh publish.sh

# Move the compiled binary to /usr/local/bin
RUN find /app/slsk-batchdl -type f -name 'sldl' -exec cp {} /usr/local/bin/sldl \; \
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
