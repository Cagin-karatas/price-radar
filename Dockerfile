FROM python:3.12-slim

# lxml derleme bagimliliklari; slim imajda yoklar
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Once bagimliliklar: kod degistiginde katman onbellegi bozulmasin
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config/ ./config/
RUN pip install --no-cache-dir --no-deps -e .

# Kok olarak calistirma
RUN useradd --create-home --uid 1000 radar && chown -R radar:radar /app
USER radar

ENTRYPOINT ["priceradar"]
CMD ["--help"]
