FROM python:3.11-slim

WORKDIR /app

# tesseract-ocr: the OCR engine itself (Python's pytesseract is just a
# wrapper around this binary — it does nothing without it).
# poppler-utils: gives pdf2image the pdftoppm binary it needs to rasterise
# PDF pages into images before Tesseract can read them.
# Both are local, offline packages — no network call, no external service,
# consistent with "nothing leaves the machine".
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
