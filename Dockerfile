# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set up the working directory
WORKDIR /app

# Create the data directory and ensure it's writable
RUN mkdir -p /app/data && chmod -R 777 /app/data

# Copy the requirements file and install dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire app directory into the container
COPY . /app/

# Expose the port the app runs on (if applicable)
EXPOSE 5000

# Run the application
CMD ["python", "app.py"]
