FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# Match the usual host login uid so bind-mounted config.d/*.json are not root:0600.
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --create-home app
USER app

CMD ["python", "zulip_interactive_bot.py", "--config-path", "config.d/config.toml"]
