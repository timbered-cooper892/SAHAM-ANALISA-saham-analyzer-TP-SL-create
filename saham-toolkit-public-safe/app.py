from web_app import app


if __name__ == "__main__":
    import os

    port = int(
        os.environ.get("PORT")
        or os.environ.get("SERVER_PORT")
        or os.environ.get("APP_PORT")
        or os.environ.get("WISP_PORT")
        or 8080
    )
    app.run(host="0.0.0.0", port=port)
