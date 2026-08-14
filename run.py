"""Development entry point: ``python run.py``."""

from asset_compensation.config import Settings
from asset_compensation.web.app import create_app

settings = Settings.from_env()
app = create_app(settings)


if __name__ == "__main__":
    app.run(host=settings.host, port=settings.port, debug=False)
