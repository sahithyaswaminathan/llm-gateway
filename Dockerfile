FROM python:3.11-slim
WORKDIR /code
RUN useradd --uid 10001 --create-home app
ENV HF_HOME=/data/hf
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY runbooks.yaml ./runbooks.yaml
RUN mkdir -p /data && chown -R app:app /data /code
USER 10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
