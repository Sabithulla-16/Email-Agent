# Use Python 3.12 slim image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# 1. Install system dependencies for Python packages AND Playwright/Chromium
# We manually install the browser dependencies to avoid the broken `playwright install-deps` on Debian
RUN apt-get update && apt-get install -y \
    git \
    wget \
    gnupg \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libxkbcommon0 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    fonts-liberation \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# 2. Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Install Playwright Chromium browser (skip install-deps)
RUN playwright install chromium

# 4. Copy the rest of the application code
COPY . .

# Expose the port Render expects
EXPOSE 10000

# Command to run the application
CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-10000}"]