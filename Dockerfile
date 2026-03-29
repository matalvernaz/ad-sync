FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install all deps except wxPython, then describealign without deps
RUN pip install --no-cache-dir \
    requests beautifulsoup4 python-dotenv \
    "ffmpeg-python~=0.2.0" "static-ffmpeg~=3.0" "matplotlib~=3.9" \
    "numpy<3.0,>=1.21" "scipy~=1.10" "platformdirs~=4.2" \
    "natsort~=8.4.0" "sortedcontainers~=2.4.0" future \
    && pip install --no-cache-dir --no-deps describealign

COPY . .
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8686
CMD ["describearr", "serve"]
