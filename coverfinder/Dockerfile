FROM python:3.13-alpine

COPY finder.py /app/finder.py
USER 65534:65534
EXPOSE 8080
CMD ["python", "-I", "/app/finder.py"]
