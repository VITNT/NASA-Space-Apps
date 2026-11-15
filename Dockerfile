# Image for hosting Burnline on a Hugging Face Space (see tools/deploy_huggingface.py).
FROM python:3.13-slim

# Hugging Face Spaces run the container as user id 1000.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt huggingface_hub

COPY --chown=user . .

EXPOSE 7860
CMD ["python", "-m", "tools.space_start"]
