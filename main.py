import os
import uvicorn

from auth_service.api.app import create_app
from auth_service.settings import get_config

config = get_config()
app = create_app(config)


if __name__ == "__main__":

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))

    uvicorn.run(app, host=host, port=port)
