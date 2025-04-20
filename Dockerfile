# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set up the working directory
WORKDIR /app

# Set up the working directory for the main application
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app/

# Expose the port the app runs on (if applicable)
EXPOSE 5000

# Run the application
CMD ["python", "app.py"]